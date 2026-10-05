"""Documents task type: plugs the file pipeline into the task framework.

Jobs
  ingest  {tenant_id, doc_id, uri, ...}   load -> parse -> chunk -> embed -> store
  delete  {tenant_id, doc_id}             tombstone + remove chunks
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from forge_embeddings.clients import model_clients
from forge_embeddings.identifiers import IdentifierExtractor
from forge_embeddings.vector_store import selected_backend
from forge_task_documents.chunking import ChunkEngine
from forge_task_documents.config import DocumentsSettings, S3Settings
from forge_task_documents.enrichment import (
    BreadcrumbEnricher,
    CompositeEnricher,
    IdentifierEnricher,
    LLMContextEnricher,
)
from forge_task_documents.errors import StaleRunError
from forge_task_documents.ingestion import IngestionPipeline, LoaderRouter, LocalFileLoader
from forge_task_documents.parsers import (
    OfficeConverter,
    OfficeConvertingParser,
    ParserRegistry,
    PdfParser,
    TesseractOcr,
    default_registry,
)
from forge_task_documents.retrieval import HybridSearchService
from forge_tasks.errors import TaskError
from forge_tasks.runner import ok, skipped
from forge_tasks.settings import load_section
from forge_tasks.tasks import JobResult, JobStatus, Schedule, TaskContext

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder
    from forge_task_documents.protocols import Enricher, StorageBackend
    from forge_tasks.protocols import Job

log = logging.getLogger(__name__)

TASK_NAME = "documents"

# ----------------------------------------------------------------------------- storage backends

DocumentStorageFactory = Callable[[TaskContext, DocumentsSettings], "StorageBackend"]
_STORAGE: dict[str, DocumentStorageFactory] = {}


def register_document_storage(name: str, factory: DocumentStorageFactory) -> None:
    """Swap the documents database: implement the storage protocols, register here,
    set ``HYBRID_DOCUMENTS__STORAGE_BACKEND=<name>``."""
    _STORAGE[name] = factory


def _mongo(ctx: TaskContext, s: DocumentsSettings) -> StorageBackend:
    from forge_task_documents.storage.mongo import MongoStorage

    return MongoStorage(
        ctx.mongo(),
        database=ctx.settings.mongo.database,
        documents_collection=s.documents_collection,
        chunks_collection=s.chunks_collection,
        text_index=s.text_index,
        vector_index=s.vector_index,
        vector_similarity=s.vector_similarity,
        vector_quantization=s.vector_quantization,
        language_analyzer=s.language_analyzer,
        binary_vectors=s.binary_vectors,
        fusion_mode=s.fusion_mode,
        owns_client=False,
    )


def _memory(ctx: TaskContext, s: DocumentsSettings) -> StorageBackend:
    from forge_task_documents.storage.memory import InMemoryStorage

    return InMemoryStorage()


def _spanner(ctx: TaskContext, s: DocumentsSettings) -> StorageBackend:
    from forge_embeddings.vector_store.spanner import Database
    from forge_task_documents.storage.spanner import SpannerStorage

    database = ctx.shared("vector.spanner", Database)
    return SpannerStorage(database.db, vector_similarity=s.vector_similarity)


register_document_storage("mongo", _mongo)
register_document_storage("memory", _memory)
register_document_storage("spanner", _spanner)


def s3_client(s3: S3Settings) -> Any:
    """A boto3 S3 client for the s3 settings; boto3's defaults fill the rest."""
    import boto3
    from botocore.config import Config

    options: dict[str, Any] = {}
    if s3.endpoint_url:
        # S3-compatible servers are addressed by path, not bucket subdomains.
        options.update(endpoint_url=s3.endpoint_url, config=Config(s3={"addressing_style": "path"}))
    if s3.region:
        options["region_name"] = s3.region
    if s3.access_key_id and s3.secret_access_key:
        options.update(
            aws_access_key_id=s3.access_key_id, aws_secret_access_key=s3.secret_access_key.get_secret_value()
        )
    return boto3.client("s3", **options)


