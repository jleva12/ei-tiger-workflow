"""PDF -> IR using pdfplumber (pdfminer.six underneath; pure Python, no ML models).

A PDF has no paragraphs or headings, only positioned glyphs, so structure is
recovered from the layout:

  lines       words clustered by baseline. A two-column page (a gutter clear of
              text down the middle, both sides filled to it) is read column by
              column, with full-width lines (titles, captions) kept in place
              between them.
  headings    the document outline (bookmarks) when it names the headings;
              otherwise short blocks set larger than the body text, levelled
              by size, then bold body-size lines standing alone. The one
              largest block on the first page is the title.
  paragraphs  lines joined until a gap, a font change, a bullet, or a short
              line ending a sentence. Hyphenated line ends are rejoined, and a
              paragraph cut by a page break carries on.
  lists       blocks starting with a bullet or a number, levelled by indent.
  code        consecutive monospace lines, indentation kept.
  tables      ruled tables (pdfplumber's finder), taken out of the text flow.
              One continuing on the next page is merged into it, and a
              "Table 3: ..." line right above one becomes its caption.
              Borderless tables are found by alignment: three or more lines
              whose text sits in the same columns, with clear space between.
  noise       running headers and footers repeated on most pages, and bare
              page numbers, are dropped.

Every element carries its page. A page with no text layer (a scan, or text
turned into outlines) is rendered and read by OCR (``ocr.py``, Tesseract) when
an engine is installed, then laid out like any other page; so is a page that
is mostly one image with a line or two of text stamped on it. A PDF with no
text even then fails with a "no text in pdf" error. The Docling parser
(``HYBRID_DOCUMENTS__PDF_PARSER=docling``) is the heavier alternative.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from forge_task_documents.errors import ParseError
from forge_task_documents.models import (
    Element,
    ElementKind,
    ParsedDocument,
    SourceFile,
    SourceLocation,
    TableData,
)
from forge_task_documents.parsers.base import SectionTracker, clean_text, dedupe_headers, title_from_filename
from forge_task_documents.parsers.ocr import OcrEngine

log = logging.getLogger(__name__)

_BOLD = re.compile(r"bold|black|heavy|demi|semibold|extrabold|ultrabold", re.IGNORECASE)
_MONO = re.compile(r"mono|courier|consol|menlo|monaco|inconsolata|fixedsys|lucidaconsole|sourcecode", re.IGNORECASE)
_BULLET = re.compile(r"^([•●▪■□◦○‣⁃∙·◆◇►▸➢➤✓✔\-–—*])\s+")
_NUMBERED = re.compile(r"^(\(?\d{1,3}[.)]|\(?[a-z][.)]|\(?[ivxlc]{1,5}[.)])\s+", re.IGNORECASE)
_PAGE_NUMBER = re.compile(
    r"^(page\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?(\s*(of|/)\s*\d{1,4})?$|^(?=[ivxlc])c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$",
    re.IGNORECASE,
)
_SECTION_NUMBER = re.compile(r"^(\d+(\.\d+)*|[ivxlc]+|[a-z])[.)]?\s+", re.IGNORECASE)
_CAPTION = re.compile(r"^(table|tab\.|exhibit)\s*[\dIVX]+", re.IGNORECASE)
_CID = re.compile(r"\(cid:\d+\)")
# A glyph with no Unicode mapping, or a Symbol/Wingdings private-use one, opening a line is a bullet.
_GLYPH_BULLET = re.compile(r"^(\(cid:\d+\)|[\ue000-\uf8ff])\s+")
_SENTENCE_END = re.compile(r"[.!?:;\"'”’)\]]$")
_JUNK_TITLE = re.compile(
    r"^(microsoft (word|powerpoint|excel) - |untitled|slide \d+$)|\.(docx?|pptx?|xlsx?|pdf)$", re.I
)
_PLACEHOLDER_META = {"unspecified", "anonymous", "unknown", "none", "n/a", "untitled"}
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})

# Page zones the running headers/footers are looked for in: the first and last lines.
_EDGE_LINES = 2
_INDENT_STEP = 6.0  # points between list levels that count as the same level
_TABLE_MIN_ROWS = 3  # lines with cells that a borderless table needs
_TABLE_MAX_COLUMNS = 15


@dataclass(slots=True)
class _Line:
    page: int
    text: str
    size: float
    bold: bool
    mono: bool
    x0: float
    x1: float
    top: float
    bottom: float
    column: int = 0  # 0 full width (or a one-column page), 1 left, 2 right
    col_x0: float = 0.0  # the text edges of its column
    col_x1: float = 0.0

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


@dataclass(slots=True)
class _Table:
    page: int
    rows: list[list[str]]
    x0: float
    x1: float
    top: float
    bottom: float
    column: int = 0


@dataclass(slots=True)
class _OcrRun:
    """What OCR did while one document was parsed."""

    budget: int
    pages: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Block:
    """Consecutive lines that read as one unit: a paragraph, heading, list item or code."""

    lines: list[_Line] = field(default_factory=list)
    kind: ElementKind = ElementKind.PARAGRAPH
    level: int | None = None

    @property
    def first(self) -> _Line:
        return self.lines[0]

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def size(self) -> float:
        return Counter(round(ln.size * 2) / 2 for ln in self.lines).most_common(1)[0][0]

    @property
    def bold(self) -> bool:
        return all(ln.bold for ln in self.lines)

    @property
    def mono(self) -> bool:
        return all(ln.mono for ln in self.lines)

    @property
    def text(self) -> str:
        return _join_lines([ln.text for ln in self.lines])


_Item = _Line | _Table


class PdfParser:
    name = "pdf"
    version = "1"
    extensions = frozenset({"pdf"})
    media_types = frozenset({"application/pdf", "application/x-pdf"})

    def __init__(
        self,
        *,
        extract_tables: bool = True,
        strip_running_text: bool = True,
        ocr: OcrEngine | None = None,
        ocr_dpi: int = 300,
        max_ocr_pages: int = 200,
    ) -> None:
        self.extract_tables = extract_tables
        self.strip_running_text = strip_running_text
        self.ocr = ocr
        self.ocr_dpi = ocr_dpi
        self.max_ocr_pages = max_ocr_pages

    def parse(self, source: SourceFile) -> ParsedDocument:
        try:
            import pdfplumber
            from pdfminer.pdfdocument import PDFPasswordIncorrect
        except ImportError as exc:  # pragma: no cover
            raise ParseError("pdfplumber is not installed") from exc

        try:
            pdf = pdfplumber.open(source.open())
        except PDFPasswordIncorrect as exc:
            raise ParseError(f"cannot open pdf {source.filename!r}: it is password-protected") from exc
        except Exception as exc:
            raise ParseError(f"cannot open pdf {source.filename!r}: {exc}") from exc

        run = _OcrRun(budget=self.max_ocr_pages)
        with pdf:
            try:
                pages: list[list[_Item]] = []
                for number, page in enumerate(pdf.pages, start=1):
                    pages.append(self._read_page(page, number, run))
                    page.close()  # drop pdfplumber's per-page caches as we go
            except ParseError:
                raise
            except Exception as exc:
                raise ParseError(f"cannot read pdf {source.filename!r}: {exc}") from exc
            info = _info(pdf.metadata or {})
            outline = _outline(pdf)

        if self.strip_running_text:
            _drop_running_text(pages)
        if not any(isinstance(item, _Line) or item.rows for items in pages for item in items):
            raise ParseError(f"no text in pdf {source.filename!r}: {self._why_no_text(run)}")

        builder = _Builder(pages, outline)
        elements, found_title = builder.build()
        title = found_title or info.pop("title", "") or title_from_filename(source.filename)
        info.pop("title", None)
        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="pdf",
            title=title,
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata={**info, "pages": len(pages), **_ocr_metadata(run)},
        )

    def _read_page(self, page: Any, number: int, run: _OcrRun) -> list[_Item]:
        original = page
        overprinted = _overprinted(page.chars)
        if overprinted:  # "fake bold" PDFs print every glyph twice
            page = page.filter(lambda obj: id(obj) not in overprinted)
        tables = _tables(page, number) if self.extract_tables else []
        if tables:
            boxes = [(t.x0, t.top, t.x1, t.bottom) for t in tables]
            page = page.filter(lambda obj: obj.get("object_type") != "char" or not _inside(obj, boxes))
        words = page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False)
        if self.ocr is not None and not tables and _looks_scanned(original, words):
            read = self._ocr_words(original, number, run)
            if read is not None:
                words = read
        return self._layout(words, tables, number, float(page.width))

    def _layout(self, words: list[dict[str, Any]], tables: list[_Table], number: int, width: float) -> list[_Item]:
        clusters = _cluster_lines(words)
        gutter = _gutter(clusters, width)
        parts: list[tuple[int, list[dict[str, Any]]]] = []
        for cluster in clusters:
            if gutter is None or _spans(cluster, gutter):
                parts.append((0, cluster))
                continue
            left = [w for w in cluster if w["x1"] <= gutter[0]]
            right = [w for w in cluster if w["x0"] >= gutter[1]]
            parts.extend((column, part) for column, part in ((1, left), (2, right)) if part)
        if gutter is not None:
            for table in tables:
                if table.x1 <= gutter[0]:
                    table.column = 1
                elif table.x0 >= gutter[1]:
                    table.column = 2
        if self.extract_tables:
            aligned, parts = _aligned_tables(parts, number)
            tables = [*tables, *aligned]
        lines = [line for column, part in parts if (line := _line(part, number, column=column))]
        _set_column_edges(lines)
        return _reading_order([*lines, *tables], two_columns=gutter is not None)

    def _ocr_words(self, page: Any, number: int, run: _OcrRun) -> list[dict[str, Any]] | None:
        """The page's words read by OCR, in points like a text layer's; None
        when OCR is out of budget or fails (the page keeps what it had)."""
        assert self.ocr is not None
        if len(run.pages) >= run.budget:
            run.skipped.append(number)
            return None
        try:
            image = page.to_image(resolution=self.ocr_dpi).original
            found = self.ocr.read(image, dpi=self.ocr_dpi)
        except Exception as exc:  # one unreadable page must not lose the document
            log.warning("ocr failed on pdf page %d: %s", number, exc)
            run.errors.append(f"page {number}: {exc}")
            return None
        run.pages.append(number)
        scale = 72.0 / self.ocr_dpi
        return [
            {
                "text": w.text,
                "x0": w.left * scale,
                "x1": w.right * scale,
                "top": w.top * scale,
                "bottom": w.bottom * scale,
                "size": round(w.line_height * scale * 2) / 2,
                "fontname": f"OCR-{self.ocr.name}",
            }
            for w in found
        ]

    def _why_no_text(self, run: _OcrRun) -> str:
        if self.ocr is None:
            return (
                "its pages are images (a scan?) and no OCR engine is installed. Install Tesseract "
                "(apt tesseract-ocr), or set HYBRID_DOCUMENTS__PDF_PARSER=docling"
            )
        if run.errors:
            return f"OCR failed ({'; '.join(run.errors[:3])})"
        if run.pages:
            return f"OCR found none on {len(run.pages)} scanned page(s): blank, or too faint to read"
        if run.skipped:
            return f"its {len(run.skipped)} scanned page(s) are over the OCR limit of {run.budget} pages per document"
        return "its pages have no text layer and nothing to OCR (blank pages?)"


# --------------------------------------------------------------------------- page layout


def _overprinted(chars: list[dict[str, Any]]) -> set[int]:
    """Glyphs printed again within a point of the same glyph: all but the first.

    A copy set a hair off the first is "fake bold": the first is marked bold
    (its font name gains "-FakeBold") so headings set that way are found.
    pdfplumber's ``dedupe_chars`` drops copies too, but in quadratic time."""
    placed: dict[tuple[str, str, float, int, int], list[dict[str, Any]]] = {}
    drop: set[int] = set()
    for c in chars:
        x, y = float(c["x0"]), float(c["top"])
        font, size, text = str(c.get("fontname")), round(float(c.get("size") or 0), 1), c["text"]
        gx, gy = round(x), round(y)
        first = next(
            (
                other
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for other in placed.get((text, font, size, gx + dx, gy + dy), ())
                if abs(float(other["x0"]) - x) <= 1 and abs(float(other["top"]) - y) <= 1
            ),
            None,
        )
        if first is None:
            placed.setdefault((text, font, size, gx, gy), []).append(c)
            continue
        drop.add(id(c))
        if (float(first["x0"]), float(first["top"])) != (x, y) and not _BOLD.search(font):
            first["fontname"] = f"{font}-FakeBold"
    return drop


