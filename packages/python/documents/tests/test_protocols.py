"""Every shipped documents implementation structurally satisfies its protocol."""

from __future__ import annotations

from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_task_documents import protocols as D
from forge_task_documents.chunking import ChunkEngine, default_strategies
from forge_task_documents.enrichment import (
    BreadcrumbEnricher,
    CompositeEnricher,
    IdentifierEnricher,
    LLMContextEnricher,
)
from forge_task_documents.ingestion import LocalFileLoader, S3Loader
from forge_task_documents.parsers import default_registry
from forge_task_documents.parsers.docling_adapter import DoclingParser
from forge_task_documents.task import DocumentsTaskFactory
from forge_tasks.protocols import Job, Task, TaskFactory

from .conftest import documents_runtime


def test_document_parsers():
    for parser in [*default_registry(load_plugins=False).parsers, DoclingParser()]:
        assert isinstance(parser, D.Parser), parser


def test_chunking():
    assert isinstance(ChunkEngine(HeuristicTokenizer()), D.Chunker)
    for s in default_strategies():
        assert isinstance(s, D.ChunkStrategy), s


def test_enrichers():
    class Dummy:
        model_id = "d"

        async def complete(self, *, system, user, max_tokens=200, cache_system=True, temperature=None):
            return ""

    for e in [BreadcrumbEnricher(), IdentifierEnricher(), LLMContextEnricher(Dummy()), CompositeEnricher([])]:
        assert isinstance(e, D.Enricher), e


def test_loaders():
    assert isinstance(LocalFileLoader(), D.SourceLoader)
    assert isinstance(S3Loader(client=object()), D.SourceLoader)


async def test_the_task_registers_with_the_worker():
    from importlib.metadata import entry_points

    from forge_tasks.tasks import ENTRY_POINT_GROUP

    [ep] = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == "documents"]
    assert ep.load() is DocumentsTaskFactory
    assert isinstance(DocumentsTaskFactory(), TaskFactory)
    rt = documents_runtime()
    try:
        assert isinstance(rt.documents, Task)
        assert set(rt.documents.jobs) == {"ingest", "delete"}
        assert all(isinstance(job, Job) for job in rt.documents.jobs.values())
    finally:
        await rt.close()
