"""Behavioural contract every StorageBackend must satisfy (documents + chunks).

Add your own backend to BACKENDS to verify it before switching databases.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from forge_embeddings.embedding import content_hash
from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_task_documents.chunking import ChunkEngine
from forge_task_documents.errors import StaleRunError
from forge_task_documents.models import DocumentRecord, IngestStatus
from forge_task_documents.protocols import ChunkStore, DocumentStore, SearchBackend, StorageBackend
from forge_task_documents.storage.memory import InMemoryStorage
from forge_task_documents.storage.mongo import MongoStorage
from forge_task_documents.task import IngestFileJob, IngestFilePayload
from forge_tasks.tasks import JobStatus

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
    with pytest.raises(StaleRunError):
        await docs.begin_run(_record())
    await docs.register(_record())  # only an explicit registration recreates it
    s2 = await docs.begin_run(_record())
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


async def stage(backend, chunks):
    first = chunks[0]
    seq = await backend.documents.begin_run(_record(first.doc_id, first.tenant_id))
    stats = await backend.chunks.replace_document_chunks(first.tenant_id, first.doc_id, seq, chunks)
    assert stats.upserted == len(chunks) and stats.deleted == 0
    return seq


async def commit(backend, seq, *, tenant="t1", doc_id="runbook.md"):
    assert await backend.documents.complete_run(tenant, doc_id, seq, fields={})


async def test_replace_chunks_upserts_and_publishing_removes_stale(backend, registry, files):
    chunks = _chunks(registry, files)
    s1 = await stage(backend, chunks)
    assert await backend.chunks.get_chunks("t1", [c.id for c in chunks]) == []
    await commit(backend, s1)
    assert await backend.chunks.publish_run("t1", "runbook.md", s1) == 0
    kept = chunks[1:]
    s2 = await stage(backend, kept)
    assert len(await backend.chunks.get_chunks("t1", [c.id for c in chunks])) == len(chunks)
    await commit(backend, s2)
    assert await backend.chunks.publish_run("t1", "runbook.md", s2) == len(chunks)
    assert await backend.chunks.publish_run("t1", "runbook.md", s2) == 0
    got = await backend.chunks.get_chunks("t1", [c.id for c in chunks])
    assert [c.id for c in got] == [c.id for c in kept]
    assert all(c.embedding is None for c in got)
    vectors = await backend.chunks.get_embeddings_by_hash("t1", {c.content_hash for c in kept})
    assert vectors[kept[0].content_hash] == pytest.approx(kept[0].embedding)
    assert await backend.chunks.get_embeddings_by_hash("t2", {c.content_hash for c in kept}) == {}


async def test_older_run_cannot_overwrite_newer_chunks(backend, registry, files):
    chunks = _chunks(registry, files)
    s1 = await stage(backend, chunks)
    newer = [c.model_copy(update={"text": c.text + " (v2)"}) for c in chunks[1:]]
    s2 = await stage(backend, newer)
    await commit(backend, s2)
    await backend.chunks.replace_document_chunks("t1", "runbook.md", s1, chunks)
    assert not await backend.documents.complete_run("t1", "runbook.md", s1, fields={})
    got = await backend.chunks.get_chunks("t1", [c.id for c in chunks])
    assert len(got) == len(newer) and all(c.text.endswith("(v2)") for c in got)
    assert await backend.chunks.delete_run_chunks("t1", "runbook.md", s1) == len(chunks)
    assert await backend.chunks.delete_run_chunks("t1", "runbook.md", s2) == 0


async def test_only_published_chunks_are_read_by_section(backend, registry, files):
    chunks = _chunks(registry, files)
    seq = await stage(backend, chunks)
    assert await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key) == []
    await commit(backend, seq)
    assert await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key)


async def test_a_failed_same_id_generation_cannot_mutate_published_metadata(backend, registry, files):
    chunks = _chunks(registry, files)
    for c in chunks:
        c.metadata = {"tags": ["approved"]}
    first = await stage(backend, chunks)
    await commit(backend, first)
    edited = [c.model_copy(update={"metadata": {"tags": ["unapproved"]}}) for c in chunks]
    second = await stage(backend, edited)
    assert await backend.documents.fail_run("t1", "runbook.md", second, error="embedding provider failed")
    visible = await backend.chunks.get_chunks("t1", [c.id for c in chunks])
    assert len(visible) == len(chunks) and all(c.metadata["tags"] == ["approved"] for c in visible)
    assert await backend.chunks.delete_run_chunks("t1", "runbook.md", second) == len(chunks)
    assert await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key)


async def test_cleanup_failure_never_exposes_two_generations(backend, registry, files, monkeypatch):
    chunks = _chunks(registry, files)
    first = await stage(backend, chunks)
    await commit(backend, first)
    newer = [c.model_copy(update={"text": c.text + " (new)"}) for c in chunks]
    second = await stage(backend, newer)
    await commit(backend, second)

    def fail(*args, **kwargs):
        raise RuntimeError("garbage collection unavailable")

    async def afail(*args, **kwargs):
        fail()

    if isinstance(backend, InMemoryStorage):
        monkeypatch.setattr(backend.chunks, "_remove", fail)
    else:
        monkeypatch.setattr(backend.chunks._c, "delete_many", afail)
    assert await backend.chunks.publish_run("t1", "runbook.md", second) == 0
    visible = await backend.chunks.get_chunks("t1", [c.id for c in chunks])
    assert len(visible) == len(chunks) and all(c.text.endswith("(new)") for c in visible)
    section = await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key)
    assert section and all(c.text.endswith("(new)") for c in section)
    assert await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key, generation=first) == []


async def test_get_section_is_ordered(backend, registry, files):
    chunks = _chunks(registry, files)
    seq = await stage(backend, list(reversed(chunks)))
    await commit(backend, seq)
    section = await backend.chunks.get_section("t1", "runbook.md", chunks[0].section_key)
    assert section and [c.ordinal for c in section] == sorted(c.ordinal for c in section)


async def test_delete_document_chunks_is_tenant_scoped(backend, registry, files):
    for tenant in ("t1", "t2"):
        seq = await stage(backend, _chunks(registry, files, tenant=tenant))
        await commit(backend, seq, tenant=tenant)
    await backend.documents.delete("t1", "runbook.md")
    assert await backend.chunks.get_section("t1", "runbook.md", "anything") == []
    assert await backend.chunks.delete_document_chunks("t1", "runbook.md") > 0
    left = _chunks(registry, files, tenant="t2")
    assert len(await backend.chunks.get_chunks("t2", [c.id for c in left])) == len(left)


async def test_old_delete_cleanup_cannot_remove_a_recreated_document(backend, registry, files):
    chunks = _chunks(registry, files)
    first = await stage(backend, chunks)
    await commit(backend, first)
    await backend.documents.delete("t1", "runbook.md")
    await backend.documents.register(_record("runbook.md"))
    second = await stage(backend, chunks)
    await commit(backend, second)
    assert await backend.chunks.delete_document_chunks("t1", "runbook.md") == 0
    assert len(await backend.chunks.get_chunks("t1", [c.id for c in chunks])) == len(chunks)


@pytest.mark.parametrize("registered", [False, True])
async def test_deleting_during_source_loading_cannot_resurrect_document(backend, pipeline, registered):
    if registered:
        await backend.documents.register(_record())
    loading, release = asyncio.Event(), asyncio.Event()

    async def load(*args, **kwargs):
        loading.set()
        await release.wait()
        return source("d1.md", b"# Private\n\nDeleted policy text.", doc_id="d1")

    pipeline.storage = backend
    job = IngestFileJob(SimpleNamespace(loader=SimpleNamespace(load=load), storage=backend, pipeline=pipeline))
    pending = asyncio.create_task(job.run(IngestFilePayload(tenant_id="t1", doc_id="d1", uri="old-upload")))
    await loading.wait()
    await backend.documents.delete("t1", "d1")
    release.set()
    assert (await pending).status is JobStatus.SUPERSEDED
    assert await backend.documents.get("t1", "d1") is None
    assert await backend.chunks.get_section("t1", "d1", "anything") == []


async def test_old_loader_failure_does_not_fail_new_registration(backend, pipeline):
    await backend.documents.register(_record())
    loading, release = asyncio.Event(), asyncio.Event()

    async def load(*args, **kwargs):
        loading.set()
        await release.wait()
        raise FileNotFoundError("old upload removed")

    pipeline.storage = backend
    job = IngestFileJob(SimpleNamespace(loader=SimpleNamespace(load=load), storage=backend, pipeline=pipeline))
    pending = asyncio.create_task(job.run(IngestFilePayload(tenant_id="t1", doc_id="d1", uri="old-upload")))
    await loading.wait()
    await backend.documents.register(_record(sha="replacement"))
    release.set()
    assert (await pending).status is JobStatus.FAILED
    record = await backend.documents.get("t1", "d1")
    assert record.status is IngestStatus.PENDING and record.sha256 == "replacement" and record.error is None
