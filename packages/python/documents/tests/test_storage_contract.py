"""Behavioural contract every StorageBackend must satisfy (documents + chunks).

Add your own backend to BACKENDS to verify it before switching databases.
"""

from __future__ import annotations

import pytest

from forge_embeddings.embedding import content_hash
from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_task_documents.chunking import ChunkEngine
from forge_task_documents.models import DocumentRecord, IngestStatus
from forge_task_documents.protocols import ChunkStore, DocumentStore, SearchBackend, StorageBackend
from forge_task_documents.storage.memory import InMemoryStorage
from forge_task_documents.storage.mongo import MongoStorage

from .conftest import source
from .mongo_fake import AsyncMongoMockClient


def _memory():
    return InMemoryStorage()


def _mongo():
    return MongoStorage(AsyncMongoMockClient(), database="test", binary_vectors=True)


def _mongo_plain_vectors():
    return MongoStorage(AsyncMongoMockClient(), database="test", binary_vectors=False)


BACKENDS = {"memory": _memory, "mongo": _mongo, "mongo-array-vectors": _mongo_plain_vectors}


@pytest.fixture(params=list(BACKENDS))
def backend(request) -> StorageBackend:
    return BACKENDS[request.param]()


def _record(doc_id="d1", tenant="t1", sha="abc"):
    return DocumentRecord(doc_id=doc_id, tenant_id=tenant, filename=f"{doc_id}.md", sha256=sha, size_bytes=10)


def _chunks(registry, files, tenant="t1", doc_id="runbook.md"):
    src = source("runbook.md", files["runbook.md"], tenant=tenant, doc_id=doc_id)
    doc = registry.resolve(src).parse(src)
    chunks = ChunkEngine(HeuristicTokenizer()).chunk(doc)
    for i, c in enumerate(chunks):
        c.embed_text = c.text
        c.content_hash = content_hash(c.text, "m")
        c.embedding = [float(i), 0.5, -0.25, 1.0]
        c.embedding_model = "m"
    return chunks


def test_conforms_to_protocols(backend):
    assert isinstance(backend, StorageBackend)
    assert isinstance(backend.documents, DocumentStore)
    assert isinstance(backend.chunks, ChunkStore)
    assert isinstance(backend.search, SearchBackend)


async def test_run_lifecycle_and_fencing(backend):
    docs = backend.documents
    s1 = await docs.begin_run(_record())
    rec = await docs.get("t1", "d1")
    assert rec.status is IngestStatus.PROCESSING and rec.run_seq == s1 == 1
    assert await docs.is_current_run("t1", "d1", s1)

    s2 = await docs.begin_run(_record(sha="def"))  # newer run takes over
    assert s2 > s1
    assert not await docs.is_current_run("t1", "d1", s1)
    assert not await docs.complete_run("t1", "d1", s1, fields={"chunk_count": 1})
    assert not await docs.fail_run("t1", "d1", s1, error="late")
    assert await docs.complete_run("t1", "d1", s2, fields={"chunk_count": 7, "chunker_version": "v", "title": "T"})
    rec = await docs.get("t1", "d1")
    assert rec.status is IngestStatus.READY and rec.chunk_count == 7 and rec.sha256 == "def"
    assert rec.ingested_at is not None and rec.error is None
    assert not await docs.is_current_run("t1", "d1", s2)  # finished runs are not "current"

    s3 = await docs.begin_run(_record(sha="ghi"))
    rec = await docs.get("t1", "d1")
    assert rec.chunk_count == 7 and rec.chunker_version == "v" and rec.title == "T"  # kept while processing
    assert await docs.fail_run("t1", "d1", s3, error="boom")
    assert (await docs.get("t1", "d1")).status is IngestStatus.FAILED


async def test_register_pending_then_fail_without_run(backend):
    docs = backend.documents
    await docs.register(_record(doc_id="p1"))
    rec = await docs.get("t1", "p1")
    assert rec.status is IngestStatus.PENDING and rec.run_seq == 0
    assert await docs.fail_run("t1", "p1", None, error="load failed")
    assert (await docs.get("t1", "p1")).status is IngestStatus.FAILED
    assert not await docs.fail_run("t1", "missing", None, error="x")
    # None never clobbers a record a run has claimed
    await docs.register(_record(doc_id="p2"))
    seq = await docs.begin_run(_record(doc_id="p2"))
    assert not await docs.fail_run("t1", "p2", None, error="x")
    assert await docs.complete_run("t1", "p2", seq, fields={})


