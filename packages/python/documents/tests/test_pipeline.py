"""End-to-end: every format through parse -> chunk -> enrich -> embed -> store ->
hybrid search, on the in-memory backend."""

from __future__ import annotations

import pytest

from forge_task_documents.errors import StaleRunError, UnsupportedFormatError
from forge_task_documents.ingestion import IngestionPipeline
from forge_task_documents.models import ChunkKind, IngestStatus, SearchFilters, SearchQuery
from forge_task_documents.storage.memory import InMemoryStorage

from . import fixtures
from .conftest import source


@pytest.fixture
async def loaded(pipeline, files):
    results = {}
    for name, data in files.items():
        results[name] = await pipeline.ingest(
            source(name, data, metadata={"tags": ["kb"] if name.endswith(".md") else []})
        )
    return results


async def test_ingest_all_formats(loaded, storage):
    for name, result in loaded.items():
        assert result.status is IngestStatus.READY, name
        assert result.chunk_count > 0
        rec = await storage.documents.get("t1", name)
        assert rec.status is IngestStatus.READY and rec.chunk_count == result.chunk_count
        assert rec.chunker_version and rec.embedding_model == "hashing-v1@256"
    stored = [c for c, _ in storage.chunks.rows.values()]
    assert all(c.embedding and len(c.embedding) == 256 for c in stored)
    assert all(c.embed_text.startswith("Document: ") for c in stored)
    assert {c.source_type for c in stored} == {"docx", "xlsx", "markdown", "text", "csv", "visio", "pdf", "pptx"}


async def test_exact_identifier_search(loaded, search_service):
    resp = await search_service.search(SearchQuery(tenant_ids=["t1"], text="SKU10042", rerank=False))
    top = resp.hits[0].chunk
    assert "SKU10042" in top.text and top.kind is ChunkKind.TABLE
    assert resp.hits[0].ranks.get("text") == 1


async def test_semantic_ish_search_and_rerank(loaded, search_service):
    resp = await search_service.search(
        SearchQuery(tenant_ids=["t1"], text="who approves a refund above the limit", top_k=3)
    )
    assert len(resp.hits) == 3
    assert all(h.rerank_score is not None for h in resp.hits)
    assert any("approval" in h.chunk.text.lower() for h in resp.hits)
    assert set(resp.timings_ms) >= {"embed_ms", "search_ms", "rerank_ms", "total_ms"}


async def test_hits_carry_their_similarity_and_irrelevant_ones_are_left_out(loaded, storage, embedder):
    from forge_task_documents.retrieval import HybridSearchService, SearchConfig

    service = HybridSearchService(search=storage.search, chunks=storage.chunks, embedder=embedder)
    answer = await service.search(SearchQuery(tenant_ids=["t1"], text="who approves a refund above the limit"))
    assert answer.hits and all(h.similarity is not None for h in answer.hits)
    assert "relevance_ms" in answer.timings_ms
    off_topic = SearchQuery(tenant_ids=["t1"], text="weather forecast tomorrow")
    # Nothing near enough, and nothing sharing a word: nothing found.
    assert (await service.search(off_topic)).hits == []
    # Without a floor, the nearest neighbours come back, however far.
    unfloored = HybridSearchService(
        search=storage.search, chunks=storage.chunks, embedder=embedder, config=SearchConfig(min_similarity=None)
    )
    assert (await unfloored.search(off_topic)).hits


async def test_a_reranker_floor_decides_when_set(loaded, storage, embedder):
    from forge_embeddings.embedding import OverlapReranker
    from forge_task_documents.retrieval import HybridSearchService, SearchConfig

    service = HybridSearchService(
        search=storage.search,
        chunks=storage.chunks,
        embedder=embedder,
        reranker=OverlapReranker(),
        config=SearchConfig(min_rerank_score=0.99),
    )
    # Every word of it in one passage: only those pass.
    found = await service.search(SearchQuery(tenant_ids=["t1"], text="refund approval limit"))
    assert found.hits and all(h.rerank_score is not None and h.rerank_score >= 0.99 for h in found.hits)
    # An exact identifier passes whatever its score.
    exact = await service.search(SearchQuery(tenant_ids=["t1"], text="price of SKU10042 in the catalogue"))
    sku = next(h for h in exact.hits if "SKU10042" in h.chunk.text)
    assert sku.rerank_score is not None and sku.rerank_score < 0.99


async def test_diagram_is_searchable(loaded, search_service):
    resp = await search_service.search(
        SearchQuery(tenant_ids=["t1"], text="what happens after validate order is approved", rerank=False)
    )
    assert resp.hits[0].chunk.source_type == "visio"


