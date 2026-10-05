"""In-memory StorageBackend: the reference implementation of the storage
protocols. Used by the test-suite and for local dev without a database.

Search is brute force (BM25 over fielded text + cosine), fused with the same
RRF formula Atlas uses. Fine for thousands of chunks; not for production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from typing import Any

from forge_embeddings.fusion import rrf_fuse
from forge_embeddings.lexical import analyze, cosine, fielded_bm25
from forge_task_documents.models import (
    Chunk,
    DocumentRecord,
    HybridSearchRequest,
    IngestStatus,
    ReplaceStats,
    SearchHit,
    utcnow,
)

# field -> boost; mirrors the Atlas $search compound query
FIELD_BOOSTS: dict[str, float] = {"text": 1.0, "section_text": 2.0, "title": 1.5, "context": 0.5}
IDENTIFIER_BOOST = 5.0


# identity of the file a run is working on (set by begin_run / complete_run)
IDENTITY_FIELDS = ("filename", "media_type", "source_uri", "sha256", "size_bytes", "metadata")


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], DocumentRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant_id: str, doc_id: str) -> DocumentRecord | None:
        rec = self._docs.get((tenant_id, doc_id))
        if rec is None or rec.status is IngestStatus.DELETED:
            return None
        return rec.model_copy(deep=True)

    def _merge(self, record: DocumentRecord, **update: Any) -> DocumentRecord:
        existing = self._docs.get((record.tenant_id, record.doc_id))
        if existing is None:
            base = record.model_copy(update={"chunk_count": 0, "run_seq": 0})
        elif existing.status is IngestStatus.DELETED:  # re-created: keep only the fence
            base = record.model_copy(update={"chunk_count": 0, "run_seq": existing.run_seq})
        else:
            base = existing.model_copy(update={f: getattr(record, f) for f in IDENTITY_FIELDS})
        return base.model_copy(update={"error": None, "updated_at": utcnow(), **update})

    async def register(self, record: DocumentRecord) -> None:
        async with self._lock:
            self._docs[(record.tenant_id, record.doc_id)] = self._merge(record, status=IngestStatus.PENDING)

    async def begin_run(self, record: DocumentRecord) -> int:
        async with self._lock:
            existing = self._docs.get((record.tenant_id, record.doc_id))
            seq = (existing.run_seq if existing else 0) + 1
            self._docs[(record.tenant_id, record.doc_id)] = self._merge(
                record, status=IngestStatus.PROCESSING, run_seq=seq
            )
            return seq

    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool:
        rec = self._docs.get((tenant_id, doc_id))
        return rec is not None and rec.run_seq == run_seq and rec.status is IngestStatus.PROCESSING

    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        async with self._lock:
            if not await self.is_current_run(tenant_id, doc_id, run_seq):
                return False
            rec = self._docs[(tenant_id, doc_id)]
            now = utcnow()
            self._docs[(tenant_id, doc_id)] = rec.model_copy(
                update={**fields, "status": IngestStatus.READY, "error": None, "updated_at": now, "ingested_at": now}
            )
            return True

    async def fail_run(self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str) -> bool:
        async with self._lock:
            rec = self._docs.get((tenant_id, doc_id))
            if rec is None:
                return False
            if run_seq is None:
                if rec.status is not IngestStatus.PENDING:
                    return False
            elif not await self.is_current_run(tenant_id, doc_id, run_seq):
                return False
            self._docs[(tenant_id, doc_id)] = rec.model_copy(
                update={"status": IngestStatus.FAILED, "error": error[:2000], "updated_at": utcnow()}
            )
            return True

    async def list(
        self, tenant_id: str, *, status: IngestStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        rows = [
            r
            for (t, _), r in self._docs.items()
            if t == tenant_id and r.status is not IngestStatus.DELETED and (status is None or r.status == status)
        ]
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        return [r.model_copy(deep=True) for r in rows[offset : offset + limit]]

    async def delete(self, tenant_id: str, doc_id: str) -> bool:
        async with self._lock:
            rec = self._docs.get((tenant_id, doc_id))
            if rec is None or rec.status is IngestStatus.DELETED:
                return False
            self._docs[(tenant_id, doc_id)] = rec.model_copy(
                update={"status": IngestStatus.DELETED, "run_seq": rec.run_seq + 1, "updated_at": utcnow()}
            )
            return True


class InMemoryChunkStore:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[Chunk, int]] = {}  # chunk id -> (chunk, run_seq)

    async def get_embeddings_by_hash(self, tenant_id: str, content_hashes: Collection[str]) -> dict[str, list[float]]:
        wanted = set(content_hashes)
        out: dict[str, list[float]] = {}
        for chunk, _ in self.rows.values():
            if chunk.tenant_id == tenant_id and chunk.content_hash in wanted and chunk.embedding is not None:
                out[chunk.content_hash] = list(chunk.embedding)
        return out

    async def replace_document_chunks(
        self, tenant_id: str, doc_id: str, run_seq: int, chunks: Sequence[Chunk]
    ) -> ReplaceStats:
        written = 0
        for c in chunks:
            current = self.rows.get(c.id)
            if current is not None and current[1] > run_seq:
                continue  # a newer run owns this chunk
            self.rows[c.id] = (c.model_copy(deep=True), run_seq)
            written += 1
        stale = [
            cid
            for cid, (c, seq) in self.rows.items()
            if c.tenant_id == tenant_id and c.doc_id == doc_id and seq < run_seq
        ]
        for cid in stale:
            del self.rows[cid]
        return ReplaceStats(upserted=written, deleted=len(stale))

    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        ids = [
            cid
            for cid, (c, seq) in self.rows.items()
            if c.tenant_id == tenant_id and c.doc_id == doc_id and seq == run_seq
        ]
        for cid in ids:
            del self.rows[cid]
        return len(ids)

    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int:
        ids = [cid for cid, (c, _) in self.rows.items() if c.tenant_id == tenant_id and c.doc_id == doc_id]
        for cid in ids:
            del self.rows[cid]
        return len(ids)

    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]:
        out = []
        for cid in chunk_ids:
            row = self.rows.get(cid)
            if row and row[0].tenant_id == tenant_id:
                out.append(row[0].model_copy(update={"embedding": None}))
        return out

    async def get_section(self, tenant_id: str, doc_id: str, section_key: str) -> list[Chunk]:
        rows = [
            c.model_copy(update={"embedding": None})
            for c, _ in self.rows.values()
            if c.tenant_id == tenant_id and c.doc_id == doc_id and c.section_key == section_key
        ]
        return sorted(rows, key=lambda c: c.ordinal)


class InMemorySearchBackend:
    def __init__(self, chunks: InMemoryChunkStore, *, k1: float = 1.2, b: float = 0.75) -> None:
        self._chunks = chunks
        self.k1, self.b = k1, b

    def _candidates(self, req: HybridSearchRequest) -> list[Chunk]:
        f = req.filters
        out = []
        for c, _ in self._chunks.rows.values():
            if c.tenant_id != req.tenant_id:
                continue
            if f.doc_ids and c.doc_id not in f.doc_ids:
                continue
            if f.source_types and c.source_type not in f.source_types:
                continue
            if f.kinds and c.kind not in f.kinds:
                continue
            if f.tags and not set(f.tags) & set(c.metadata.get("tags") or []):
                continue
            out.append(c)
        return out

    @staticmethod
    def _hit(chunk: Chunk, score: float, ranks: dict[str, int] | None = None) -> SearchHit:
        return SearchHit(chunk=chunk.model_copy(update={"embedding": None}), score=score, ranks=ranks or {})

    def _bm25(self, docs: list[Chunk], req: HybridSearchRequest) -> list[tuple[Chunk, float]]:
        idents = {i.lower() for i in req.identifiers}
        if not docs or (not analyze(req.text) and not idents):
            return []
        fields = [
            {"text": d.text, "section_text": d.section_text, "title": d.title, "context": d.context or ""} for d in docs
        ]
        scores = fielded_bm25(req.text, fields, FIELD_BOOSTS, k1=self.k1, b=self.b)
        if idents:
            for i, d in enumerate(docs):
                scores[i] += IDENTIFIER_BOOST * len(idents & {x.lower() for x in d.identifiers})
        ranked = sorted(((d, s) for d, s in zip(docs, scores, strict=True) if s > 0), key=lambda x: -x[1])
        return ranked[: req.per_leg_limit]

    def _knn(self, docs: list[Chunk], req: HybridSearchRequest) -> list[tuple[Chunk, float]]:
        scored = [(d, cosine(req.vector, d.embedding)) for d in docs if d.embedding]
        scored.sort(key=lambda x: -x[1])
        return scored[: req.per_leg_limit]

    async def text_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        return [self._hit(c, s) for c, s in self._bm25(self._candidates(request), request)]

    async def vector_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        return [self._hit(c, s) for c, s in self._knn(self._candidates(request), request)]

    async def hybrid_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        docs = self._candidates(request)
        text = self._bm25(docs, request)
        vec = self._knn(docs, request)
        by_id = {c.id: c for c, _ in text + vec}
        fused = rrf_fuse(
            {"vector": [c.id for c, _ in vec], "text": [c.id for c, _ in text]},
            {"vector": request.vector_weight, "text": request.text_weight},
        )
        return [self._hit(by_id[i], s, r) for i, s, r in fused[: request.limit]]


class InMemoryStorage:
    def __init__(self) -> None:
        self.documents = InMemoryDocumentStore()
        self.chunks = InMemoryChunkStore()
        self.search = InMemorySearchBackend(self.chunks)

    async def ensure_schema(self, *, embedding_dimensions: int) -> None:
        return None

    async def close(self) -> None:
        return None