def _looks_scanned(page: Any, words: list[dict[str, Any]]) -> bool:
    """Whether a page's text has to be read from its pixels: no text layer (or
    only unmapped glyphs) over an image, outlined text or glyphs; or a page
    that is mostly one image with a line or two of text stamped on it."""
    text = sum(len(_CID.sub("", str(w["text"])).strip()) for w in words)
    width, height = float(page.width), float(page.height)
    area = max(width * height, 1.0)
    covered = 0.0
    for image in page.images:
        w = max(0.0, min(float(image["x1"]), width) - max(float(image["x0"]), 0.0))
        h = max(0.0, min(float(image["bottom"]), height) - max(float(image["top"]), 0.0))
        covered += w * h
    covered /= area
    if text == 0:
        return bool(page.chars) or covered >= 0.1 or len(page.curves) >= 100
    return text < 200 and covered >= 0.5


def _ocr_metadata(run: _OcrRun) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if run.pages:
        out["ocr_pages"] = run.pages
    if run.skipped:
        out["ocr_skipped_pages"] = run.skipped  # over the per-document budget
    if run.errors:
        out["ocr_errors"] = run.errors[:10]
    return out


def _segments(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A line's words grouped by the wide gaps between them: its would-be cells."""
    groups = [[words[0]]]
    for prev, word in zip(words, words[1:], strict=False):
        size = max(float(prev["size"]), float(word["size"]), 1.0)
        if word["x0"] - prev["x1"] > max(1.5 * size, 6.0):
            groups.append([word])
        else:
            groups[-1].append(word)
    return groups


def _is_mono(words: list[dict[str, Any]]) -> bool:
    return all(_MONO.search(str(w.get("fontname") or "")) for w in words)


def _aligned_tables(
    parts: list[tuple[int, list[dict[str, Any]]]], number: int
) -> tuple[list[_Table], list[tuple[int, list[dict[str, Any]]]]]:
    """Borderless tables: runs of lines in one column whose words fall into
    the same columns, with clear space between. Returns the tables and the
    lines left over."""
    tables: list[_Table] = []
    taken: set[int] = set()
    by_column: dict[int, list[int]] = {}
    for i, (column, _) in enumerate(parts):
        by_column.setdefault(column, []).append(i)

    def flush(run: list[int], column: int) -> None:
        while run and len(_segments(parts[run[-1]][1])) < 2:
            run.pop()  # a lone line after the table is the next paragraph's
        table = _table_from_lines([parts[i][1] for i in run], number, column) if run else None
        if table is not None:
            tables.append(table)
            taken.update(run)

    for column, indexes in by_column.items():
        run: list[int] = []
        for i in indexes:
            words = parts[i][1]
            if run:
                prev = parts[run[-1]][1]
                height = max(max(w["bottom"] for w in prev) - min(w["top"] for w in prev), 1.0)
                if min(w["top"] for w in words) - max(w["bottom"] for w in prev) > 2.5 * height:
                    flush(run, column)
                    run = []
            if _is_mono(words):
                flush(run, column)
                run = []
            elif len(_segments(words)) >= 2 or run:
                run.append(i)  # a lone line inside a run may be a wrapped cell
            else:
                flush(run, column)
                run = []
        flush(run, column)
    return tables, [part for i, part in enumerate(parts) if i not in taken]


def _table_from_lines(lines: list[list[dict[str, Any]]], number: int, column: int) -> _Table | None:
    rows_segments = [_segments(words) for words in lines]
    multi = [segs for segs in rows_segments if len(segs) >= 2]
    if len(multi) < _TABLE_MIN_ROWS:
        return None
    sizes = sorted(float(w["size"]) for words in lines for w in words)
    channel = max(0.5 * sizes[len(sizes) // 2], 3.0)
    # The columns: the cells' extents merged, wherever less than a channel apart.
    spans = sorted((min(w["x0"] for w in seg), max(w["x1"] for w in seg)) for segs in multi for seg in segs)
    columns: list[list[float]] = [list(spans[0])]
    for x0, x1 in spans[1:]:
        if x0 - columns[-1][1] < channel:
            columns[-1][1] = max(columns[-1][1], x1)
        else:
            columns.append([x0, x1])
    if not 2 <= len(columns) <= _TABLE_MAX_COLUMNS:
        return None

    def columns_of(seg: list[dict[str, Any]]) -> list[int]:
        x0, x1 = min(w["x0"] for w in seg), max(w["x1"] for w in seg)
        return [i for i, (c0, c1) in enumerate(columns) if x0 < c1 and x1 > c0]

    spread = sum(1 for segs in multi if len({c for seg in segs for c in columns_of(seg)}) >= 2)
    if spread < 0.75 * len(multi):
        return None

    rows: list[list[str]] = []
    for segs in rows_segments:
        if len(segs) == 1 and rows:
            hit = columns_of(segs[0])
            if len(hit) == 1 and hit[0] > 0:  # a wrapped cell: carry on the row above
                cell = rows[-1][hit[0]]
                rows[-1][hit[0]] = f"{cell} {_words_text(segs[0])}".strip()
                continue
        row = [""] * len(columns)
        for seg in segs:
            hit = columns_of(seg) or [0]
            row[hit[0]] = f"{row[hit[0]]} {_words_text(seg)}".strip()
        rows.append(row)
    words = [w for line in lines for w in line]
    return _Table(
        page=number,
        rows=rows,
        x0=min(w["x0"] for w in words),
        x1=max(w["x1"] for w in words),
        top=min(w["top"] for w in words),
        bottom=max(w["bottom"] for w in words),
        column=column,
    )


def _words_text(words: list[dict[str, Any]]) -> str:
    return clean_text(_CID.sub("", " ".join(str(w["text"]) for w in words)).translate(_LIGATURES))


def _tables(page: Any, number: int) -> list[_Table]:
    out: list[_Table] = []
    for found in page.find_tables():
        rows = [[_cell(c) for c in row] for row in found.extract()]
        rows = _trim_table(rows)
        if sum(1 for r in rows if sum(1 for c in r if c) >= 2) < 2:
            continue  # boxed paragraphs (or text highlights), not a table: leave the text in the flow
        x0, top, x1, bottom = found.bbox
        out.append(_Table(page=number, rows=rows, x0=x0, x1=x1, top=top, bottom=bottom))
    return out


def _cell(value: str | None) -> str:
    return clean_text(_CID.sub("", value or "").translate(_LIGATURES)).replace("\n", " ")


def _trim_table(rows: list[list[str]]) -> list[list[str]]:
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    used = [i for i in range(width) if any(r[i] for r in padded)]
    return [[r[i] for i in used] for r in padded]


def _inside(obj: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return any(x0 - 1 <= cx <= x1 + 1 and top - 1 <= cy <= bottom + 1 for x0, top, x1, bottom in boxes)


def _cluster_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Words that share a baseline band, each line's words left to right."""
    lines: list[list[dict[str, Any]]] = []
    bands: list[tuple[float, float]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if bands:
            top, bottom = bands[-1]
            overlap = min(bottom, word["bottom"]) - max(top, word["top"])
            if overlap >= 0.5 * min(bottom - top, word["bottom"] - word["top"]):
                lines[-1].append(word)
                continue
        lines.append([word])
        bands.append((word["top"], word["bottom"]))
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _gutter(lines: list[list[dict[str, Any]]], width: float) -> tuple[float, float] | None:
    """The x-range between two text columns, when the page has two.

    A gutter is a band in the middle of the page that at most a few lines
    (full-width titles) cross, with plenty of lines on both sides, and with
    the left side's lines running up to it: labels in a form stop short.
    """
    if len(lines) < 12 or width <= 0:
        return None
    cover = [0] * (int(width) + 2)
    for line in lines:
        for word in line:
            for x in range(max(0, int(word["x0"])), min(len(cover), math.ceil(word["x1"]))):
                cover[x] += 1
    allowed = max(1, len(lines) // 5)
    lo, hi = int(width * 0.3), int(width * 0.7)
    best: tuple[int, int] | None = None
    start: int | None = None
    for x in range(lo, hi + 2):
        if x <= hi and cover[x] <= allowed:
            start = x if start is None else start
            continue
        if start is not None and x - start >= 8 and (best is None or x - start > best[1] - best[0]):
            best = (start, x)
        start = None
    if best is None:
        return None
    g0, g1 = float(best[0]), float(best[1])
    lefts = [max(w["x1"] for w in line if w["x1"] <= g0) for line in lines if any(w["x1"] <= g0 for w in line)]
    rights = [line for line in lines if any(w["x0"] >= g1 for w in line)]
    if len(lefts) < 6 or len(rights) < 6:
        return None
    left_edge = min(w["x0"] for line in lines for w in line)
    reach = g0 - 0.25 * (g0 - left_edge)
    if sum(1 for x1 in lefts if x1 >= reach) < 0.5 * len(lefts):
        return None
    return g0, g1


def _spans(line: list[dict[str, Any]], gutter: tuple[float, float]) -> bool:
    return any(w["x0"] < gutter[1] and w["x1"] > gutter[0] for w in line)


def _line(words: list[dict[str, Any]], page: int, *, column: int) -> _Line | None:
    text = _GLYPH_BULLET.sub("• ", " ".join(w["text"] for w in words))
    text = clean_text(_CID.sub("", text).translate(_LIGATURES))
    if not text:
        return None
    chars = Counter[float]()
    bold = mono = total = 0
    for w in words:
        n = len(w["text"])
        chars[round(float(w["size"]) * 2) / 2] += n
        font = str(w.get("fontname") or "").split("+")[-1]
        bold += n if _BOLD.search(font) else 0
        mono += n if _MONO.search(font) else 0
        total += n
    return _Line(
        page=page,
        text=text,
        size=chars.most_common(1)[0][0],
        bold=bold >= 0.8 * total,
        mono=mono >= 0.8 * total,
        x0=min(w["x0"] for w in words),
        x1=max(w["x1"] for w in words),
        top=min(w["top"] for w in words),
        bottom=max(w["bottom"] for w in words),
        column=column,
    )


def _set_column_edges(lines: list[_Line]) -> None:
    edges: dict[int, tuple[float, float]] = {}
    for column in {ln.column for ln in lines}:
        members = [ln for ln in lines if ln.column == column]
        edges[column] = (min(ln.x0 for ln in members), max(ln.x1 for ln in members))
    for ln in lines:
        ln.col_x0, ln.col_x1 = edges[ln.column]


def _reading_order(items: list[_Item], *, two_columns: bool) -> list[_Item]:
    items = sorted(items, key=lambda i: (i.top, i.x0))
    if not two_columns:
        return items
    ordered: list[_Item] = []
    left: list[_Item] = []
    right: list[_Item] = []
    for item in items:
        if item.column == 1:
            left.append(item)
        elif item.column == 2:
            right.append(item)
        else:  # full width: the columns above it are read first
            ordered += left + right + [item]
            left, right = [], []
    return ordered + left + right


def _drop_running_text(pages: list[list[_Item]]) -> None:
    """Remove bare page numbers, and lines repeated at the top or bottom of
    most pages (digits ignored, so "Page 3 of 9" repeats)."""

    def key(text: str) -> str:
        return re.sub(r"\d+", "#", text.lower()).strip()

    def edges(items: list[_Item]) -> list[_Line]:
        lines = [i for i in items if isinstance(i, _Line)]
        by_top = sorted(lines, key=lambda ln: ln.top)
        return by_top[:_EDGE_LINES] + by_top[-_EDGE_LINES:]

    seen: Counter[str] = Counter()
    for items in pages:
        seen.update({key(ln.text) for ln in edges(items)})
    threshold = max(3, math.ceil(0.5 * len(pages)))
    for items in pages:
        drop = {
            id(ln)
            for ln in edges(items)
            if _PAGE_NUMBER.match(ln.text) or (len(pages) >= 3 and seen[key(ln.text)] >= threshold)
        }
        items[:] = [i for i in items if id(i) not in drop]


# --------------------------------------------------------------------------- structure


def _is_list_start(text: str) -> bool:
    return bool(_BULLET.match(text) or _NUMBERED.match(text))


def _join_lines(texts: list[str]) -> str:
    out = ""
    for text in texts:
        if not out:
            out = text
        elif out.endswith("\xad"):
            out = out[:-1] + text
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and text[:1].islower():
            out = out[:-1] + text  # "recon-" + "ciliation"
        else:
            out += " " + text
    return out.replace("\xad", "")


def _code_text(lines: list[_Line]) -> str:
    left = min(ln.x0 for ln in lines)
    rows = []
    for ln in lines:
        indent = round((ln.x0 - left) / max(ln.size * 0.6, 1.0))
        rows.append(" " * indent + ln.text)
    return "\n".join(rows)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def _heading_key(text: str) -> str:
    """How a heading and its bookmark are matched: section numbers aside."""
    return _norm(_SECTION_NUMBER.sub("", text, count=1))


def _reads_as_heading(text: str) -> bool:
    """A short phrase starting with a capital or a number, not a sentence
    fragment: "Refund policy", "1. General.", not "peripheral devices. By ..."."""
    if len(text) > 120 or text.endswith((",", ";")) or not (text[:1].isupper() or text[:1].isdigit()):
        return False
    phrase = _SECTION_NUMBER.sub("", text, count=1)
    if re.search(r"[.!?]\s", phrase):
        return False
    return not phrase.endswith(".") or (phrase != text and len(phrase.split()) <= 8)


def _numbered_to_list(blocks: list[_Block]) -> None:
    for b in blocks:
        if b.kind is ElementKind.PARAGRAPH and _NUMBERED.match(b.first.text):
            b.kind = ElementKind.LIST_ITEM


class _Builder:
    """Turns the pages' lines and tables into IR elements."""

    def __init__(self, pages: list[list[_Item]], outline: dict[str, int]) -> None:
        self.pages = pages
        self.outline = outline
        lines = [i for items in pages for i in items if isinstance(i, _Line)]
        body = Counter[float]()
        for ln in lines:
            if not ln.mono:
                body[ln.size] += len(ln.text)
        self.body = body.most_common(1)[0][0] if body else 10.0
        gaps = [
            b.top - a.bottom
            for items in pages
            for a, b in zip(items, items[1:], strict=False)
            if isinstance(a, _Line) and isinstance(b, _Line) and a.column == b.column and abs(a.size - b.size) < 0.6
        ]
        gaps = sorted(g for g in gaps if -2 < g < 2 * self.body)
        # The usual gap between a paragraph's lines. Capped, for documents of one-line paragraphs.
        self.line_gap = min(gaps[len(gaps) // 2], 0.8 * self.body) if gaps else 0.2 * self.body

    # ---- blocks

    def _breaks(self, block: _Block, prev: _Line, line: _Line) -> bool:
        if line.column != prev.column or line.page != prev.page:
            return True
        if abs(line.size - prev.size) >= 0.6 or line.mono != prev.mono:
            return True
        width = max(prev.col_x1 - prev.col_x0, 1.0)
        short = prev.x1 < prev.col_x1 - 0.15 * width
        if line.bold != prev.bold and short:
            return True
        gap = line.top - prev.bottom
        if gap > max(0.6 * prev.size, 1.6 * self.line_gap + 1.0) or gap < -prev.height:
            return True
        if line.mono:
            return False
        if _is_list_start(line.text):
            return True
        first = block.first
        hanging = _BULLET.match(first.text) or (len(block.lines) > 1 and block.lines[1].x0 > first.x0 + 2)
        if hanging and line.x0 <= first.x0 + 2 and _is_list_start(first.text):
            return True  # back out at the bullet's edge: the item has ended
        if short and _SENTENCE_END.search(prev.text):
            return True
        return line.x0 > prev.x0 + 8 and _SENTENCE_END.search(prev.text) is not None  # first-line indent

    def _blocks(self, items: list[_Item]) -> list[_Block | _Table]:
        out: list[_Block | _Table] = []
        current: _Block | None = None
        for item in items:
            if isinstance(item, _Table):
                out.append(item)
                current = None
                continue
            if current is not None and not self._breaks(current, current.lines[-1], item):
                current.lines.append(item)
                continue
            current = _Block([item])
            out.append(current)
        return out

    # ---- headings

    def _classify(self, blocks: list[_Block]) -> _Block | None:
        """Set each block's kind and level; return the title block, if any."""
        big = self.body + max(1.0, 0.12 * self.body)
        for b in blocks:
            if b.mono:
                b.kind = ElementKind.CODE
            elif _BULLET.match(b.first.text):
                b.kind = ElementKind.LIST_ITEM
            elif _NUMBERED.match(b.first.text) and b.size < big and not b.bold:
                b.kind = ElementKind.LIST_ITEM  # "1. Introduction" set large or bold may be a heading

        def headingish(b: _Block) -> bool:
            text = b.text
            return (
                b.kind is ElementKind.PARAGRAPH
                and len(b.lines) <= 3
                and len(text) <= 200
                and sum(c.isalpha() for c in text) >= 2
            )

        title: _Block | None = None
        candidates = [b for b in blocks if headingish(b) and b.size >= big]
        if candidates:
            top_size = max(b.size for b in candidates)
            largest = [b for b in candidates if b.size == top_size]
            first_page = min(b.page for b in blocks)
            if len(largest) == 1 and largest[0].page == first_page:
                title = largest[0]

        if self._apply_outline(blocks, title):
            _numbered_to_list(blocks)
            return title

        sizes: list[float] = []
        for size in sorted({b.size for b in candidates if b is not title}, reverse=True):
            if not sizes or sizes[-1] - size >= 0.6:
                sizes.append(size)
        for b in candidates:
            if b is title:
                continue
            level = next((i for i, s in enumerate(sizes, start=1) if b.size >= s - 0.3), len(sizes))
            b.kind, b.level = ElementKind.HEADING, min(level, 6)
        minor = min(len(sizes) + 1, 6)
        for b in blocks:
            if (
                headingish(b)
                and b.bold
                and b.size < big
                and abs(b.size - self.body) < 1.0
                and len(b.lines) <= 2
                and _reads_as_heading(b.text)
            ):
                b.kind, b.level = ElementKind.HEADING, minor
        _numbered_to_list(blocks)
        return title

    def _apply_outline(self, blocks: list[_Block], title: _Block | None) -> bool:
        """Headings from the bookmarks, when enough of them name a block."""
        if len(self.outline) < 3:
            return False
        matched = [
            (b, self.outline[_heading_key(b.text)])
            for b in blocks
            if b is not title and b.kind is ElementKind.PARAGRAPH and _heading_key(b.text) in self.outline
        ]
        if len({_heading_key(b.text) for b, _ in matched}) < max(3, len(self.outline) // 2):
            return False
        for b, level in matched:
            b.kind, b.level = ElementKind.HEADING, min(level, 6)
        return True

    # ---- elements

    def build(self) -> tuple[list[Element], str]:
        per_page = [self._blocks(items) for items in self.pages]
        blocks = [b for page in per_page for b in page if isinstance(b, _Block)]
        title = self._classify(blocks)
        list_levels = _list_levels([b for b in blocks if b.kind is ElementKind.LIST_ITEM])

        tracker = SectionTracker()
        elements: list[Element] = []
        column = 0
        for page in per_page:
            first_on_page = True
            for block in page:
                if block is title:
                    continue
                if isinstance(block, _Table):
                    self._add_table(elements, block, tracker, continues=first_on_page)
                    first_on_page = False
                    continue
                loc = SourceLocation(page=block.page)
                moved = first_on_page or block.first.column != column
                column = block.first.column
                if block.kind is ElementKind.HEADING:
                    text = block.text
                    level = block.level or 1
                    elements.append(
                        Element(
                            kind=ElementKind.HEADING,
                            text=text,
                            level=level,
                            section_path=tracker.push(level, text),
                            location=loc,
                        )
                    )
                elif block.kind is ElementKind.CODE:
                    elements.append(
                        Element(
                            kind=ElementKind.CODE, text=_code_text(block.lines), section_path=tracker.path, location=loc
                        )
                    )
                elif block.kind is ElementKind.LIST_ITEM:
                    text = _BULLET.sub("", block.text, count=1)
                    elements.append(
                        Element(
                            kind=ElementKind.LIST_ITEM,
                            text=text,
                            level=list_levels.get(id(block), 0),
                            section_path=tracker.path,
                            location=loc,
                        )
                    )
                else:
                    text = block.text
                    prev = elements[-1] if elements else None
                    if (
                        moved
                        and prev is not None
                        and prev.kind is ElementKind.PARAGRAPH
                        and not _SENTENCE_END.search(prev.text)
                        and text[:1].islower()
                    ):
                        prev.text = _join_lines([prev.text, text])  # carried over a page or column break
                    else:
                        elements.append(
                            Element(kind=ElementKind.PARAGRAPH, text=text, section_path=tracker.path, location=loc)
                        )
                first_on_page = False
        return elements, title.text if title else ""

    @staticmethod
    def _add_table(elements: list[Element], table: _Table, tracker: SectionTracker, *, continues: bool) -> None:
        rows = table.rows
        prev = elements[-1] if elements else None
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        if continues and prev is not None and prev.kind is ElementKind.TABLE and prev.table is not None:
            if len(prev.table.headers) == width:  # the same table, carried on to this page
                body = rows[1:] if rows[0] == prev.table.headers else rows
                prev.table.rows.extend(body)
                return
        caption = None
        if (
            prev is not None
            and prev.kind is ElementKind.PARAGRAPH
            and prev.location.page == table.page
            and len(prev.text) <= 200
            and _CAPTION.match(prev.text)
        ):
            caption = elements.pop().text
        header, body = rows[0], rows[1:]
        filled = [h for h in header if h]
        if len(filled) < max(1, width // 2):
            header, body = [""] * width, rows
        headers = dedupe_headers([h or f"Column {i + 1}" for i, h in enumerate(header)])
        elements.append(
            Element(
                kind=ElementKind.TABLE,
                table=TableData(headers=headers, rows=body, caption=caption),
                section_path=tracker.path,
                location=SourceLocation(page=table.page),
            )
        )


def _list_levels(items: list[_Block]) -> dict[int, int]:
    """List depth from indentation: each distinct indent of a bullet (relative
    to its column), smallest first, is one level deeper."""
    offsets = sorted({round(b.first.x0 - b.first.col_x0, 1) for b in items})
    steps: list[float] = []
    for offset in offsets:
        if not steps or offset - steps[-1] >= _INDENT_STEP:
            steps.append(offset)
    levels: dict[int, int] = {}
    for b in items:
        offset = b.first.x0 - b.first.col_x0
        levels[id(b)] = max(0, sum(1 for s in steps if offset >= s - _INDENT_STEP / 2) - 1)
    return levels


# --------------------------------------------------------------------------- document info


def _info(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, name in (("Author", "author"), ("Subject", "subject"), ("Keywords", "keywords")):
        value = _meta_text(meta.get(key))
        if value and value.lower() not in _PLACEHOLDER_META:
            out[name] = value
    for key, name in (("CreationDate", "created"), ("ModDate", "modified")):
        date = _pdf_date(_meta_text(meta.get(key)))
        if date:
            out[name] = date
    title = _meta_text(meta.get("Title"))
    if title and not _JUNK_TITLE.search(title):
        out["title"] = title
    return out


def _meta_text(value: Any) -> str:
    if isinstance(value, bytes):
        from pdfminer.utils import decode_text

        value = decode_text(value)
    return clean_text(str(value)) if value else ""


def _pdf_date(value: str) -> str | None:
    """PDF dates look like D:20240131094500+01'00'."""
    m = re.match(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", value)
    if not m:
        return None
    year, month, day, hour, minute, second = (
        g or d for g, d in zip(m.groups(), ("", "01", "01", "00", "00", "00"), strict=True)
    )
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}"


def _outline(pdf: Any) -> dict[str, int]:
    """The bookmarks' titles (normalised) and levels, 1 at the top."""
    out: dict[str, int] = {}
    try:
        for level, title, *_ in pdf.doc.get_outlines():
            text = _heading_key(_meta_text(title))
            if text:
                out.setdefault(text, int(level))
    except Exception:  # no outline, or a broken one: fall back to fonts
        return {}
    return out
