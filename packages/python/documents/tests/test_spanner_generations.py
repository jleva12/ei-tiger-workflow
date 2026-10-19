"""Exercise Spanner's transaction callbacks and visibility SQL without a service.

SQLite supplies real transactions/queries; this adapter only translates array
parameters and supplies the JSON/search functions used by Spanner's SQL.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

import pytest

from forge_embeddings.vector_store.spanner import RECORDS, VECTORS, put_record, put_vectors, vector_row
from forge_task_documents.errors import StaleRunError
from forge_task_documents.models import Chunk, DocumentRecord, HybridSearchRequest, IngestStatus
from forge_task_documents.models.chunk import ChunkKind
from forge_task_documents.storage.spanner import (
    SpannerChunkStore,
    SpannerDocumentStore,
    SpannerSearchBackend,
    _SnapshotQueries,
)


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)

    def json_value(raw: str, path: str) -> str | None:
        value = json.loads(raw)
        for key in path.removeprefix("$.").split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            return None
        return json.dumps(value) if isinstance(value, bool) else str(value)

    def score(text: str | None, query: str) -> int:
        return sum(word.lower() in (text or "").lower() for word in re.findall(r'"([^"]+)"', query))

    db.create_function("JSON_VALUE", 2, json_value)
    db.create_function("SEARCH", 2, lambda text, query: score(text, query) > 0)
    db.create_function("SCORE", 2, score)
    db.create_function("IF", 3, lambda predicate, yes, no: yes if predicate else no)
    db.create_function("ARRAY_LENGTH", 1, lambda value: len(json.loads(value)) if value else None)
    db.create_function(
        "DOT_PRODUCT", 2, lambda a, b: sum(x * y for x, y in zip(json.loads(a), json.loads(b), strict=True))
    )
    return db


def encoded(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, list | dict) else value


class Reader:
    def __init__(self, db: sqlite3.Connection, owner: SqlSpanner) -> None:
        self.db, self.owner = db, owner

    def execute_sql(self, sql: str, *, params: dict[str, Any], **kwargs: Any) -> list[Any]:
        sql = re.sub(r"IN UNNEST\((@\w+)\)", r"IN (SELECT value FROM json_each(\1))", sql)
        rows = list(self.db.execute(sql, {key: encoded(value) for key, value in params.items()}))
        if self.owner.after_read:
            callback, self.owner.after_read = self.owner.after_read, None
            callback()
        return rows

    def insert_or_update(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        if table == VECTORS:
            self.owner.vector_batches += 1
            if self.owner.fail_vector_batch == self.owner.vector_batches:
                raise RuntimeError("injected chunk write failure")
        self.db.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [[encoded(value) for value in row] for row in values],
        )

    def execute_update(self, sql: str, *, params: dict[str, Any], **kwargs: Any) -> int:
        if self.owner.fail_delete:
            raise RuntimeError("injected cleanup failure")
        return self.db.execute(sql, params).rowcount


class Checkout:
    def __init__(self, owner: SqlSpanner) -> None:
        self.owner = owner

    def __enter__(self) -> Reader:
        self.copy = connection()
        self.owner.connection.backup(self.copy)
        return Reader(self.copy, self.owner)

    def __exit__(self, *args: Any) -> None:
        self.copy.close()


class SqlSpanner:
    """The Database interface plus a snapshot-checkout SDK stand-in."""

    def __init__(self) -> None:
        self.connection = connection()
        self.db = self
        self.after_read: Callable[[], None] | None = None
        self.fail_vector_batch: int | None = None
        self.vector_batches = 0
        self.fail_delete = False
        self.snapshots = 0
        self.connection.executescript(f"""
            CREATE TABLE {RECORDS} (Namespace TEXT, TenantId TEXT, Id TEXT, Payload TEXT,
                PRIMARY KEY(Namespace,TenantId,Id));
            CREATE TABLE {VECTORS} (
                Namespace TEXT, TenantId TEXT, ScopeId TEXT, Id TEXT, Payload TEXT,
                RunSeq INTEGER, ContentHash TEXT, EmbeddingModel TEXT, Embedding TEXT,
                SearchText TEXT, Title TEXT, SectionText TEXT, ContextText TEXT, Identifiers TEXT,
                TextTokens TEXT GENERATED ALWAYS AS (SearchText),
                TitleTokens TEXT GENERATED ALWAYS AS (Title),
                SectionTokens TEXT GENERATED ALWAYS AS (SectionText),
                ContextTokens TEXT GENERATED ALWAYS AS (ContextText),
                PRIMARY KEY(Namespace,TenantId,ScopeId,Id));
        """)

    def snapshot(self, **kwargs: Any) -> Checkout:
        self.snapshots += 1
        return Checkout(self)

    async def query(self, sql: str, params: dict[str, Any]) -> list[Any]:
        with self.snapshot() as reader:
            return reader.execute_sql(sql, params=params)

    async def transaction(self, callback: Callable[[Reader], Any]) -> Any:
        with self.connection:
            return callback(Reader(self.connection, self))


def record() -> DocumentRecord:
    return DocumentRecord(tenant_id="t1", doc_id="d1", filename="d1.md", sha256="hash", size_bytes=10)


def chunk(chunk_id: str = "same", text: str = "Old document", *, ordinal: int = 0) -> Chunk:
    return Chunk(
        id=chunk_id,
        tenant_id="t1",
        doc_id="d1",
        source_type="markdown",
        kind=ChunkKind.PROSE,
        ordinal=ordinal,
        title="Document",
        section_path=["Section"],
        section_key="section",
        text=text,
        token_count=3,
        chunker_version="v1",
        embedding=[1.0, 0.5],
        embedding_model="m1",
    )


@pytest.fixture
def stores():
    db = SqlSpanner()
    yield db, SpannerDocumentStore(db), SpannerChunkStore(db), SpannerSearchBackend(db, "dotProduct")
    db.connection.close()


async def committed(docs, chunks, items: list[Chunk]) -> int:
    seq = await docs.begin_run(record())
    await chunks.replace_document_chunks("t1", "d1", seq, items)
    assert await docs.complete_run("t1", "d1", seq, fields={"chunk_count": len(items)})
    return seq


async def test_deleted_and_replaced_source_reads_cannot_claim_a_new_run(stores):
    _, docs, _, _ = stores
    initial = await docs.begin_run(record(), expected_run_seq=0)
    assert await docs.delete("t1", "d1")
    with pytest.raises(StaleRunError):
        await docs.begin_run(record(), expected_run_seq=initial)
    assert await docs.get("t1", "d1") is None
    assert not await docs.delete("t1", "unknown")
    with pytest.raises(StaleRunError):
        await docs.begin_run(record().model_copy(update={"doc_id": "unknown"}), expected_run_seq=0)
    await docs.register(record())
    pending = await docs.get("t1", "d1")
    assert pending.run_seq > initial + 1
    assert pending.published_run_seq == 0
    with pytest.raises(StaleRunError):
        await docs.begin_run(record(), expected_run_seq=initial)
    assert not await docs.fail_run("t1", "d1", None, error="obsolete load", expected_run_seq=initial)
    assert (await docs.get("t1", "d1")).status is IngestStatus.PENDING
    assert await docs.begin_run(record(), expected_run_seq=pending.run_seq) == pending.run_seq + 1


async def test_same_public_chunk_id_keeps_old_content_until_atomic_commit(stores):
    _, docs, chunks, search = stores
    first = await committed(docs, chunks, [chunk()])
    next_run = await docs.begin_run(record())
    await chunks.replace_document_chunks("t1", "d1", next_run, [chunk(text="New document")])
    assert (await chunks.get_chunks("t1", ["same"]))[0].text == "Old document"
    assert (await chunks.get_section("t1", "d1", "section"))[0].text == "Old document"
    request = HybridSearchRequest(tenant_ids=["t1"], text="document", vector=[1.0, 0.5], limit=1, per_leg_limit=1)
    hits = await search.vector_search(request)
    assert [hit.chunk.text for hit in hits] == ["Old document"]
    assert hits[0].generation == first
    assert await docs.complete_run("t1", "d1", next_run, fields={"chunk_count": 1})
    assert (await docs.get("t1", "d1")).published_run_seq == next_run
    assert (await chunks.get_chunks("t1", ["same"]))[0].text == "New document"
    assert [hit.chunk.text for hit in await search.text_search(request)] == ["New document"]
    assert await chunks.get_section("t1", "d1", "section", generation=hits[0].generation) == []
    assert [c.text for c in await chunks.get_section("t1", "d1", "section", generation=next_run)] == ["New document"]
    assert await chunks.delete_run_chunks("t1", "d1", next_run) == 0
    assert await chunks.publish_run("t1", "d1", first) == 1
    assert await chunks.publish_run("t1", "d1", next_run) == 0


async def test_partial_batch_failure_is_invisible_and_preserves_committed_content(stores):
    db, docs, chunks, _ = stores
    await committed(docs, chunks, [chunk()])
    next_run = await docs.begin_run(record())
    db.fail_vector_batch = db.vector_batches + 2
    replacement = [chunk(str(i), f"New document {i}", ordinal=i) for i in range(60)]
    replacement[0] = chunk(text="Changed same ID")
    with pytest.raises(RuntimeError, match="write failure"):
        await chunks.replace_document_chunks("t1", "d1", next_run, replacement)
    assert await docs.fail_run("t1", "d1", next_run, error="failed")
    assert [c.text for c in await chunks.get_section("t1", "d1", "section")] == ["Old document"]
    assert await chunks.delete_run_chunks("t1", "d1", next_run) == 50
    assert [c.text for c in await chunks.get_chunks("t1", ["same"])] == ["Old document"]


async def test_failed_cleanup_does_not_change_success_or_expose_old_generations(stores):
    db, docs, chunks, _ = stores
    await committed(docs, chunks, [chunk()])
    current = await committed(docs, chunks, [chunk("new", "New document")])
    db.fail_delete = True
    assert await chunks.publish_run("t1", "d1", current) == 0
    assert (await docs.get("t1", "d1")).status is IngestStatus.READY
    assert [c.id for c in await chunks.get_chunks("t1", ["same", "new"])] == ["new"]


async def test_tombstone_hides_chunks_and_old_delete_cleanup_cannot_erase_recreation(stores):
    _, docs, chunks, _ = stores
    await committed(docs, chunks, [chunk()])
    assert await docs.delete("t1", "d1")
    assert await chunks.get_chunks("t1", ["same"]) == []
    assert await chunks.get_section("t1", "d1", "section") == []
    await docs.register(record())
    await committed(docs, chunks, [chunk("new", "Recreated document")])
    assert await chunks.delete_document_chunks("t1", "d1") == 0
    assert [c.id for c in await chunks.get_section("t1", "d1", "section")] == ["new"]
    assert await docs.delete("t1", "d1")
    assert await chunks.delete_document_chunks("t1", "d1") == 2


async def test_legacy_rows_remain_visible_until_first_generation_commit(stores):
    db, docs, chunks, search = stores
    legacy = record().model_copy(update={"status": IngestStatus.READY, "run_seq": 1}).model_dump(mode="json")
    legacy.pop("published_run_seq", None)
    await db.transaction(
        lambda tx: tx.insert_or_update(
            RECORDS, ["Namespace", "TenantId", "Id", "Payload"], [["documents", "t1", "d1", legacy]]
        )
    )
    await db.transaction(
        lambda tx: put_vectors(tx, [vector_row(chunk(), namespace="chunks", scope="d1", key="same", seq=1)])
    )
    assert [c.id for c in await chunks.get_chunks("t1", ["same"])] == ["same"]
    assert (await docs.get("t1", "d1")).published_run_seq is None
    hits = await search.text_search(HybridSearchRequest(tenant_ids=["t1"], text="document", vector=[]))
    assert hits[0].generation == 1
    assert "generation" not in hits[0].model_dump()
    await committed(docs, chunks, [chunk("new", "New document")])
    assert [c.id for c in await chunks.get_chunks("t1", ["same", "new"])] == ["new"]


async def test_recreated_legacy_document_does_not_republish_deleted_chunks(stores):
    db, docs, chunks, _ = stores
    legacy = record().model_copy(update={"status": IngestStatus.READY, "run_seq": 1, "published_run_seq": None})
    await db.transaction(lambda tx: put_record(tx, "documents", "t1", "d1", legacy))
    await db.transaction(
        lambda tx: put_vectors(tx, [vector_row(chunk(), namespace="chunks", scope="d1", key="same", seq=1)])
    )
    assert await chunks.get_chunks("t1", ["same"])
    await docs.delete("t1", "d1")
    await docs.register(record())
    assert (await docs.get("t1", "d1")).published_run_seq == 0
    assert await chunks.get_chunks("t1", ["same"]) == []
    seq = await docs.begin_run(record())
    assert await chunks.get_chunks("t1", ["same"]) == []
    assert await docs.fail_run("t1", "d1", seq, error="new run failed")
    assert await chunks.get_chunks("t1", ["same"]) == []


async def test_hybrid_legs_share_publication_snapshot(stores):
    db, docs, chunks, search = stores
    await committed(docs, chunks, [chunk("old", "Old document")])
    replacement = await docs.begin_run(record())
    await chunks.replace_document_chunks("t1", "d1", replacement, [chunk("new", "New document")])
    before = await docs.get("t1", "d1")

    def publish_between_legs() -> None:
        published = before.model_copy(update={"published_run_seq": replacement, "status": IngestStatus.READY})
        with db.connection:
            put_record(Reader(db.connection, db), "documents", "t1", "d1", published)

    db.after_read = publish_between_legs
    snapshot_count = db.snapshots
    request = HybridSearchRequest(tenant_ids=["t1"], text="document", vector=[1.0, 0.5])
    assert [hit.chunk.id for hit in await search.hybrid_search(request)] == ["old"]
    assert db.snapshots == snapshot_count + 1
    assert [hit.chunk.id for hit in await search.hybrid_search(request)] == ["new"]


@pytest.mark.timeout(5)
async def test_cancelled_snapshot_read_finishes_before_session_cleanup():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingReader:
        def execute_sql(self, *args: Any, **kwargs: Any) -> list[Any]:
            started.set()
            release.wait(3)
            finished.set()
            return []

    queries = _SnapshotQueries(BlockingReader())
    reading = asyncio.create_task(queries.query("SELECT 1"))
    assert await asyncio.to_thread(started.wait, 1)
    reading.cancel()
    closing = asyncio.create_task(queries.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await closing
    assert finished.is_set()
    with pytest.raises(RuntimeError, match="snapshot is closed"):
        await queries.query("SELECT 1")
