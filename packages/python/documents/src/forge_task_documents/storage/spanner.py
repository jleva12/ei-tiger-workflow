"""Spanner implementation of the document storage protocols.

The document record is the ingest fence. Every chunk batch and cleanup reads it
inside the same transaction as its writes, so superseded runs cannot resurrect
chunks after a newer run or a deletion.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Collection, Sequence
from typing import Any, cast

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
    storage_io,
    types,
    vector_row,
)
from forge_task_documents.errors import StaleRunError
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
log = logging.getLogger(__name__)

# A chunk a run wrote that search doesn't see until the run is published has
# "live": false in its payload; every other chunk has no such key.
PUBLISHED = f"COALESCE(JSON_VALUE({VECTORS}.Payload, '$.live'), 'true') != 'false'"
# The document pointer, committed with successful completion, is the sole
# publication switch. Legacy rows have no pointer and no live:false marker.
# Qualify outer columns: Payload/TenantId inside EXISTS otherwise bind to d.
VISIBLE = (
    f"EXISTS (SELECT 1 FROM {RECORDS} d WHERE d.Namespace='documents' "
    f"AND d.TenantId={VECTORS}.TenantId AND d.Id={VECTORS}.ScopeId "
    "AND JSON_VALUE(d.Payload, '$.status')!='deleted' AND ("
    "(CAST(JSON_VALUE(d.Payload, '$.published_run_seq') AS INT64)>0 AND "
    f"CAST(JSON_VALUE(d.Payload, '$.published_run_seq') AS INT64)={VECTORS}.RunSeq) OR "
    f"(JSON_VALUE(d.Payload, '$.published_run_seq') IS NULL AND {PUBLISHED})))"
)


def _document(value: Any) -> DocumentRecord:
    # Missing in pre-migration JSON means legacy publication. The new default
    # zero explicitly means unpublished, including recreated tombstones.
    return DocumentRecord.model_validate({"published_run_seq": None, **payload(value)})


class SpannerDocumentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, tenant_id: str, doc_id: str) -> DocumentRecord | None:
        rows = await self.db.query(
            f"SELECT Payload FROM {RECORDS} WHERE Namespace='documents' AND TenantId=@tenant AND Id=@id AND {field('status')}!='deleted'",
            {"tenant": tenant_id, "id": doc_id},
        )
        return _document(rows[0][0]) if rows else None

    async def _start(self, record: DocumentRecord, *, begin: bool, expected_run_seq: int | None = None) -> int:
        def update(tx: Any) -> int:
            old = get_record(tx, "documents", record.tenant_id, record.doc_id)
            existing = _document(old) if old else None
            seq = existing.run_seq if existing else 0
            if begin and (
                (existing is not None and existing.status is IngestStatus.DELETED)
                or (expected_run_seq is not None and expected_run_seq != seq)
            ):
                raise StaleRunError("The document was deleted or replaced before this run started.")
            if existing is None or existing.status is IngestStatus.DELETED:
                base = record.model_copy(update={"chunk_count": 0, "run_seq": seq, "published_run_seq": 0})
            else:
                base = existing.model_copy(update={f: getattr(record, f) for f in IDENTITY_FIELDS})
            seq += int(begin or existing is not None)
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

    async def begin_run(self, record: DocumentRecord, *, expected_run_seq: int | None = None) -> int:
        return await self._start(record, begin=True, expected_run_seq=expected_run_seq)

    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool:
        rec = await self.get(tenant_id, doc_id)
        return rec is not None and rec.run_seq == run_seq and rec.status is IngestStatus.PROCESSING

    async def _finish(
        self,
        tenant: str,
        doc: str,
        seq: int | None,
        *,
        success: bool,
        fields: dict[str, Any],
        expected_run_seq: int | None = None,
    ) -> bool:
        def update(tx: Any) -> bool:
            row = get_record(tx, "documents", tenant, doc)
            if (
                not row
                or (seq is None and row["status"] != "pending")
                or (seq is None and expected_run_seq is not None and row["run_seq"] != expected_run_seq)
                or (seq is not None and (row["run_seq"] != seq or row["status"] != "processing"))
            ):
                return False
            now = utcnow()
            changes = {**fields, "status": IngestStatus.READY if success else IngestStatus.FAILED, "updated_at": now}
            if success:
                changes.update(error=None, ingested_at=now, published_run_seq=seq)
            put_record(tx, "documents", tenant, doc, _document(row).model_copy(update=changes))
            return True

        return await self.db.transaction(update)

    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        return await self._finish(tenant_id, doc_id, run_seq, success=True, fields=fields)

    async def fail_run(
        self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str, expected_run_seq: int | None = None
    ) -> bool:
        return await self._finish(
            tenant_id, doc_id, run_seq, success=False, fields={"error": error[:2000]}, expected_run_seq=expected_run_seq
        )

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
        return [_document(r[0]) for r in rows]

    async def delete(self, tenant_id: str, doc_id: str) -> bool:
        def update(tx: Any) -> bool:
            row = get_record(tx, "documents", tenant_id, doc_id)
            if row and row["status"] == "deleted":
                return False
            existing = (
                _document(row)
                if row
                else DocumentRecord(tenant_id=tenant_id, doc_id=doc_id, filename=doc_id, sha256="", size_bytes=0)
            )
            rec = existing.model_copy(
                update={
                    "status": IngestStatus.DELETED,
                    "run_seq": existing.run_seq + 1,
                    "chunk_count": 0,
                    "published_run_seq": 0,
                    "updated_at": utcnow(),
                }
            )
            put_record(tx, "documents", tenant_id, doc_id, rec)
            return row is not None

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
        rows = []
        for chunk in chunks:
            # Public IDs remain stable in Payload; storage identities never
            # overwrite a previously committed generation (even a same-ID edit).
            key = hashlib.sha256(f"{run_seq}\0{chunk.id}".encode()).hexdigest()
            row = vector_row(chunk, namespace="chunks", scope=doc_id, key=f"generation:{key}", seq=run_seq)
            row["Payload"] = JsonObject({**row["Payload"], "live": False})
            rows.append(row)

        def current(tx: Any) -> bool:
            r = get_record(tx, "documents", tenant_id, doc_id)
            return r is not None and r["run_seq"] == run_seq and r["status"] == "processing"

        written = 0
        for offset in range(0, len(rows), 50):
            batch = rows[offset : offset + 50]

            def write(tx: Any, batch: list[dict[str, Any]] = batch) -> int:
                if not current(tx):
                    return 0
                put_vectors(tx, batch)
                return len(batch)

            written += await self.db.transaction(write)
        return ReplaceStats(upserted=written, deleted=0)

    async def publish_run(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        where = "Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc"

        def publish(tx: Any) -> int:
            record = get_record(tx, "documents", tenant_id, doc_id)
            selected = record.get("published_run_seq") if record else None
            if selected is None or selected <= 0 or record["status"] == "deleted":
                return 0
            # A late cleanup uses the authoritative pointer, never its stale
            # caller's seq; publishing is already complete and atomic.
            params = {"tenant": tenant_id, "doc": doc_id, "seq": selected}
            return int(
                tx.execute_update(
                    f"DELETE FROM {VECTORS} WHERE {where} AND RunSeq<@seq", params=params, param_types=types(params)
                )
            )

        try:
            return await self.db.transaction(publish)
        except Exception:
            log.warning("Cleaning old document generations failed after publication", exc_info=True)
            return 0

    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        def remove(tx: Any) -> int:
            record = get_record(tx, "documents", tenant_id, doc_id)
            selected = record.get("published_run_seq") if record else None
            if selected == run_seq:
                return 0
            params = {"tenant": tenant_id, "doc": doc_id, "seq": run_seq}
            # Preserve pre-migration published rows without a pointer.
            legacy = f" AND {field('live')}='false'" if selected is None else ""
            return int(
                tx.execute_update(
                    f"DELETE FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant "
                    f"AND ScopeId=@doc AND RunSeq=@seq{legacy}",
                    params=params,
                    param_types=types(params),
                )
            )

        return await self.db.transaction(remove)

    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int:
        def remove(tx: Any) -> int:
            record = get_record(tx, "documents", tenant_id, doc_id)
            if not record or record["status"] != "deleted":
                return 0
            params = {"tenant": tenant_id, "doc": doc_id, "seq": record["run_seq"]}
            return int(
                tx.execute_update(
                    f"DELETE FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant "
                    "AND ScopeId=@doc AND RunSeq<=@seq",
                    params=params,
                    param_types=types(params),
                )
            )

        return await self.db.transaction(remove)

    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        rows = await self.db.query(
            f"SELECT Payload FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant "
            f"AND {field('id')} IN UNNEST(@ids) AND {VISIBLE}",
            {"tenant": tenant_id, "ids": list(chunk_ids)},
        )
        found = {c.id: c for r in rows for c in [Chunk.model_validate(payload(r[0]))]}
        return [found[i] for i in chunk_ids if i in found]

    async def get_section(
        self, tenant_id: str, doc_id: str, section_key: str, *, generation: int | None = None
    ) -> list[Chunk]:
        params: dict[str, Any] = {"tenant": tenant_id, "doc": doc_id, "section": section_key}
        version = ""
        if generation is not None:
            params["generation"] = generation
            version = " AND RunSeq=@generation"
        rows = await self.db.query(
            f"SELECT Payload FROM {VECTORS} WHERE Namespace='chunks' AND TenantId=@tenant AND ScopeId=@doc AND {field('section_key')}=@section AND {VISIBLE}{version} ORDER BY CAST({field('ordinal')} AS INT64), Id",
            params,
        )
        return [Chunk.model_validate(payload(r[0])) for r in rows]


class SpannerSearchBackend:
    def __init__(self, db: Database, similarity: str) -> None:
        self.db, self.similarity = db, similarity
        self.search = Search(db, "chunks", similarity)

    def options(self, req: HybridSearchRequest) -> dict[str, Any]:
        p: dict[str, Any] = {"tenants": list(req.tenant_ids)}
        conditions = ["TenantId IN UNNEST(@tenants)", VISIBLE]
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
        return [SearchHit(chunk=Chunk.model_validate(r), score=s, generation=r.get("_generation")) for r, s in rows]

    async def vector_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        rows = await self.search.leg("vector", **self.options(request))
        return [SearchHit(chunk=Chunk.model_validate(r), score=s, generation=r.get("_generation")) for r, s in rows]

    async def hybrid_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        options = self.options(request)
        options["leg_limit"] = options.pop("limit")
        # Both legs must read the same pointer and vector snapshot. Otherwise a
        # publication between the legs could fuse chunks from two generations.
        checkout = self.db.db.snapshot(multi_use=True)
        opening = asyncio.create_task(storage_io(checkout.__enter__))
        try:
            reader = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Session checkout also runs in a thread, which cancellation does
            # not stop. Return a session that finishes opening after cancellation.
            try:
                await opening
            except Exception:
                pass
            else:
                await storage_io(checkout.__exit__, None, None, None)
            raise
        queries = _SnapshotQueries(reader)
        try:
            search = Search(cast(Database, queries), "chunks", self.similarity)
            rows = await search.fused(
                weights={"text": request.text_weight, "vector": request.vector_weight}, limit=request.limit, **options
            )
        finally:
            # gather() can raise while another leg is still reading. Wait for
            # that SDK call before returning its session to Spanner's pool.
            await queries.close()
            await storage_io(checkout.__exit__, None, None, None)
        return [
            SearchHit(chunk=Chunk.model_validate(r), score=s, ranks=ranks, generation=r.get("_generation"))
            for r, s, ranks in rows
        ]


class _SnapshotQueries:
    """Serialize SDK reads while all hybrid legs share one read-only snapshot."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.lock = asyncio.Lock()
        self.closed = False

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        async with self.lock:
            if self.closed:
                raise RuntimeError("The search snapshot is closed.")
            read = asyncio.create_task(storage_io(execute, self.reader, sql, params or {}))
            try:
                return await asyncio.shield(read)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop the SDK call. Keep the
                # lock until it really ends, so cleanup cannot reuse its session.
                try:
                    await read
                except Exception:
                    pass
                raise

    async def close(self) -> None:
        self.closed = True
        async with self.lock:
            pass


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
