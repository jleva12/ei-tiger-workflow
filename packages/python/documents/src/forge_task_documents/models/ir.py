"""Intermediate representation (IR) every parser emits.

Parsers translate a file format into a flat, ordered list of typed ``Element``s.
Structure that matters for retrieval (section hierarchy, table rows/headers,
diagram nodes/edges) is kept as data, never flattened to markdown, so the
chunker can treat a table from Word, Excel or Markdown exactly the same way.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ElementKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    CODE = "code"
    TABLE = "table"
    GRAPH = "graph"


class SourceLocation(BaseModel):
    """Where an element (or chunk) came from, for citations and debugging."""

    model_config = ConfigDict(frozen=True)

    page: int | None = None
    page_name: str | None = None
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    shape_ids: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return self == SourceLocation()


class TableData(BaseModel):
    headers: list[str]
    rows: list[list[str]]
    caption: str | None = None
    # 1-based spreadsheet origin of the header row, when the source has cells.
    origin_row: int | None = None
    origin_col: int | None = None


class GraphNode(BaseModel):
    id: str
    label: str
    shape_type: str | None = None
    group: str | None = None  # container / swimlane / group label
    properties: dict[str, str] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str | None = None
    directed: bool = True


class GraphData(BaseModel):
    name: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class Element(BaseModel):
    kind: ElementKind
    text: str = ""
    section_path: list[str] = Field(default_factory=list)
    level: int | None = None  # heading level or list depth
    language: str | None = None  # code blocks
    table: TableData | None = None
    graph: GraphData | None = None
    location: SourceLocation = Field(default_factory=SourceLocation)
    attrs: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    tenant_id: str
    doc_id: str
    source_type: str  # "docx", "xlsx", "markdown", ...
    title: str
    elements: list[Element]
    parser_name: str
    parser_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def plain_text(self, max_chars: int | None = None) -> str:
        """Best-effort linear text of the whole document (used for LLM context)."""
        parts: list[str] = []
        size = 0
        for el in self.elements:
            if el.kind is ElementKind.TABLE and el.table:
                piece = " | ".join(el.table.headers)
            elif el.kind is ElementKind.GRAPH and el.graph:
                piece = "; ".join(n.label for n in el.graph.nodes)
            else:
                piece = el.text
            if not piece:
                continue
            parts.append(piece)
            size += len(piece) + 1
            if max_chars is not None and size >= max_chars:
                break
        text = "\n".join(parts)
        return text[:max_chars] if max_chars is not None else text