async def test_delete_is_a_tombstone_that_keeps_the_fence(backend):
    docs = backend.documents
    s1 = await docs.begin_run(_record())
    assert await docs.delete("t1", "d1")
    assert await docs.get("t1", "d1") is None
    assert not await docs.delete("t1", "d1")
    assert not await docs.complete_run("t1", "d1", s1, fields={})  # in-flight run is fenced out
    assert await docs.list("t1") == []
    s2 = await docs.begin_run(_record())  # re-created: the fence keeps increasing
    assert s2 > s1 + 1
    rec = await docs.get("t1", "d1")
    assert rec.status is IngestStatus.PROCESSING and rec.chunk_count == 0


async def test_list_documents(backend):
    for i in range(3):
        await backend.documents.begin_run(_record(doc_id=f"d{i}"))
    await backend.documents.begin_run(_record(doc_id="x", tenant="t2"))
    await backend.documents.complete_run("t1", "d1", 1, fields={})
    assert len(await backend.documents.list("t1")) == 3
    ready = await backend.documents.list("t1", status=IngestStatus.READY)
    assert [r.doc_id for r in ready] == ["d1"]


async def test_replace_chunks_upserts_and_removes_stale(backend, registry, files):
    store = backend.chunks
    chunks = _chunks(registry, files)
    stats = await store.replace_document_chunks("t1", "runbook.md", 1, chunks)
    assert stats.upserted == len(chunks) and stats.deleted == 0

    kept = chunks[1:]  # pretend the first chunk disappeared in the new version
    stats = await store.replace_document_chunks("t1", "runbook.md", 2, kept)
    assert stats.deleted == 1
    got = await store.get_chunks("t1", [c.id for c in chunks])
    assert [c.id for c in got] == [c.id for c in kept]  # input order preserved, stale gone
    assert all(c.embedding is None for c in got)  # vectors never leave the store on reads

    vectors = await store.get_embeddings_by_hash("t1", {c.content_hash for c in kept})
    assert vectors[kept[0].content_hash] == pytest.approx(kept[0].embedding)
    assert await store.get_embeddings_by_hash("t2", {c.content_hash for c in kept}) == {}  # tenant scoped


async def test_older_run_cannot_overwrite_newer_chunks(backend, registry, files):
    store = backend.chunks
    chunks = _chunks(registry, files)
    newer = [c.model_copy(update={"text": c.text + " (v2)"}) for c in chunks[1:]]
    await store.replace_document_chunks("t1", "runbook.md", 5, newer)
    # a stale run (seq 3) finishes late and writes the old version
    stats = await store.replace_document_chunks("t1", "runbook.md", 3, chunks)
    assert stats.upserted == 1 and stats.deleted == 0  # only its extra chunk got in
    got = {c.id: c for c in await store.get_chunks("t1", [c.id for c in chunks])}
    assert all(got[c.id].text.endswith("(v2)") for c in newer)  # newer content intact
    assert await store.delete_run_chunks("t1", "runbook.md", 3) == 1  # stale run cleans up
    assert {c.id for c in await store.get_chunks("t1", [c.id for c in chunks])} == {c.id for c in newer}


async def test_get_section_is_ordered(backend, registry, files):
    chunks = _chunks(registry, files)
    await backend.chunks.replace_document_chunks("t1", "runbook.md", 1, list(reversed(chunks)))
    key = chunks[0].section_key
    section = await backend.chunks.get_section("t1", "runbook.md", key)
    assert section and [c.ordinal for c in section] == sorted(c.ordinal for c in section)
    assert all(c.section_key == key for c in section)


async def test_delete_document_chunks_is_tenant_scoped(backend, registry, files):
    await backend.chunks.replace_document_chunks("t1", "runbook.md", 1, _chunks(registry, files, tenant="t1"))
    await backend.chunks.replace_document_chunks("t2", "runbook.md", 1, _chunks(registry, files, tenant="t2"))
    removed = await backend.chunks.delete_document_chunks("t1", "runbook.md")
    assert removed > 0
    left = _chunks(registry, files, tenant="t2")
    assert len(await backend.chunks.get_chunks("t2", [c.id for c in left])) == len(left)
