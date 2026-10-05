"""Word (.docx) -> IR using python-docx (pure Python, no ML models).

Handles: heading hierarchy (built-in "Heading N" styles, styles based on them,
and explicit outline levels), numbered/bulleted lists with depth, code-styled
or monospace paragraphs (merged into one code element), and tables with
merged-cell de-duplication. Not handled: text boxes, headers/footers,
footnotes, embedded images (use the Docling parser if you need those).
"""

from __future__ import annotations

import re
from typing import Any

from forge_task_documents.errors import ParseError
from forge_task_documents.models import Element, ElementKind, ParsedDocument, SourceFile, TableData
from forge_task_documents.parsers.base import (
    SectionTracker,
    clean_text,
    core_properties_metadata,
    title_from_filename,
)

_HEADING_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_CODE_STYLES = {"code", "html preformatted", "source code", "macro text", "plain text"}
_MONO_FONTS = {"consolas", "courier new", "courier", "menlo", "monaco", "source code pro"}


class DocxParser:
    name = "docx"
    version = "1"
    extensions = frozenset({"docx", "docm"})
    media_types = frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"})

    def parse(self, source: SourceFile) -> ParsedDocument:
        try:
            import docx
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:  # pragma: no cover
            raise ParseError("python-docx is not installed") from exc

        try:
            document = docx.Document(source.open())
        except Exception as exc:
            raise ParseError(f"cannot open docx {source.filename!r}: {exc}") from exc

        core_title = (document.core_properties.title or "").strip()
        title = core_title
        tracker = SectionTracker()
        elements: list[Element] = []
        code_lines: list[str] = []

        def flush_code() -> None:
            if any(line.strip() for line in code_lines):
                elements.append(
                    Element(
                        kind=ElementKind.CODE,
                        text="\n".join(code_lines).strip("\n"),
                        section_path=tracker.path,
                    )
                )
            code_lines.clear()

        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                style = (item.style.name if item.style is not None else "") or ""
                if _is_code(item, style):
                    code_lines.append(item.text.rstrip())
                    continue
                flush_code()

                text = clean_text(item.text)
                if not text:
                    continue
                if style.lower() == "title":
                    title = title or text
                    continue
                level = _heading_level(item, style)
                if level is not None:
                    path = tracker.push(level, text)
                    elements.append(Element(kind=ElementKind.HEADING, text=text, level=level, section_path=path))
                    continue
                list_level = _list_level(item, style)
                if list_level is not None:
                    elements.append(
                        Element(
                            kind=ElementKind.LIST_ITEM,
                            text=text,
                            level=list_level,
                            section_path=tracker.path,
                        )
                    )
                    continue
                elements.append(Element(kind=ElementKind.PARAGRAPH, text=text, section_path=tracker.path))
            elif isinstance(item, Table):
                flush_code()
                table = _table_data(item)
                if table is None:
                    continue
                elements.append(Element(kind=ElementKind.TABLE, table=table, section_path=tracker.path))
        flush_code()

        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="docx",
            title=title or title_from_filename(source.filename),
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata=core_properties_metadata(document.core_properties),
        )


def _heading_level(paragraph: Any, style_name: str) -> int | None:
    style = paragraph.style
    name = style_name
    for _ in range(4):  # follow custom styles "based on" Heading N
        m = _HEADING_RE.match(name or "")
        if m:
            return int(m.group(1))
        if style is None or style.base_style is None:
            break
        style = style.base_style
        name = style.name
    ppr = paragraph._p.pPr
    if ppr is not None:
        outline = ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}outlineLvl")
        if outline is not None:
            val = outline.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
            if val is not None and val.isdigit() and int(val) < 9:
                return int(val) + 1
    return None


def _list_level(paragraph: Any, style_name: str) -> int | None:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.numPr is not None:
        ilvl = ppr.numPr.ilvl
        return int(ilvl.val) if ilvl is not None and ilvl.val is not None else 0
    if style_name.lower().startswith("list"):
        m = re.search(r"(\d)$", style_name)
        return int(m.group(1)) - 1 if m else 0
    return None


def _is_code(paragraph: Any, style_name: str) -> bool:
    lowered = style_name.lower()
    if lowered in _CODE_STYLES or "code" in lowered:
        return True
    runs = [r for r in paragraph.runs if r.text.strip()]
    if not runs:
        return False
    return all((r.font.name or "").lower() in _MONO_FONTS for r in runs)


def _table_data(table: Any) -> TableData | None:
    rows: list[list[str]] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                # horizontally merged cell: python-docx repeats the same <w:tc>.
                # Keep the column slot (so later values stay under the right
                # header) but don't repeat the text.
                cells.append("")
                continue
            seen.add(key)
            cells.append(clean_text(cell.text).replace("\n", " "))
        if any(cells):
            rows.append(cells)
    if not rows:
        return None
    headers, body = rows[0], rows[1:]
    width = max(len(r) for r in rows)
    headers = [h or f"Column {i + 1}" for i, h in enumerate(headers + [""] * (width - len(headers)))]
    body = [r + [""] * (width - len(r)) for r in body]
    return TableData(headers=headers, rows=body)
