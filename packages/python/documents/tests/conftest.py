from __future__ import annotations

import pytest

from forge_embeddings.embedding import HashingEmbedder, OverlapReranker
from forge_embeddings.identifiers import IdentifierExtractor
from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_task_documents.chunking import ChunkEngine, ChunkingConfig
from forge_task_documents.enrichment import BreadcrumbEnricher, CompositeEnricher, IdentifierEnricher
from forge_task_documents.ingestion import IngestionPipeline
from forge_task_documents.models import SourceFile
from forge_task_documents.parsers import default_registry
from forge_task_documents.retrieval import HybridSearchService
from forge_task_documents.storage.memory import InMemoryStorage

from . import fixtures


@pytest.fixture(scope="session")
def files() -> dict[str, bytes]:
    return {
        "handbook.docx": fixtures.make_docx(),
        "pricing.xlsx": fixtures.make_xlsx(),
        "runbook.md": fixtures.make_markdown(),
        "incident.txt": fixtures.make_text_latin1(),
        "people.csv": fixtures.make_csv(),
        "order_flow.vsdx": fixtures.make_vsdx(),
        "refund_guide.pdf": fixtures.make_pdf(),
        "review.pptx": fixtures.make_pptx(),
    }


def source(name: str, data: bytes, *, tenant: str = "t1", doc_id: str | None = None, **kw: object) -> SourceFile:
    return SourceFile(tenant_id=tenant, doc_id=doc_id or name, filename=name, data=data, **kw)  # type: ignore[arg-type]


@pytest.fixture
def registry():
    return default_registry(load_plugins=False)


@pytest.fixture
def config() -> ChunkingConfig:
    return ChunkingConfig(
        max_tokens=160, min_tokens=40, overlap_tokens=24, table_max_rows_per_chunk=20, table_summary_min_rows=30
    )


@pytest.fixture
def engine(config: ChunkingConfig) -> ChunkEngine:
    return ChunkEngine(HeuristicTokenizer(), config)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder(dimensions=256)


@pytest.fixture
def pipeline(registry, engine, embedder, storage) -> IngestionPipeline:
    extractor = IdentifierExtractor()
    return IngestionPipeline(
        registry=registry,
        chunker=engine,
        enricher=CompositeEnricher([BreadcrumbEnricher(), IdentifierEnricher(extractor)]),
        embedder=embedder,
        storage=storage,
    )


@pytest.fixture
def search_service(storage, embedder) -> HybridSearchService:
    return HybridSearchService(
        search=storage.search, chunks=storage.chunks, embedder=embedder, reranker=OverlapReranker()
    )


def offline_models(dimensions: int = 64, **sections: object):
    """Model clients that need no network: the hashing embedder, no LLM."""
    from forge_embeddings.clients import build_models
    from forge_embeddings.config import ModelSettings

    values = {
        "embedding": {"provider": "hashing", "dimensions": dimensions},
        "rerank": {"provider": "none"},
        "llm": {"provider": "none"},
        **sections,
    }
    return build_models(ModelSettings(_env_file=None, **values))  # type: ignore[arg-type]


def documents_runtime(*, models=None, extras=None, queue=None, **documents: object):
    """The documents task, built as the worker builds it, on in-memory storage."""
    from forge_embeddings.clients import MODELS_RESOURCE
    from forge_task_documents.config import DocumentsSettings
    from forge_tasks.runtime import build_runtime
    from forge_tasks.settings import CoreSettings

    return build_runtime(
        CoreSettings(_env_file=None, enabled_tasks=["documents"]),  # type: ignore[call-arg]
        options={"documents": DocumentsSettings(**{"storage_backend": "memory", **documents})},  # type: ignore[arg-type]
        resources={MODELS_RESOURCE: models or offline_models()},
        extras=extras,
        queue=queue,
    )