# ----------------------------------------------------------------------------- jobs


class IngestFilePayload(BaseModel):
    tenant_id: str
    doc_id: str
    uri: str
    filename: str | None = None
    media_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class DeleteFilePayload(BaseModel):
    tenant_id: str
    doc_id: str


class IngestFileJob:
    name = "ingest"
    payload_model = IngestFilePayload

    def __init__(self, task: DocumentsTask) -> None:
        self.task = task

    def lock_key(self, payload: IngestFilePayload) -> str:
        return f"{payload.tenant_id}:{payload.doc_id}"

    def describe(self, payload: IngestFilePayload) -> str:
        return payload.filename or payload.uri.rsplit("/", 1)[-1] or payload.doc_id

    async def run(self, payload: IngestFilePayload) -> JobResult:
        t = self.task
        try:
            source = await t.loader.load(
                payload.uri,
                tenant_id=payload.tenant_id,
                doc_id=payload.doc_id,
                filename=payload.filename,
                media_type=payload.media_type,
                metadata=payload.metadata,
            )
        except (FileNotFoundError, ValueError, TaskError) as exc:
            if isinstance(exc, TaskError) and not exc.permanent:
                raise
            # no run exists yet: fail the record only if it's still PENDING
            await t.storage.documents.fail_run(payload.tenant_id, payload.doc_id, None, error=f"load failed: {exc}")
            return JobResult.failed(f"load failed: {exc}", doc_id=payload.doc_id)
        try:
            result = await t.pipeline.ingest(source, force=payload.force)
        except StaleRunError:
            return JobResult(status=JobStatus.SUPERSEDED, detail={"doc_id": payload.doc_id})
        detail = result.model_dump(mode="json", exclude={"tenant_id"})
        if result.skipped:
            return skipped(result.reason or "skipped", **detail)
        return ok(**detail)


class DeleteFileJob:
    name = "delete"
    payload_model = DeleteFilePayload

    def __init__(self, task: DocumentsTask) -> None:
        self.task = task

    def lock_key(self, payload: DeleteFilePayload) -> str:
        return f"{payload.tenant_id}:{payload.doc_id}"

    async def describe(self, payload: DeleteFilePayload) -> str:
        record = await self.task.storage.documents.get(payload.tenant_id, payload.doc_id)
        return record.filename if record is not None else payload.doc_id

    async def run(self, payload: DeleteFilePayload) -> JobResult:
        removed = await self.task.pipeline.delete(payload.tenant_id, payload.doc_id)
        return ok(doc_id=payload.doc_id, chunks_removed=removed)


# ----------------------------------------------------------------------------- task


