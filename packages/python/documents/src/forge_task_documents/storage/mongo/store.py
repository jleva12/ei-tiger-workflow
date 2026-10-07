"""MongoDB Atlas implementation of the storage protocols (pymongo async API).

Uses ``pymongo.AsyncMongoClient`` directly rather than an ODM: chunk writes are
bulk upserts and search is a raw aggregation, neither of which benefits from
an ODM. If your app uses Beanie, you can pass Beanie's client in here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Collection, Coroutine, Sequence
from typing import Any, Literal

from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import BulkWriteError, OperationFailure, PyMongoError
from pymongo.operations import SearchIndexModel

from forge_embeddings.fusion import rrf_fuse
from forge_embeddings.vectors import decode_vector, is_subset
from forge_task_documents.models import (
    Chunk,
    DocumentRecord,
    HybridSearchRequest,
    IngestStatus,
    ReplaceStats,
    SearchHit,
    utcnow,
)
from forge_task_documents.storage.mongo import pipelines
from forge_task_documents.storage.mongo.pipelines import LIVE, PUBLISHED, UNPUBLISHED
from forge_task_documents.storage.mongo.schema import (
    CHUNK_INDEXES,
    DOCUMENT_INDEXES,
    chunk_to_doc,
    doc_to_chunk,
    doc_to_record,
    record_to_doc,
    search_index_definition,
    vector_index_definition,
)
from forge_tasks.errors import TaskError, from_pymongo

log = logging.getLogger(__name__)

FusionMode = Literal["server", "client"]
_WRITE_BATCH = 500
_IN_BATCH = 1000
_UNRECOGNIZED_STAGE = 40324  # server error code for an unknown aggregation stage


def _storage_error(exc: PyMongoError) -> TaskError:
    return from_pymongo(exc)


def _wrap_errors[**P, T](fn: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    """Driver errors are classified: failover/network -> StorageError (retried),
    everything else -> permanent TaskError."""

    @functools.wraps(fn)
    async def inner(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except PyMongoError as exc:
            raise _storage_error(exc) from exc

    return inner


# identity of the file a run works on; written by register/begin_run/complete_run
IDENTITY_FIELDS = ("filename", "media_type", "source_uri", "sha256", "size_bytes", "metadata")
_DELETED = IngestStatus.DELETED.value
_PROCESSING = IngestStatus.PROCESSING.value


class MongoDocumentStore:
    def __init__(self, collection: Any) -> None:
        self._c = collection

    @staticmethod
    def _key(tenant_id: str, doc_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id, "doc_id": doc_id}

    @staticmethod
    def _identity(record: DocumentRecord) -> dict[str, Any]:
        doc = record_to_doc(record)
        return {f: doc[f] for f in IDENTITY_FIELDS}

    @_wrap_errors
    async def get(self, tenant_id: str, doc_id: str) -> DocumentRecord | None:
        doc = await self._c.find_one({**self._key(tenant_id, doc_id), "status": {"$ne": _DELETED}})
        return doc_to_record(doc) if doc else None

    @_wrap_errors
    async def register(self, record: DocumentRecord) -> None:
        now = utcnow()
        await self._c.update_one(
            self._key(record.tenant_id, record.doc_id),
            {
                "$set": {
                    **self._identity(record),
                    "status": IngestStatus.PENDING.value,
                    "error": None,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": record.created_at or now, "chunk_count": 0, "run_seq": 0},
            },
            upsert=True,
        )

    @_wrap_errors
    async def begin_run(self, record: DocumentRecord) -> int:
        now = utcnow()
        doc = await self._c.find_one_and_update(
            self._key(record.tenant_id, record.doc_id),
            {
                "$set": {**self._identity(record), "status": _PROCESSING, "error": None, "updated_at": now},
                "$inc": {"run_seq": 1},
                "$setOnInsert": {"created_at": record.created_at or now, "chunk_count": 0},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
            projection={"run_seq": 1},
        )
        return int(doc["run_seq"])

    @_wrap_errors
    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool:
        doc = await self._c.find_one(
            {**self._key(tenant_id, doc_id), "run_seq": run_seq, "status": _PROCESSING}, {"_id": 1}
        )
        return doc is not None

    @_wrap_errors
    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        now = utcnow()
        update = {**fields, "status": IngestStatus.READY.value, "error": None, "updated_at": now, "ingested_at": now}
        res = await self._c.update_one(
            {**self._key(tenant_id, doc_id), "run_seq": run_seq, "status": _PROCESSING}, {"$set": update}
        )
        return bool(res.matched_count)

    @_wrap_errors
    async def fail_run(self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str) -> bool:
        query: dict[str, Any] = self._key(tenant_id, doc_id)
        if run_seq is None:
            query["status"] = IngestStatus.PENDING.value
        else:
            query.update(run_seq=run_seq, status=_PROCESSING)
        res = await self._c.update_one(
            query, {"$set": {"status": IngestStatus.FAILED.value, "error": error[:2000], "updated_at": utcnow()}}
        )
        return bool(res.matched_count)

    @_wrap_errors
    async def list(
        self, tenant_id: str, *, status: IngestStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        query: dict[str, Any] = {"tenant_id": tenant_id, "status": {"$ne": _DELETED}}
        if status is not None:
            query["status"] = status.value
        cursor = self._c.find(query).sort("updated_at", -1).skip(offset).limit(limit)
        return [doc_to_record(d) async for d in cursor]

    @_wrap_errors
    async def delete(self, tenant_id: str, doc_id: str) -> bool:
        # Tombstone instead of removing, so run_seq keeps increasing if the
        # same doc_id is re-created and in-flight runs are fenced out.
        res = await self._c.update_one(
            {**self._key(tenant_id, doc_id), "status": {"$ne": _DELETED}},
            {"$set": {"status": _DELETED, "chunk_count": 0, "updated_at": utcnow()}, "$inc": {"run_seq": 1}},
        )
        return bool(res.matched_count)


class MongoChunkStore:
    def __init__(self, collection: Any, *, binary_vectors: bool = True) -> None:
        self._c = collection
        self.binary_vectors = binary_vectors

    @_wrap_errors
    async def get_embeddings_by_hash(self, tenant_id: str, content_hashes: Collection[str]) -> dict[str, list[float]]:
        hashes = list(dict.fromkeys(content_hashes))
        out: dict[str, list[float]] = {}
        for i in range(0, len(hashes), _IN_BATCH):
            batch = hashes[i : i + _IN_BATCH]
            cursor = self._c.find(
                {"tenant_id": tenant_id, "content_hash": {"$in": batch}, "embedding": {"$exists": True}},
                {"content_hash": 1, "embedding": 1},
            )
            async for doc in cursor:
                vec = decode_vector(doc.get("embedding"))
                if vec is not None:
                    out.setdefault(doc["content_hash"], vec)
        return out

    @_wrap_errors
    async def replace_document_chunks(
        self, tenant_id: str, doc_id: str, run_seq: int, chunks: Sequence[Chunk]
    ) -> ReplaceStats:
        skipped = 0
        for i in range(0, len(chunks), _WRITE_BATCH):
            ops = []
            for c in chunks[i : i + _WRITE_BATCH]:
                doc = chunk_to_doc(c, run_seq, binary_vectors=self.binary_vectors)
                _id = doc.pop("_id")
                # only overwrite chunks written by this or an older run; if a
                # newer run owns the id, the upsert hits a duplicate key (11000).
                # A new chunk is unpublished until the run is; one already
                # there keeps whether it's published.
                ops.append(
                    UpdateOne(
                        {"_id": _id, "run_seq": {"$not": {"$gt": run_seq}}},
                        {"$set": doc, "$setOnInsert": UNPUBLISHED},
                        upsert=True,
                    )
                )
            try:
                await self._c.bulk_write(ops, ordered=False)
            except BulkWriteError as exc:
                errors = exc.details.get("writeErrors", [])
                if any(e.get("code") != 11000 for e in errors):
                    raise
                skipped += len(errors)
        return ReplaceStats(upserted=len(chunks) - skipped, deleted=0)

    @_wrap_errors
    async def publish_run(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        key = {"tenant_id": tenant_id, "doc_id": doc_id}
        await self._c.update_many({**key, "run_seq": run_seq, **UNPUBLISHED}, {"$unset": {LIVE: ""}})
        res = await self._c.delete_many({**key, "run_seq": {"$lt": run_seq}})
        return int(res.deleted_count)

    @_wrap_errors
    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        res = await self._c.delete_many({"tenant_id": tenant_id, "doc_id": doc_id, "run_seq": run_seq, **UNPUBLISHED})
        return int(res.deleted_count)

    @_wrap_errors
    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int:
        res = await self._c.delete_many({"tenant_id": tenant_id, "doc_id": doc_id})
        return int(res.deleted_count)

    @_wrap_errors
    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]:
        ids = list(chunk_ids)
        found: dict[str, Chunk] = {}
        for i in range(0, len(ids), _IN_BATCH):
            cursor = self._c.find({"tenant_id": tenant_id, "_id": {"$in": ids[i : i + _IN_BATCH]}}, {"embedding": 0})
            async for doc in cursor:
                chunk = doc_to_chunk(doc)
                found[chunk.id] = chunk
        return [found[i] for i in ids if i in found]

    @_wrap_errors
    async def get_section(self, tenant_id: str, doc_id: str, section_key: str) -> list[Chunk]:
        cursor = self._c.find(
            {"tenant_id": tenant_id, "doc_id": doc_id, "section_key": section_key, **PUBLISHED}, {"embedding": 0}
        ).sort("ordinal", 1)
        return [doc_to_chunk(d) async for d in cursor]


class AtlasSearchBackend:
    """Hybrid search on Atlas. ``fusion_mode='server'`` uses $rankFusion
    (MongoDB 8.0+); ``'client'`` runs both legs and fuses in Python (any
    Atlas tier, same RRF formula)."""

    def __init__(
        self,
        collection: Any,
        *,
        text_index: str = "chunks_text",
        vector_index: str = "chunks_vector",
        fusion_mode: FusionMode = "server",
    ) -> None:
        self._c = collection
        self.text_index = text_index
        self.vector_index = vector_index
        self.fusion_mode = fusion_mode

    @_wrap_errors
    async def _run(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cursor = await self._c.aggregate(pipeline)
        return [d async for d in cursor]

    @staticmethod
    def _to_hit(doc: dict[str, Any], ranks: dict[str, int] | None = None, score: float | None = None) -> SearchHit:
        raw_score = doc.pop("_score", None)
        details = doc.pop("_score_details", None)
        hit_ranks = dict(ranks or {})
        if details and not hit_ranks:
            for d in details.get("details", []):
                rank = d.get("rank")
                if isinstance(rank, int):
                    hit_ranks[d.get("inputPipelineName", "?")] = rank
        return SearchHit(
            chunk=doc_to_chunk(doc),
            score=float(score if score is not None else (raw_score or 0.0)),
            ranks=hit_ranks,
            details=details,
        )

    async def text_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        docs = await self._run(
            pipelines.single_leg_pipeline(
                request, leg="text", text_index=self.text_index, vector_index=self.vector_index
            )
        )
        return [self._to_hit(d) for d in docs]

    async def vector_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        docs = await self._run(
            pipelines.single_leg_pipeline(
                request, leg="vector", text_index=self.text_index, vector_index=self.vector_index
            )
        )
        return [self._to_hit(d) for d in docs]

    async def hybrid_search(self, request: HybridSearchRequest) -> list[SearchHit]:
        if self.fusion_mode == "server":
            pipeline = pipelines.rank_fusion_pipeline(
                request, text_index=self.text_index, vector_index=self.vector_index
            )
            try:
                cursor = await self._c.aggregate(pipeline)
                docs = [d async for d in cursor]
                return [self._to_hit(d) for d in docs]
            except OperationFailure as exc:
                if exc.code != _UNRECOGNIZED_STAGE:
                    raise _storage_error(exc) from exc
                log.warning("$rankFusion not supported by this cluster; falling back to client-side RRF")
                self.fusion_mode = "client"
            except PyMongoError as exc:
                raise _storage_error(exc) from exc

        text_hits, vector_hits = await asyncio.gather(self.text_search(request), self.vector_search(request))
        by_id = {h.chunk.id: h.chunk for h in [*text_hits, *vector_hits]}
        fused = rrf_fuse(
            {"vector": [h.chunk.id for h in vector_hits], "text": [h.chunk.id for h in text_hits]},
            {"vector": request.vector_weight, "text": request.text_weight},
        )
        return [SearchHit(chunk=by_id[i], score=s, ranks=r) for i, s, r in fused[: request.limit]]


class MongoStorage:
    """Bundles the three stores + schema management for one Atlas database."""

    def __init__(
        self,
        client: Any,
        *,
        database: str,
        documents_collection: str = "documents",
        chunks_collection: str = "chunks",
        text_index: str = "chunks_text",
        vector_index: str = "chunks_vector",
        vector_similarity: str = "dotProduct",
        vector_quantization: str = "scalar",
        language_analyzer: str = "lucene.english",
        binary_vectors: bool = True,
        fusion_mode: FusionMode = "server",
        owns_client: bool = True,
    ) -> None:
        self._client = client
        self._db = client[database]
        self._documents_name = documents_collection
        self._chunks_name = chunks_collection
        self.text_index = text_index
        self.vector_index = vector_index
        self.vector_similarity = vector_similarity
        self.vector_quantization = vector_quantization
        self.language_analyzer = language_analyzer
        self._owns_client = owns_client
        docs_coll = self._db[documents_collection]
        chunks_coll = self._db[chunks_collection]
        self.documents = MongoDocumentStore(docs_coll)
        self.chunks = MongoChunkStore(chunks_coll, binary_vectors=binary_vectors)
        self.search = AtlasSearchBackend(
            chunks_coll, text_index=text_index, vector_index=vector_index, fusion_mode=fusion_mode
        )

    @classmethod
    def from_uri(cls, uri: str, **kwargs: Any) -> MongoStorage:
        from pymongo import AsyncMongoClient

        client: Any = AsyncMongoClient(uri, appname="forge-task-documents", tz_aware=True)
        return cls(client, **kwargs)

    def wanted_search_indexes(self, embedding_dimensions: int) -> dict[str, tuple[str, dict[str, Any]]]:
        return {
            self.text_index: ("search", search_index_definition(language_analyzer=self.language_analyzer)),
            self.vector_index: (
                "vectorSearch",
                vector_index_definition(
                    dimensions=embedding_dimensions,
                    similarity=self.vector_similarity,
                    quantization=self.vector_quantization,
                ),
            ),
        }

    @_wrap_errors
    async def ensure_schema(
        self,
        *,
        embedding_dimensions: int,
        update_existing: bool = False,
        wait: bool = False,
        timeout_s: float = 600,
    ) -> None:
        """Idempotent and safe to run from several replicas at once.

        Existing search indexes whose definition differs are only rebuilt when
        ``update_existing`` is set (a rebuild re-indexes the whole collection);
        otherwise the drift is logged. Run it with update_existing=True from a
        deploy/migration step, not from every API boot.
        """
        # create_indexes creates the collections implicitly (no create_collection race)
        await self._db[self._documents_name].create_indexes(DOCUMENT_INDEXES)
        chunks = self._db[self._chunks_name]
        await chunks.create_indexes(CHUNK_INDEXES)

        wanted = self.wanted_search_indexes(embedding_dimensions)
        current = {ix["name"]: ix async for ix in await chunks.list_search_indexes()}
        to_create: list[SearchIndexModel] = []
        for name, (kind, definition) in wanted.items():
            ix = current.get(name)
            if ix is None:
                to_create.append(SearchIndexModel(definition=definition, name=name, type=kind))
                continue
            if ix.get("type", kind) != kind:
                raise TaskError(f"search index {name!r} exists with type {ix.get('type')!r}, expected {kind!r}")
            if not is_subset(definition, ix.get("latestDefinition") or {}):
                if update_existing:
                    log.info("updating search index %s (full rebuild)", name)
                    await chunks.update_search_index(name, definition)
                else:
                    log.warning(
                        "search index %s differs from the wanted definition; run with update_existing=True", name
                    )
        if to_create:
            try:
                await chunks.create_search_indexes(to_create)
            except OperationFailure:
                # another replica may have created them first
                names = {ix["name"] async for ix in await chunks.list_search_indexes()}
                if not all(m.document["name"] in names for m in to_create):
                    raise
        if wait:
            await self.wait_until_ready(timeout_s=timeout_s)

    async def wait_until_ready(self, *, timeout_s: float = 600, poll_s: float = 5) -> None:
        """Wait until both search indexes are READY on their latest definition."""
        chunks = self._db[self._chunks_name]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        names = {self.text_index, self.vector_index}
        while True:
            status = {ix["name"]: ix.get("status") async for ix in await chunks.list_search_indexes()}
            failed = [n for n in names if status.get(n) == "FAILED"]
            if failed:
                raise TaskError(f"search index build failed: {failed}")
            if all(status.get(n) == "READY" for n in names):
                return
            if loop.time() > deadline:
                raise TimeoutError(f"search indexes not READY after {timeout_s}s: {status}")
            await asyncio.sleep(poll_s)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()