async def test_pdf_and_slide_hits_cite_their_page(loaded, search_service):
    pdf = await search_service.search(
        SearchQuery(
            tenant_ids=["t1"], text="reconcile batch", filters=SearchFilters(source_types=["pdf"]), rerank=False
        )
    )
    code = next(h.chunk for h in pdf.hits if "def reconcile" in h.chunk.text)
    assert code.location.page == 2 and code.section_path == ["Settlement"]
    deck = await search_service.search(
        SearchQuery(
            tenant_ids=["t1"], text="refunds per quarter", filters=SearchFilters(source_types=["pptx"]), rerank=False
        )
    )
    chart = next(h.chunk for h in deck.hits if "Refunds per quarter" in h.chunk.text)
    assert chart.location.page == 3 and chart.location.page_name == "Limits by region"


async def test_filters_and_tenant_isolation(loaded, search_service):
    resp = await search_service.search(
        SearchQuery(tenant_ids=["t1"], text="refund", filters=SearchFilters(source_types=["markdown"]), rerank=False)
    )
    assert resp.hits and {h.chunk.source_type for h in resp.hits} == {"markdown"}
    tagged = await search_service.search(
        SearchQuery(tenant_ids=["t1"], text="refund", filters=SearchFilters(tags=["kb"]), rerank=False)
    )
    assert tagged.hits and all("kb" in h.chunk.metadata["tags"] for h in tagged.hits)
    assert (await search_service.search(SearchQuery(tenant_ids=["other"], text="refund"))).hits == []


async def test_section_expansion(loaded, search_service):
    resp = await search_service.search(
        SearchQuery(
            tenant_ids=["t1"],
            text="settlement batch ledger",
            filters=SearchFilters(source_types=["docx"]),
            expand_sections=True,
            top_k=1,
        )
    )
    hit = resp.hits[0]
    assert hit.expanded_text and len(hit.expanded_text) > len(hit.chunk.text)
    assert "Sentence number 1 " in hit.expanded_text and "Sentence number 59" in hit.expanded_text


async def test_reingest_unchanged_is_skipped(pipeline, files):
    first = await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    assert first.embedding_model == "hashing-v1@256"  # what its chunks are embedded with
    again = await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    assert again.skipped and again.reason == "unchanged" and again.embedding_model == "hashing-v1@256"


REFUNDS_V1 = b"# Refunds\n\nRefunds are approved by the support lead within 30 days.\n"
REFUNDS_V2 = b"# Refunds\n\nRefunds are approved by the finance director within 14 days.\n"


async def found(search_service, text: str) -> str:
    resp = await search_service.search(SearchQuery(tenant_ids=["t1"], text=text, rerank=False))
    return " ".join(h.chunk.text for h in resp.hits)


