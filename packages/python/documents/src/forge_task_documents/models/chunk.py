from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from forge_task_documents.models.ir import SourceLocation


class ChunkKind(StrEnum):
    PROSE = "prose"
    CODE = "code"
    TABLE = "table"
    TABLE_SUMMARY = "table_summary"
    GRAPH = "graph"
    GRAPH_SUMMARY = "graph_summary"


class Chunk(BaseModel):
    """The unit that gets embedded, BM25-indexed and returned by search."""

    id: str
    tenant_id: str
    doc_id: str
    source_type: str
    kind: ChunkKind
    ordinal: int
    title: str
    section_path: list[str]
    section_key: str  # groups sibling chunks of one section (small-to-big expansion)
    text: str  # raw chunk text: BM25 + display
    embed_text: str = ""  # what the embedder sees (breadcrumb + context + text)
    context: str | None = None  # optional LLM-written situating sentence
    identifiers: list[str] = Field(default_factory=list)  # exact-match tokens
    token_count: int
    content_hash: str = ""  # hash(embed_text, model): embedding reuse key
    location: SourceLocation = Field(default_factory=SourceLocation)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunker_version: str
    embedding: list[float] | None = Field(default=None, repr=False)
    embedding_model: str | None = None

    @property
    def section_text(self) -> str:
        return " > ".join(self.section_path)