class DocumentsTask:
    name = TASK_NAME
    queue = TASK_NAME

    def __init__(
        self,
        *,
        settings: DocumentsSettings,
        storage: StorageBackend,
        pipeline: IngestionPipeline,
        search: HybridSearchService,
        loader: LoaderRouter,
        embedder: Embedder,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.pipeline = pipeline
        self.search = search
        self.loader = loader
        self.embedder = embedder
        self._jobs: dict[str, Job] = {j.name: j for j in (IngestFileJob(self), DeleteFileJob(self))}

    @property
    def jobs(self) -> Mapping[str, Job]:
        return self._jobs

    async def ensure_schema(self) -> None:
        await self.storage.ensure_schema(embedding_dimensions=self.embedder.dimensions)

    async def close(self) -> None:
        await self.storage.close()


def _search_service(
    storage: StorageBackend, embedder: Any, reranker: Any, extractor: IdentifierExtractor, s: DocumentsSettings
) -> HybridSearchService:
    return HybridSearchService(
        search=storage.search,
        chunks=storage.chunks,
        embedder=embedder,
        reranker=reranker,
        identifier_extractor=extractor,
        config=s.search,
    )


def build_search(ctx: TaskContext, settings: DocumentsSettings | None = None) -> HybridSearchService:
    """The documents' hybrid search on its own, over the same storage and
    models as the documents task (``HYBRID_DOCUMENTS__*``): what another task
    searches a team's documents with (a workflow's Search knowledge step)."""
    s = settings or load_section(TASK_NAME, DocumentsSettings)
    models = model_clients(ctx)
    storage = ctx.extras.get("documents.storage") or _STORAGE[selected_backend(s.storage_backend)](ctx, s)
    return _search_service(storage, models.embedder(s.embedding_profile), models.reranker, IdentifierExtractor(), s)


class DocumentsTaskFactory:
    name = TASK_NAME
    queue = TASK_NAME
    schedules: list[Schedule] = []
    settings_model = DocumentsSettings
    cli_name = "docs"

    def prepare(self) -> None:
        """The tokenizer files, into the image."""
        from forge_embeddings.clients import prepare as prepare_models

        prepare_models()

    def add_cli(self, parser: Any) -> None:
        from forge_task_documents.cli import add_commands

        add_commands(parser)

    async def run_cli(self, args: Any, runtime: Any) -> None:
        from forge_task_documents.cli import run_command

        await run_command(args, runtime)

    def build(self, ctx: TaskContext) -> DocumentsTask:
        s: DocumentsSettings = ctx.options or DocumentsSettings()
        models = model_clients(ctx)
        backend = selected_backend(s.storage_backend)
        if backend not in _STORAGE:
            raise ValueError(f"unknown documents storage {backend!r}; registered: {sorted(_STORAGE)}")
        storage = ctx.extras.get("documents.storage") or _STORAGE[backend](ctx, s)
        embedder = models.embedder(s.embedding_profile)
        extractor = IdentifierExtractor()
        enrichers: list[Enricher] = []
        if s.context.enabled:
            enrichers.append(LLMContextEnricher(models.require_llm(), concurrency=s.context.concurrency))
        enrichers += [BreadcrumbEnricher(), IdentifierEnricher(extractor)]
        pipeline = IngestionPipeline(
            registry=self._registry(s),
            chunker=ChunkEngine(models.tokenizer, s.chunking),
            enricher=CompositeEnricher(enrichers),
            embedder=embedder,
            storage=storage,
            config=s.pipeline,
        )
        search = _search_service(storage, embedder, models.reranker, extractor, s)
        loaders: list[Any] = [LocalFileLoader(max_bytes=s.max_file_bytes)]
        try:
            import boto3  # noqa: F401

            from forge_task_documents.ingestion import S3Loader

            s3 = ctx.extras.get("documents.s3") or load_section("s3", S3Settings)
            loaders.append(S3Loader(client=s3_client(s3), max_bytes=s.max_file_bytes))
        except ImportError:
            pass
        return DocumentsTask(
            settings=s,
            storage=storage,
            pipeline=pipeline,
            search=search,
            loader=LoaderRouter(loaders),
            embedder=embedder,
        )

    @staticmethod
    def _registry(s: DocumentsSettings) -> ParserRegistry:
        registry = default_registry()
        if s.pdf_parser == "pdfplumber":
            ocr = (
                TesseractOcr.find(
                    s.ocr.command,
                    languages=s.ocr.languages,
                    timeout_s=s.ocr.page_timeout_s,
                    min_confidence=s.ocr.min_confidence,
                )
                if s.ocr.enabled
                else None
            )
            registry.register(PdfParser(ocr=ocr, ocr_dpi=s.ocr.dpi, max_ocr_pages=s.ocr.max_pages))
        converter = OfficeConverter.find(s.office.command, timeout_s=s.office.timeout_s) if s.office.enabled else None
        registry.register(OfficeConvertingParser(converter, registry.resolve))
        docling = {ext for ext, parser in (("docx", s.docx_parser), ("pdf", s.pdf_parser)) if parser == "docling"}
        if docling:
            from forge_task_documents.parsers.docling_adapter import DoclingParser

            registry.register(DoclingParser(extensions=frozenset(docling)))
        return registry
