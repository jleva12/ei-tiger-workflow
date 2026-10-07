"""The admin API's knowledge base search (``forge_admin.knowledge.search``):
the documents task's search over the store the worker writes, as the
settings select it."""

import asyncio
from typing import Any

import pytest
from forge_embeddings.config import EmbeddingSettings, RerankSettings
from forge_task_documents.storage.mongo import MongoStorage
from pydantic import SecretStr

from forge_admin.config import Settings
from forge_admin.knowledge.search import KnowledgeSearch, SearchError


def test_it_reads_the_store_the_worker_writes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_VECTOR_STORE", raising=False)
    # Mongo, the default selection, without its URI: not set up.
    assert KnowledgeSearch.from_settings(settings) is None
    configured = settings.model_copy(
        update={
            "mongo_uri": SecretStr("mongodb://127.0.0.1:1"),
            "knowledge_embedding": EmbeddingSettings(provider="hashing", dimensions=64),
            "knowledge_rerank": RerankSettings(provider="overlap"),
        }
    )
    search = KnowledgeSearch.from_settings(configured)
    assert search is not None
    shared = search._search  # noqa: SLF001
    assert isinstance(shared.storage, MongoStorage)
    # The worker's reranker, so chat agents rank as workflows do.
    assert shared.service._reranker is not None  # noqa: SLF001
    asyncio.run(search.aclose())


def test_a_failed_search_is_a_search_error() -> None:
    class Down:
        async def search(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("connection refused")

    search = KnowledgeSearch(Down())  # type: ignore[arg-type]
    with pytest.raises(SearchError, match="connection refused"):
        asyncio.run(search.search(["kb_members", "kb_claims"], "claims"))


def test_it_reads_the_workers_records_and_hides_what_is_removed() -> None:
    from forge_embeddings.embedding import HashingEmbedder
    from forge_task_documents.models import DocumentRecord, IngestStatus
    from forge_task_documents.retrieval import KnowledgeBaseSearch
    from forge_task_documents.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    search = KnowledgeSearch(
        KnowledgeBaseSearch(storage, embedder=HashingEmbedder(dimensions=8))
    )
    record = DocumentRecord(
        doc_id="d1", tenant_id="kb", filename="a.md", sha256="0" * 64, size_bytes=1
    )

    async def main() -> None:
        run = await storage.documents.begin_run(record)
        await storage.documents.complete_run("kb", "d1", run, fields={"chunk_count": 3})
        known = await search.records([("kb", "d1"), ("kb", "nope")])
        assert known[("kb", "nope")] is None
        assert known[("kb", "d1")].status is IngestStatus.READY  # type: ignore[union-attr]

        # Removed: tombstoned, so no search finds it before its delete job runs.
        assert await search.forget("kb", "d1") is True
        assert (await search.records([("kb", "d1")]))[("kb", "d1")] is None

    asyncio.run(main())