async def test_search_sees_whole_versions(pipeline, storage, search_service, monkeypatch):
    await pipeline.ingest(source("refunds.md", REFUNDS_V1))
    assert "support lead" in await found(search_service, "who approves refunds")

    # A run writes the new version, then fails before completing: search never
    # sees it, and the last version keeps answering.
    complete = storage.documents.complete_run

    async def database_gone(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(storage.documents, "complete_run", database_gone)
    with pytest.raises(RuntimeError):
        await pipeline.ingest(source("refunds.md", REFUNDS_V2))
    text = await found(search_service, "who approves refunds")
    assert "support lead" in text and "finance director" not in text

    # The next run completes and publishes it: the new version, none of the old.
    monkeypatch.setattr(storage.documents, "complete_run", complete)
    result = await pipeline.ingest(source("refunds.md", REFUNDS_V2))
    assert result.deleted_stale == 1
    text = await found(search_service, "who approves refunds")
    assert "finance director" in text and "support lead" not in text


async def test_a_run_that_stopped_before_publishing_is_published_next_time(
    pipeline, storage, search_service, monkeypatch
):
    publish = storage.chunks.publish_run

    async def crash(*args, **kwargs):
        raise RuntimeError("the worker stopped")

    monkeypatch.setattr(storage.chunks, "publish_run", crash)
    with pytest.raises(RuntimeError):
        await pipeline.ingest(source("refunds.md", REFUNDS_V1))
    assert (await storage.documents.get("t1", "refunds.md")).status is IngestStatus.READY
    assert await found(search_service, "who approves refunds") == ""  # complete, but unpublished

    # Its retry finds it unchanged, and publishes it.
    monkeypatch.setattr(storage.chunks, "publish_run", publish)
    again = await pipeline.ingest(source("refunds.md", REFUNDS_V1))
    assert again.skipped
    assert "support lead" in await found(search_service, "who approves refunds")


async def test_a_new_parser_version_reads_it_again(pipeline, files):
    await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    parser = pipeline.registry.resolve(source("runbook.md", b""))
    parser.version = parser.version + ".1"
    again = await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    assert not again.skipped and again.status is IngestStatus.READY


async def test_vectors_of_another_model_are_not_compared(pipeline, storage, files):
    """Same dimensions, another model (the configured one changed before the
    knowledge base was re-indexed): the vector leg leaves its chunks out, and
    only their words find them."""
    from forge_embeddings.embedding import HashingEmbedder
    from forge_task_documents.retrieval import HybridSearchService

    await pipeline.ingest(source("runbook.md", files["runbook.md"]))

    class Other(HashingEmbedder):
        def __init__(self) -> None:
            super().__init__(dimensions=256)
            self.model_id = "other-model@256"

    other = HybridSearchService(search=storage.search, chunks=storage.chunks, embedder=Other())
    query = SearchQuery(tenant_ids=["t1"], text="refund approval", rerank=False)
    assert (await other.search(query)).hits  # the words still find them ...
    assert all("vector" not in h.ranks for h in (await other.search(query)).hits)  # ... the vectors don't


async def test_force_reingest_reuses_every_embedding(pipeline, embedder, files):
    first = await pipeline.ingest(source("handbook.docx", files["handbook.docx"]))
    calls = embedder.calls
    forced = await pipeline.ingest(source("handbook.docx", files["handbook.docx"]), force=True)
    assert forced.embedded == 0 and forced.reused_embeddings == first.chunk_count
    assert embedder.calls == calls


async def test_edit_only_embeds_changed_chunks_and_removes_stale(pipeline, storage, files):
    first = await pipeline.ingest(source("handbook.docx", files["handbook.docx"]))
    edited = fixtures.make_docx(extra_intro="Internal draft, do not share.")
    second = await pipeline.ingest(source("handbook.docx", edited))
    # only the chunks around the inserted paragraph change
    assert 0 < second.embedded <= 3 and second.embedded < second.chunk_count // 3
    assert second.reused_embeddings == second.chunk_count - second.embedded
    assert second.deleted_stale >= 1
    ids = {cid for cid, (c, _) in storage.chunks.rows.items() if c.doc_id == "handbook.docx"}
    assert len(ids) == second.chunk_count  # no leftovers from the first version
    assert abs(first.chunk_count - second.chunk_count) <= 1


async def test_unsupported_file_marks_failed(pipeline, storage):
    with pytest.raises(UnsupportedFormatError):
        await pipeline.ingest(source("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00"))
    rec = await storage.documents.get("t1", "photo.png")
    assert rec.status is IngestStatus.FAILED and "UnsupportedFormatError" in rec.error


async def test_superseded_run_does_not_write(pipeline, storage, files, monkeypatch):
    original = storage.documents.is_current_run

    async def hijack(tenant_id, doc_id, run_seq):
        # another worker claims the document right before we write
        rec = await storage.documents.get(tenant_id, doc_id)
        await storage.documents.begin_run(rec)
        return await original(tenant_id, doc_id, run_seq)

    monkeypatch.setattr(storage.documents, "is_current_run", hijack)
    with pytest.raises(StaleRunError):
        await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    rec = await storage.documents.get("t1", "runbook.md")
    assert rec.status is IngestStatus.PROCESSING and rec.run_seq == 2  # not marked failed
    assert not storage.chunks.rows


def _chunk_texts(storage, doc_id):
    return sorted(c.text for c, _ in storage.chunks.rows.values() if c.doc_id == doc_id)


async def test_late_stale_run_cannot_clobber_newer_version(pipeline, storage, monkeypatch):
    """R1 (v1) passes its pre-write check, then R2 (v2) runs to completion,
    then R1 writes. The newer version must survive intact."""
    v1, v2 = fixtures.make_docx(), fixtures.make_docx(extra_intro="Version two intro paragraph.")
    original = storage.documents.is_current_run
    state = {"hijacked": False}

    async def hijack(tenant_id, doc_id, run_seq):
        ok = await original(tenant_id, doc_id, run_seq)
        if not state["hijacked"]:
            state["hijacked"] = True
            await pipeline.ingest(source("h.docx", v2))  # R2 runs fully in between
        return ok

    monkeypatch.setattr(storage.documents, "is_current_run", hijack)
    with pytest.raises(StaleRunError):
        await pipeline.ingest(source("h.docx", v1))

    rec = await storage.documents.get("t1", "h.docx")
    assert rec.status is IngestStatus.READY and rec.sha256 == source("h.docx", v2).sha256
    monkeypatch.setattr(storage.documents, "is_current_run", original)
    reference = InMemoryStorage()
    ref_pipeline = IngestionPipeline(
        registry=pipeline.registry,
        chunker=pipeline.chunker,
        enricher=pipeline.enricher,
        embedder=pipeline.embedder,
        storage=reference,
    )
    await ref_pipeline.ingest(source("h.docx", v2))
    assert _chunk_texts(storage, "h.docx") == _chunk_texts(reference, "h.docx")  # exactly v2
    assert (await pipeline.ingest(source("h.docx", v2))).skipped


async def test_reupload_during_run_is_not_lost(pipeline, storage, monkeypatch):
    v1, v2 = fixtures.make_docx(), fixtures.make_docx(extra_intro="Uploaded while v1 was processing.")
    original = storage.documents.is_current_run

    async def reupload(tenant_id, doc_id, run_seq):
        # the API registers v2 while the v1 run is in flight
        from forge_task_documents.models import DocumentRecord

        src = source("h.docx", v2)
        await storage.documents.register(
            DocumentRecord(
                doc_id=doc_id, tenant_id=tenant_id, filename="h.docx", sha256=src.sha256, size_bytes=src.size_bytes
            )
        )
        return await original(tenant_id, doc_id, run_seq)

    monkeypatch.setattr(storage.documents, "is_current_run", reupload)
    # the v1 run is superseded by the registration (status left PROCESSING -> PENDING)
    with pytest.raises(StaleRunError):
        await pipeline.ingest(source("h.docx", v1))
    monkeypatch.setattr(storage.documents, "is_current_run", original)
    result = await pipeline.ingest(source("h.docx", v2))  # the queued v2 task
    assert not result.skipped and result.status is IngestStatus.READY
    assert (await storage.documents.get("t1", "h.docx")).sha256 == source("h.docx", v2).sha256


async def test_metadata_change_reingests(pipeline, storage, files):
    await pipeline.ingest(source("runbook.md", files["runbook.md"], metadata={"tags": ["a"]}))
    res = await pipeline.ingest(source("runbook.md", files["runbook.md"], metadata={"tags": ["b"]}))
    assert not res.skipped and res.embedded == 0  # same text: vectors reused
    assert {t for c, _ in storage.chunks.rows.values() for t in c.metadata["tags"]} == {"b"}


async def test_delete_during_run_leaves_no_orphans(pipeline, storage, files, monkeypatch):
    original = storage.chunks.replace_document_chunks

    async def delete_first(tenant_id, doc_id, run_seq, chunks):
        await pipeline.delete(tenant_id, doc_id)  # user deletes while we're writing
        return await original(tenant_id, doc_id, run_seq, chunks)

    monkeypatch.setattr(storage.chunks, "replace_document_chunks", delete_first)
    with pytest.raises(StaleRunError):
        await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    assert await storage.documents.get("t1", "runbook.md") is None
    assert not storage.chunks.rows


async def test_delete(pipeline, storage, files):
    await pipeline.ingest(source("runbook.md", files["runbook.md"]))
    removed = await pipeline.delete("t1", "runbook.md")
    assert removed > 0 and not storage.chunks.rows
    assert await storage.documents.get("t1", "runbook.md") is None


async def test_runtime_in_memory(files, tmp_path):
    from forge_tasks.tasks import JobSpec

    from .conftest import documents_runtime, offline_models

    rt = documents_runtime(models=offline_models(rerank={"provider": "overlap"}))
    await rt.ensure_schema()
    path = tmp_path / "people.csv"
    path.write_bytes(files["people.csv"])
    res = await rt.submit(
        JobSpec(task_type="documents", kind="ingest", payload={"tenant_id": "t1", "doc_id": "p", "uri": str(path)})
    )
    assert res.status.value == "ok"
    resp = await rt.documents.search.search(SearchQuery(tenant_ids=["t1"], text="who works in Oslo"))
    assert "Oslo" in resp.hits[0].chunk.text
    await rt.close()


def test_s3_client_reads_the_s3_settings():
    from forge_task_documents.config import S3Settings
    from forge_task_documents.task import s3_client

    client = s3_client(
        S3Settings(endpoint_url="http://127.0.0.1:19000", region="us-east-1", access_key_id="k", secret_access_key="s")
    )
    assert client.meta.endpoint_url == "http://127.0.0.1:19000"
    assert client.meta.config.s3 == {"addressing_style": "path"} and client.meta.region_name == "us-east-1"
    assert client._request_signer._credentials.access_key == "k"
    blank = S3Settings(endpoint_url="", region="", access_key_id="", secret_access_key="")
    assert (blank.endpoint_url, blank.region, blank.access_key_id, blank.secret_access_key) == (None,) * 4


async def test_mermaid_diagram_is_searchable(pipeline, search_service):
    result = await pipeline.ingest(source("order-flow.mmd", fixtures.make_mermaid()))
    assert result.status is IngestStatus.READY and result.chunk_count > 0
    resp = await search_service.search(
        SearchQuery(tenant_ids=["t1"], text="what happens when an order isn't valid", rerank=False)
    )
    top = resp.hits[0].chunk
    assert top.source_type == "mermaid" and "[Valid?] --no--> [Reject order]" in top.text
