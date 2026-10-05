from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge_task_documents.models.chunk import Chunk, ChunkKind


class SearchFilters(BaseModel):
    doc_ids: list[str] | None = None
    source_types: list[str] | None = None
    kinds: list[ChunkKind] | None = None
    tags: list[str] | None = None  # matches chunk.metadata.tags


class HybridSearchRequest(BaseModel):
    """What the service hands a SearchBackend. Everything is already computed."""

    tenant_id: str
    text: str
    vector: list[float]
    embedding_model: str | None = None
    identifiers: list[str] = Field(default_factory=list)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = 50  # fused results to return
    per_leg_limit: int = 50  # results each leg (vector / text) feeds into fusion
    num_candidates: int = 500  # HNSW exploration for the vector leg
    vector_weight: float = 1.0
    text_weight: float = 1.0
    score_details: bool = False


class SearchHit(BaseModel):
    chunk: Chunk
    score: float  # fused (RRF) score
    rerank_score: float | None = None
    ranks: dict[str, int] = Field(default_factory=dict)  # leg name -> 1-based rank
    details: dict[str, Any] | None = None
    expanded_text: str | None = None  # full section text when expansion is on


class SearchQuery(BaseModel):
    """Public search input (what your API receives)."""

    tenant_id: str
    text: str = Field(min_length=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    top_k: int = Field(default=8, ge=1, le=100)
    candidate_k: int | None = None  # defaults to SearchConfig.candidate_k
    rerank: bool = True
    expand_sections: bool = False
    vector_weight: float | None = None
    text_weight: float | None = None
    debug: bool = False


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    timings_ms: dict[str, float] = Field(default_factory=dict)
