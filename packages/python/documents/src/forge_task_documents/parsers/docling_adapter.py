"""Optional: use Docling for parsing, while keeping chunking in our engine.

Docling is heavier (torch) but models page layout and OCRs scanned PDFs. This
adapter converts a ``DoclingDocument`` into the IR so its chunks follow exactly
the same rules as every other format. Enable it per format with
``HYBRID_DOCUMENTS__DOCX_PARSER=docling`` or ``HYBRID_DOCUMENTS__PDF_PARSER=docling``.

``docling_to_ir`` only needs ``docling-core`` and is unit-tested; the parser
class additionally needs the full ``docling`` package at runtime.
"""

from __future__ import annotations

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
from forge_task_documents.parsers.base import SectionTracker, clean_text, title_from_filename


def docling_to_ir(
    dl_doc: Any, *, source: SourceFile, source_type: str, parser_name: str, parser_version: str
) -> ParsedDocument:
    from docling_core.types.doc import (
        CodeItem,
        ListItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )

    title = ""
    tracker = SectionTracker()
    elements: list[Element] = []
    for item, _depth in dl_doc.iterate_items():
        loc = _location(item)
        # subclasses before TextItem: order matters
        if isinstance(item, TitleItem):
            title = title or clean_text(item.text)
        elif isinstance(item, SectionHeaderItem):
            text = clean_text(item.text)
            if text:
                level = int(getattr(item, "level", 1) or 1)
                path = tracker.push(level, text)
                elements.append(
                    Element(kind=ElementKind.HEADING, text=text, level=level, section_path=path, location=loc)
                )
        elif isinstance(item, TableItem):
            table = _table(item, dl_doc)
            if table is not None:
                elements.append(Element(kind=ElementKind.TABLE, table=table, section_path=tracker.path, location=loc))
        elif isinstance(item, CodeItem):
            if item.text.strip():
                lang = getattr(getattr(item, "code_language", None), "value", None)
                elements.append(
                    Element(
                        kind=ElementKind.CODE,
                        text=item.text,
                        language=None if lang in (None, "unknown") else str(lang).lower(),
                        section_path=tracker.path,
                        location=loc,
                    )
                )
        elif isinstance(item, ListItem):
            text = clean_text(item.text)
            if text:
                elements.append(
                    Element(kind=ElementKind.LIST_ITEM, text=text, level=0, section_path=tracker.path, location=loc)
                )
        elif isinstance(item, TextItem):
            text = clean_text(item.text)
            if text:
                elements.append(Element(kind=ElementKind.PARAGRAPH, text=text, section_path=tracker.path, location=loc))

    return ParsedDocument(
        tenant_id=source.tenant_id,
        doc_id=source.doc_id,
        source_type=source_type,
        title=title or title_from_filename(source.filename),
        elements=elements,
        parser_name=parser_name,
        parser_version=parser_version,
    )


def _location(item: Any) -> SourceLocation:
    prov = getattr(item, "prov", None) or []
    if prov:
        page = getattr(prov[0], "page_no", None)
        if page:
            return SourceLocation(page=int(page))
    return SourceLocation()


def _table(item: Any, dl_doc: Any) -> TableData | None:
    df = item.export_to_dataframe(doc=dl_doc)
    if df.empty and len(df.columns) == 0:
        return None
    headers = [clean_text(str(c)) or f"Column {i + 1}" for i, c in enumerate(df.columns)]
    rows = [[clean_text(str(v)) if v is not None else "" for v in row] for row in df.astype(object).values.tolist()]
    caption = None
    try:
        caption = clean_text(item.caption_text(dl_doc)) or None
    except Exception:
        pass
    return TableData(headers=headers, rows=rows, caption=caption)


_MEDIA_TYPES: dict[str, tuple[str, ...]] = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    "pdf": ("application/pdf", "application/x-pdf"),
}


class DoclingParser:
    """Docling-backed parser. Register it to override the default docx or pdf parser."""

    name = "docling"
    version = "1"

    def __init__(self, *, extensions: frozenset[str] = frozenset({"docx"}), artifacts_path: str | None = None) -> None:
        self.extensions = extensions
        self.media_types = frozenset(mt for ext in extensions for mt in _MEDIA_TYPES.get(ext, ()))
        self._artifacts_path = artifacts_path
        self._converter: Any = None

    def _get_converter(self) -> Any:
        if self._converter is None:
            try:
                from docling.document_converter import DocumentConverter
            except ImportError as exc:
                raise ParseError("docling is not installed (pip install docling)") from exc
            self._converter = DocumentConverter()
        return self._converter

    def parse(self, source: SourceFile) -> ParsedDocument:
        from docling.datamodel.base_models import DocumentStream

        try:
            result = self._get_converter().convert(DocumentStream(name=source.filename, stream=source.open()))
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"docling failed on {source.filename!r}: {exc}") from exc
        return docling_to_ir(
            result.document,
            source=source,
            source_type=source.extension or "docx",
            parser_name=self.name,
            parser_version=self.version,
        )
