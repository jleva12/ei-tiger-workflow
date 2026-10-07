"""Spanner implementation of the document storage protocols.

The document record is the ingest fence. Every chunk batch and cleanup reads it
inside the same transaction as its writes, so superseded runs cannot resurrect
chunks after a newer run or a deletion.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from google.cloud.spanner_v1.data_types import JsonObject

from forge_embeddings.vector_store.spanner import (
    RECORDS,
    VECTORS,
    Database,
    Search,
    array,
    execute,
    field,
    get_record,
    intersect,
    payload,
    put_record,
    put_vectors,
    vector_row,
)
from forge_task_documents.models import (
    Chunk,
    DocumentRecord,
    HybridSearchRequest,
    IngestStatus,
    ReplaceStats,
    SearchHit,
    utcnow,
)

IDENTITY_FIELDS = ("filename", "media_type", "source_uri", "sha256", "size_bytes", "metadata")

# A chunk a run wrote that search doesn't see until the run is published has
# "live": false in its payload; every other chunk has no such key.
PUBLISHED = f"COALESCE({field('live')}, 'true') != 'false'"
UNPUBLISHED = f"{field('live')} = 'false'"
# Chunks published per transaction.
PUBLISH_BATCH = 500


class SpannerDocumentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, tenant_id: str, doc_id: str) -> DocumentRecord | None:
        rows = await self.db.query(
            f"SELECT Payload FROM {RECORDS} WHERE Namespace='documents' AND TenantId=@tenant AND Id=@id AND {field('status')}!='deleted'",
            {"tenant": tenant_id, "id": doc_id},
        )
        return DocumentRecord.model_validate(payload(rows[0][0])) if rows else None

    async def _start(self, record: DocumentRecord, *, begin: bool) -> int:
        def update(tx: Any) -> int:
            old = get_record(tx, "documents", record.tenant_id, record.doc_id)
            existing = DocumentRecord.model_validate(old) if old else None
            seq = existing.run_seq if existing else 0
            if existing is None or existing.status is IngestStatus.DELETED:
                base = record.model_copy(update={"chunk_count": 0, "run_seq": seq})
            else:
                base = existing.model_copy(update={f: getattr(record, f) for f in IDENTITY_FIELDS})
            seq += int(begin)
            base = base.model_copy(
                update={
                    "run_seq": seq,
                    "status": IngestStatus.PROCESSING if begin else IngestStatus.PENDING,
                    "error": None,
                    "updated_at": utcnow(),
                }
            )
            put_record(tx, "documents", record.tenant_id, record.doc_id, base)
            return seq

        return await self.db.transaction(update)

    async def register(self, record: DocumentRecord) -> None:
        await self._start(record, begin=False)

    async def begin_run(self, record: DocumentRecord) -> int:
        return await self._start(record, begin=True)

    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool:
        rec = await self.get(tenant_id, doc_id)
        return rec is not None and rec.run_seq == run_seq and rec.status is IngestStatus.PROCESSING

    async def _finish(self, tenant: str, doc: str, seq: int | None, *, success: bool, fields: dict[str, Any]) -> bool:
        def update(tx: Any) -> bool:
            row = get_record(tx, "documents", tenant, doc)
            if (
                not row
                or (seq is None and row["status"] != "pending")
                or (seq is not None and (row["run_seq"] != seq or row["status"] != "processing"))
            ):
                return False
            now = utcnow()
            changes = {**fields, "status": IngestStatus.READY if success else IngestStatus.FAILED, "updated_at": now}
            if success:
                changes.update(error=None, ingested_at=now)
            put_record(tx, "documents", tenant, doc, DocumentRecord.model_validate(row).model_copy(update=changes))
            return True

        return await self.db.transaction(update)

    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        return await self._finish(tenant_id, doc_id, run_seq, success=True, fields=fields)

    async def fail_run(self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str) -> bool:
        return await self._finish(tenant_id, doc_id, run_seq, success=False, fields={"error": error[:2000]})

    async def list(
        self, tenant_id: str, *, status: IngestStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        params: dict[str, Any] = {"tenant": tenant_id, "limit": max(0, limit), "offset": max(0, offset)}
        condition = f"Namespace='documents' AND TenantId=@tenant AND {field('status')}!='deleted'"
        if status is not None:
            params["status"] = status.value
            condition += f" AND {field('status')}=@status"
        rows = await self.db.query(
            f"SELECT Payload FROM {RECORDS} WHERE {condition} ORDER BY CAST({field('updated_at')} AS TIMESTAMP) DESC, Id LIMIT @limit OFFSET @offset",
            params,
        )
        return [DocumentRecord.model_validate(payload(r[0])) for r in rows]

    async def delete(self, tenant_id: str, doc_id: str) -> bool:
        def update(tx: Any) -> bool:
            row = get_record(tx, "documents", tenant_id, doc_id)
            if not row or row["status"] == "deleted":
                return False
            rec = DocumentRecord.model_validate(row).model_copy(
                update={
                    "status": IngestStatus.DELETED,
                    "run_seq": row["run_seq"] + 1,
                    "chunk_count": 0,
                    "updated_at": utcnow(),
                }
            )
            put_record(tx, "documents", tenant_id, doc_id, rec)
            return True

        return await self.db.transaction(update)


class SpannerChunkStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_embeddings_by_hash(self, tenant_id: str, content_hashes: Collection[str]) -> dict[str, list[float]]:
        return await self.db.embeddings("chunks", tenant_id, list(content_hashes))

    async def replace_document_chunks(
        self, tenant_id: str, doc_id: str, run_seq: int, chunks: Sequence[Chunk]
    ) -> ReplaceStats:
        if any(c.tenant_id != tenant_id or c.doc_id != doc_id for c in chunks):
            raise ValueError("chunks must belong to the requested tenant and document")
        rows = [vector_row(c, namespace="chunks", scope=doc_id, key=c.id, seq=run_seq) for c in chunks]

        def current(tx: Any) -> bool:
            r = get_record(tx, "documents", tenant_id, doc_id)
            return r is not None and r["run_seq"] == run_seq and r["status"] == "processing"

        written = 0
        for offset in range(0, len(rows), 50):
            batch = rows[offset : offset + 50]

            def write(tx: Any, batch: list[dict[str, Any]] = batch) -> int:
                if not current(tx):
                    return 0
                # A new chunk is unpublished until the run is; one already
                # there keeps whether it's published.
                p = {"tenant": tenant_id, "doc": doc_id, "ids": [r["Id"] for r in batch]}
                shown = {
                    r[0]: r[1] != "false"
                    for r in execute(
                        tx,
                        f"SELECT Id, {field('live')} FROM {VECTORS} WHERE Namespace='chunks' AND "
                        "TenantId=@tenant AND ScopeId=@doc AND Id IN UNNEST(@ids)",
                        p,
                    )
                }
                for row in batch:
                    if not shown.get(row["Id"], False):
                        row["Payload"] = JsonObject({**row["Payload"], "live": False})
                put_vectors(tx, batch)
                return len(batch)

            written += await self.db.transaction(write)
        return ReplaceStats(upserted=written, deleted=0)

    async def publish_run(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        key = {"tenant": tenant_id, "doc": doc_id, "seq": run_seq}
        where = "Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc"

        def publish(tx: Any) -> int:
            rows = execute(
                tx,
                f"SELECT Id, Payload FROM {VECTORS} WHERE {where} AND RunSeq=@seq AND {UNPUBLISHED} "
                f"LIMIT {PUBLISH_BATCH}",
                key,
            )
            if rows:
                tx.update(
                    VECTORS,
                    ["Namespace", "TenantId", "ScopeId", "Id", "Payload"],
                    [
                        [
                            "chunks",
                            tenant_id,
                            doc_id,
                            r[0],
                            JsonObject({k: v for k, v in payload(r[1]).items() if k != "live"}),
                        ]
                        for r in rows
                    ],
                )
            return len(rows)

        while await self.db.transaction(publish) == PUBLISH_BATCH:
            pass
        return int(await self.db.delete(VECTORS, f"{where} AND RunSeq<@seq", key))

    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        return await self.db.delete(
            VECTORS,
            f"Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc AND RunSeq=@seq AND {UNPUBLISHED}",
            {"tenant": tenant_id, "doc": doc_id, "seq": run_seq},
        )

    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int:
        return await self.db.delete(
            VECTORS, "Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc", {"tenant": tenant_id, "doc": doc_id}
        )

    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        rows = await self.db.query(
            f"SELECT Payload FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant AND Id IN UNNEST(@ids)",
            {"tenant": tenant_id, "ids": list(chunk_ids)},
        )
        found = {c.id: c for r in rows for c in [Chunk.model_validate(payload(r[0]))]}
        return [found[i] for i in chunk_ids if i in found]

    async def get_section(self, tenant_id: str, doc_id: str, section_key: str) -> list[Chunk]:
        rows = await self.db.query(
            f"SELECT Payload FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc AND {field('section_key')}=@section AND {PUBLISHED} ORDER BY CAST({field('ordinal')} AS INT64), Id",
            {"tenant": tenant_id, "doc": doc_id, "section": section_key},
        )
        return [Chunk.model_validate(payload(r[0])) for r in rows]


class SpannerSearchBackend:
    def __init__(self, db: Database, similarity: str) -> None:
        self.search = Search(db, "chunks", similarity)

    def options(self, req: HybridSearchRequest) -> dict[str, Any]:
        p: dict[str, Any] = {"tenants": list(req.tenant_ids)}
        conditions = ["TenantId IN UNNEST(@tenants)", PUBLISHED]
        for name, expr, values in (
            ("docs", "ScopeId", req.filters.doc_ids),
            ("sources", field("source_type"), req.filters.source_types),
            ("kinds", field("kind"), req.filters.kinds),
        ):
            if values:
                conditions.append(f"{expr} IN UNNEST(@{name})")
                p[name] = [str(v) for v in values]
        if req.filters.tags:
            conditions.append(intersect(array("metadata.tags"), "tags"))
            p["tags"] = req.filters.tags
        return dict(
            where=" AND ".join(conditions),
            params=p,
            text=req.text,
            vector=req.vector,
            model=req.embedding_model,
            identifiers=req.identifiers,
            limit=req.per_leg_limit,
        )

    async def text_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        rows = await self.search.leg("text", **self.options(request))
        return [SearchHit(chunk=Chunk.model_validate(r), score=s) for r, s in rows]

    async def vector_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        rows = await self.search.leg("vector", **self.options(request))
        return [SearchHit(chunk=Chunk.model_validate(r), score=s) for r, s in rows]

    async def hybrid_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        options = self.options(request)
        options["leg_limit"] = options.pop("limit")
        rows = await self.search.fused(
            weights={"text": request.text_weight, "vector": request.vector_weight}, limit=request.limit, **options
        )
        return [SearchHit(chunk=Chunk.model_validate(r), score=s, ranks=ranks) for r, s, ranks in rows]


class SpannerStorage:
    def __init__(self, database: Any = None, *, vector_similarity: str = "dotProduct") -> None:
        self.db = Database(database)
        self.documents = SpannerDocumentStore(self.db)
        self.chunks = SpannerChunkStore(self.db)
        self.search = SpannerSearchBackend(self.db, vector_similarity)

    async def ensure_schema(self, *, embedding_dimensions: int) -> None:
        await self.db.ensure_schema(embedding_dimensions=embedding_dimensions)

    async def close(self) -> None:
        await self.db.close()
