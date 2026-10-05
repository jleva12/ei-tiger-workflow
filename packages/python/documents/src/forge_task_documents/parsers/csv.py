"""CSV/TSV -> IR. Also the smallest example of adding a format: decode, emit one
TABLE element, done. The table chunking strategy does the rest."""

from __future__ import annotations

import csv
import io

from forge_task_documents.errors import ParseError
from forge_task_documents.models import (
    Element,
    ElementKind,
    ParsedDocument,
    SourceFile,
    SourceLocation,
    TableData,
)
from forge_task_documents.parsers.base import title_from_filename
from forge_task_documents.parsers.text import decode_bytes


class CsvParser:
    name = "csv"
    version = "1"
    extensions = frozenset({"csv", "tsv"})
    media_types = frozenset({"text/csv", "text/tab-separated-values"})

    def __init__(self, *, max_rows: int = 500_000) -> None:
        self.max_rows = max_rows

    def parse(self, source: SourceFile) -> ParsedDocument:
        text = decode_bytes(source.data)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if source.extension == "tsv" else csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        try:
            rows = [
                [c.strip() for c in row]
                for i, row in enumerate(reader)
                if i <= self.max_rows and any(c.strip() for c in row)
            ]
        except csv.Error as exc:
            raise ParseError(f"invalid CSV {source.filename!r}: {exc}") from exc
        title = title_from_filename(source.filename)
        elements: list[Element] = []
        if rows:
            width = max(len(r) for r in rows)
            headers = [h or f"Column {i + 1}" for i, h in enumerate(rows[0] + [""] * (width - len(rows[0])))]
            body = [r + [""] * (width - len(r)) for r in rows[1:]]
            elements.append(
                Element(
                    kind=ElementKind.TABLE,
                    section_path=[],
                    table=TableData(headers=headers, rows=body, caption=title, origin_row=1, origin_col=1),
                    location=SourceLocation(line_start=1, line_end=len(rows)),
                )
            )
        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="csv",
            title=title,
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
        )
