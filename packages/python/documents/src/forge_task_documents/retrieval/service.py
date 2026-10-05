"""Query-time orchestration: embed query -> hybrid search -> rerank -> expand."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from forge_embeddings.identifiers import IdentifierExtractor
from forge_task_documents.models import HybridSearchRequest, SearchHit, SearchQuery, SearchResponse

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder, Reranker
    from forge_task_documents.protocols import ChunkStore, SearchBackend


class SearchConfig(BaseModel):
    candidate_k: int = Field(default=50, ge=1)  # fused candidates handed to the reranker
    per_leg_limit: int = Field(default=50, ge=1)  # results each leg contributes to fusion
    num_candidates_factor: int = Field(default=10, ge=1)  # HNSW numCandidates = factor * per_leg_limit
    vector_weight: float = 1.0
    text_weight: float = 1.0
    rerank_input_chars: int = 4000


class HybridSearchService:
    def __init__(
        self,
        *,
        search: SearchBackend,
        chunks: ChunkStore,
        embedder: Embedder,
        reranker: Reranker | None = None,
        identifier_extractor: IdentifierExtractor | None = None,
        config: SearchConfig | None = None,
    ) -> None:
        self._search = search
        self._chunks = chunks
        self._embedder = embedder
        self._reranker = reranker
        self._ids = identifier_extractor or IdentifierExtractor()
        self.config = config or SearchConfig()

    def build_request(self, q: SearchQuery, vector: list[float]) -> HybridSearchRequest:
        cfg = self.config
        candidate_k = q.candidate_k or cfg.candidate_k
        return HybridSearchRequest(
            tenant_id=q.tenant_id,
            text=q.text,
            vector=vector,
            embedding_model=self._embedder.model_id,
            identifiers=self._ids.extract(q.text),
            filters=q.filters,
            limit=max(candidate_k, q.top_k),
            per_leg_limit=max(cfg.per_leg_limit, q.top_k),
            num_candidates=cfg.num_candidates_factor * max(cfg.per_leg_limit, q.top_k),
            vector_weight=q.vector_weight if q.vector_weight is not None else cfg.vector_weight,
            text_weight=q.text_weight if q.text_weight is not None else cfg.text_weight,
            score_details=q.debug,
        )

    async def search(self, q: SearchQuery) -> SearchResponse:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        vector = await self._embedder.embed_query(q.text)
        timings["embed_ms"] = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        hits = await self._search.hybrid_search(self.build_request(q, vector))
        timings["search_ms"] = (time.perf_counter() - t1) * 1000

        if q.rerank and self._reranker is not None and hits:
            t2 = time.perf_counter()
            hits = await self._rerank(q, hits)
            timings["rerank_ms"] = (time.perf_counter() - t2) * 1000
        hits = hits[: q.top_k]

        if q.expand_sections and hits:
            t3 = time.perf_counter()
            await self._expand(q.tenant_id, hits)
            timings["expand_ms"] = (time.perf_counter() - t3) * 1000
        timings["total_ms"] = (time.perf_counter() - t0) * 1000
        return SearchResponse(hits=hits, timings_ms=timings)

    async def _rerank(self, q: SearchQuery, hits: list[SearchHit]) -> list[SearchHit]:
        assert self._reranker is not None
        docs = [
            (f"{h.chunk.title} > {h.chunk.section_text}\n" if h.chunk.section_path else f"{h.chunk.title}\n")
            + h.chunk.text[: self.config.rerank_input_chars]
            for h in hits
        ]
        order = await self._reranker.rerank(q.text, docs, top_k=min(q.top_k, len(hits)))
        out = []
        for idx, score in order:
            hit = hits[idx]
            hit.rerank_score = score
            out.append(hit)
        return out

    async def _expand(self, tenant_id: str, hits: list[SearchHit]) -> None:
        """Small-to-big: attach the full text of each hit's section."""
        cache: dict[tuple[str, str], str] = {}
        for hit in hits:
            key = (hit.chunk.doc_id, hit.chunk.section_key)
            if key not in cache:
                section = await self._chunks.get_section(tenant_id, *key)
                cache[key] = "\n\n".join(c.text for c in section)
            hit.expanded_text = cache[key]
