"""Query-time orchestration: embed query -> hybrid search -> rerank ->
relevance -> expand."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from forge_embeddings.identifiers import IdentifierExtractor
from forge_embeddings.lexical import analyze, cosine
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
    # The least relevance a hit needs, so a question the documents don't
    # answer finds nothing rather than the nearest of what's there (the
    # vector leg always has nearest neighbours). With a reranker and
    # min_rerank_score, its score; otherwise the hit's similarity to the
    # question (cosine of their vectors), and only for a hit nothing else
    # found: one sharing a word or an identifier with the question is kept.
    # None turns a floor off. 0.2 leaves out what text-embedding-3 vectors
    # call unrelated; tune it with the eval harness (forge_embeddings.eval).
    min_similarity: float | None = Field(default=0.2, ge=-1, le=1)
    min_rerank_score: float | None = None


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
            tenant_ids=list(dict.fromkeys(q.tenant_ids)),
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

        if hits:
            t_rel = time.perf_counter()
            await self._similarities(vector, hits)
            hits = self._relevant(q.text, hits)
            timings["relevance_ms"] = (time.perf_counter() - t_rel) * 1000

        if q.expand_sections and hits:
            t3 = time.perf_counter()
            await self._expand(hits)
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

    async def _similarities(self, vector: list[float], hits: list[SearchHit]) -> None:
        """Each hit's similarity to the question: the cosine of its vector and
        the question's, when it's embedded with the same model (another
        model's vector says nothing about it). Its vector is read by its
        content hash, in its tenant."""
        model = self._embedder.model_id
        wanted: dict[str, set[str]] = defaultdict(set)
        for hit in hits:
            if hit.chunk.embedding_model == model and hit.chunk.content_hash:
                wanted[hit.chunk.tenant_id].add(hit.chunk.content_hash)
        if not wanted:
            return
        tenants = list(wanted)
        found = await asyncio.gather(*(self._chunks.get_embeddings_by_hash(t, wanted[t]) for t in tenants))
        vectors = {(t, h): v for t, got in zip(tenants, found, strict=True) for h, v in got.items()}
        for hit in hits:
            known = vectors.get((hit.chunk.tenant_id, hit.chunk.content_hash))
            if known is not None and hit.chunk.embedding_model == model:
                hit.similarity = cosine(vector, known)

    def _relevant(self, question: str, hits: list[SearchHit]) -> list[SearchHit]:
        """The hits relevant enough to answer (``SearchConfig.min_similarity``,
        ``min_rerank_score``), in their order."""
        cfg = self.config
        identifiers = {i.lower() for i in self._ids.extract(question)}
        words = set(analyze(question))

        def keep(hit: SearchHit) -> bool:
            chunk = hit.chunk
            if identifiers & {i.lower() for i in chunk.identifiers}:
                return True  # an exact identifier is as relevant as it gets
            if hit.rerank_score is not None and cfg.min_rerank_score is not None:
                return hit.rerank_score >= cfg.min_rerank_score
            if cfg.min_similarity is None or hit.similarity is None or hit.similarity >= cfg.min_similarity:
                return True
            # Below the floor, only its meaning was near: keep it if its words are the question's too.
            return bool(words & set(analyze(f"{chunk.title} {chunk.section_text} {chunk.text}")))

        return [hit for hit in hits if keep(hit)]

    async def _expand(self, hits: list[SearchHit]) -> None:
        """Small-to-big: attach the full text of each hit's section (read in
        the hit's own tenant, as a search may span several)."""
        cache: dict[tuple[str, str, str], str] = {}
        for hit in hits:
            key = (hit.chunk.tenant_id, hit.chunk.doc_id, hit.chunk.section_key)
            if key not in cache:
                section = await self._chunks.get_section(*key)
                cache[key] = "\n\n".join(c.text for c in section)
            hit.expanded_text = cache[key]
