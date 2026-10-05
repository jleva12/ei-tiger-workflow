from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from forge_task_documents.models import ChunkKind, Element, SourceLocation

if TYPE_CHECKING:
    from forge_embeddings.protocols import Tokenizer


class BlockKind(StrEnum):
    PROSE = "prose"
    CODE = "code"
    TABLE = "table"
    GRAPH = "graph"


@dataclass(slots=True)
class Block:
    """Contiguous elements of one block kind within one section."""

    kind: BlockKind
    section_path: list[str]
    elements: list[Element]


@dataclass(slots=True)
class ChunkDraft:
    """Strategy output. The engine turns drafts into Chunks (ids, ordinals, hashes)."""

    kind: ChunkKind
    text: str
    section_path: list[str]
    location: SourceLocation = field(default_factory=SourceLocation)
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int | None = None


class ChunkingConfig(BaseModel):
    max_tokens: int = Field(default=400, ge=32)
    min_tokens: int = Field(default=80, ge=0)
    # Overlap is only used when a single paragraph/code block must be split.
    overlap_tokens: int = Field(default=40, ge=0)
    merge_peers: bool = True
    table_max_rows_per_chunk: int = Field(default=25, ge=1)
    table_summary_min_rows: int = Field(default=30, ge=1)
    table_summary_sample_rows: int = Field(default=3, ge=0)
    table_distinct_values_max: int = Field(default=12, ge=0)


@dataclass(slots=True)
class ChunkContext:
    tokenizer: Tokenizer
    config: ChunkingConfig
    doc_title: str
