"""Plain text -> IR. Paragraphs are blank-line separated; encoding is detected.

Light structure recovery: a short line followed by an underline of ``===`` or
``---`` (setext style) or a short ALL CAPS line on its own becomes a heading,
which gives the chunker real section boundaries in "unstructured" text.
"""

from __future__ import annotations

import re

from forge_task_documents.models import Element, ElementKind, ParsedDocument, SourceFile, SourceLocation
from forge_task_documents.parsers.base import SectionTracker, clean_text, title_from_filename

_UNDERLINE = re.compile(r"^(=+|-+)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def decode_bytes(data: bytes, *, fallback: str = "cp1252", detect_min_bytes: int = 2048) -> str:
    """BOM -> strict UTF-8 -> statistical detection (only on enough bytes to be
    reliable; it misfires on short text) -> ``fallback`` (cp1252: the usual
    legacy encoding of Western office files) -> latin-1, which never fails."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if len(data) >= detect_min_bytes:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(data).best()
            if best is not None and best.chaos < 0.1:
                return str(best)
        except ImportError:  # pragma: no cover
            pass
    try:
        return data.decode(fallback)
    except UnicodeDecodeError:
        return data.decode("latin-1")


class TextParser:
    name = "text"
    version = "1"
    extensions = frozenset({"txt", "text", "log"})
    media_types = frozenset({"text/plain"})

    def parse(self, source: SourceFile) -> ParsedDocument:
        text = decode_bytes(source.data).replace("\r\n", "\n").replace("\r", "\n")
        tracker = SectionTracker()
        elements: list[Element] = []
        lines = text.split("\n")
        para: list[str] = []
        para_start = 0

        def flush(end_line: int) -> None:
            if not para:
                return
            body = clean_text("\n".join(para))
            if body:
                loc = SourceLocation(line_start=para_start + 1, line_end=end_line)
                if all(_BULLET.match(p) for p in para if p.strip()):
                    for offset, item in enumerate(para):
                        if item.strip():
                            line_no = para_start + offset + 1
                            indent = len(item) - len(item.lstrip())
                            elements.append(
                                Element(
                                    kind=ElementKind.LIST_ITEM,
                                    text=_BULLET.sub("", item).strip(),
                                    level=indent // 2,
                                    section_path=tracker.path,
                                    location=SourceLocation(line_start=line_no, line_end=line_no),
                                )
                            )
                else:
                    elements.append(
                        Element(
                            kind=ElementKind.PARAGRAPH,
                            text=" ".join(body.split("\n")),
                            section_path=tracker.path,
                            location=loc,
                        )
                    )
            para.clear()

        i = 0
        while i < len(lines):
            line = lines[i]
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            stripped = line.strip()
            if not stripped:
                flush(i)
                i += 1
                continue
            if not para and _is_setext(stripped, nxt):
                level = 1 if nxt.strip().startswith("=") else 2
                self._heading(elements, tracker, stripped, level, i)
                i += 2
                continue
            if not para and _is_caps_heading(stripped):
                self._heading(elements, tracker, stripped, 2, i)
                i += 1
                continue
            if not para:
                para_start = i
            para.append(line)
            i += 1
        flush(len(lines))

        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="text",
            title=title_from_filename(source.filename),
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
        )

    @staticmethod
    def _heading(elements: list[Element], tracker: SectionTracker, text: str, level: int, line: int) -> None:
        path = tracker.push(level, text)
        elements.append(
            Element(
                kind=ElementKind.HEADING,
                text=text,
                level=level,
                section_path=path,
                location=SourceLocation(line_start=line + 1, line_end=line + 1),
            )
        )


def _is_setext(line: str, nxt: str) -> bool:
    return len(line) <= 80 and bool(_UNDERLINE.match(nxt.strip())) and len(nxt.strip()) >= 3


def _is_caps_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return (
        3 <= len(line) <= 60
        and len(letters) >= 3
        and all(c.isupper() for c in letters)
        and not line.endswith((".", ",", ";", ":"))
        and not _BULLET.match(line)
    )
