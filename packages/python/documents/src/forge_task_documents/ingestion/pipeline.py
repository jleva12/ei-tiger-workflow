"""IngestionPipeline: SourceFile -> parse -> chunk -> enrich -> embed -> store.

Guarantees:
- Idempotent: the same bytes + chunker + embedding model are skipped.
- Cheap re-ingest: embeddings are reused by content hash (tenant-wide), so an
  edited document only pays for the chunks that actually changed.
- Newest run wins: each run takes a fencing token (``run_seq``, monotonic per
  document). Chunk writes never overwrite a newer run's chunks, stale runs
  can't mark the document READY, and they clean up what they wrote.
- Every component is injected via its protocol; nothing here knows about
  file formats or databases.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from forge_embeddings.embedding.stage import EmbeddingStage
from forge_task_documents.errors import StaleRunError
from forge_task_documents.models import Chunk, DocumentRecord, IngestResult, IngestStatus, SourceFile
from forge_tasks.errors import TaskError

if TYPE_CHECKING:
    from forge_embeddings.protocols import Embedder
    from forge_task_documents.parsers.registry import ParserRegistry
    from forge_task_documents.protocols import Chunker, Enricher, StorageBackend

log = logging.getLogger(__name__)


class PipelineConfig(BaseModel):
    # document metadata keys copied onto every chunk (so they're filterable)
    propagate_metadata: tuple[str, ...] = ("tags",)
    max_chunks_per_document: int = 20_000


class IngestionPipeline:
    def __init__(
        self,
        *,
        registry: ParserRegistry,
        chunker: Chunker,
        enricher: Enricher,
        embedder: Embedder,
        storage: StorageBackend,
        config: PipelineConfig | None = None,
    ) -> None:
        self.registry = registry
        self.chunker = chunker
        self.enricher = enricher
        self.embedder = embedder
        self.storage = storage
        self.config = config or PipelineConfig()

    async def ingest(self, source: SourceFile, *, force: bool = False) -> IngestResult:
        tenant_id, doc_id = source.tenant_id, source.doc_id
        timings: dict[str, float] = {}
        docs = self.storage.documents

        existing = await docs.get(tenant_id, doc_id)
        if not force and self._is_unchanged(existing, source):
            return IngestResult(
                tenant_id=tenant_id,
                doc_id=doc_id,
                status=IngestStatus.READY,
                skipped=True,
                reason="unchanged",
                chunk_count=existing.chunk_count if existing else 0,
                embedding_model=existing.embedding_model if existing else None,
            )

        identity: dict[str, object] = {
            "filename": source.filename,
            "media_type": source.media_type,
            "source_uri": source.uri,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "metadata": source.metadata,
        }
        record = DocumentRecord(
            doc_id=doc_id,
            tenant_id=tenant_id,
            filename=source.filename,
            media_type=source.media_type,
            source_uri=source.uri,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            metadata=source.metadata,
        )
        run_seq = await docs.begin_run(record)
        log_ctx = {"tenant_id": tenant_id, "doc_id": doc_id, "run_seq": run_seq}
        log.info("ingest start", extra=log_ctx)
        wrote = False

        try:
            with _timer(timings, "parse_ms"):
                parser = self.registry.resolve(source)
                parsed = await asyncio.to_thread(parser.parse, source)
            if source.metadata:
                parsed.metadata = {**parsed.metadata, **source.metadata}

            with _timer(timings, "chunk_ms"):
                chunks = await asyncio.to_thread(self.chunker.chunk, parsed)
                if len(chunks) > self.config.max_chunks_per_document:
                    raise TaskError(
                        f"{len(chunks)} chunks exceeds max_chunks_per_document={self.config.max_chunks_per_document}"
                    )
                self._propagate_metadata(chunks, source.metadata)

            with _timer(timings, "enrich_ms"):
                chunks = await self.enricher.enrich(parsed, chunks)

            with _timer(timings, "embed_ms"):
                embedded, reused = await self._embed(tenant_id, chunks)

            # cheap early exit; correctness comes from the run_seq fence below
            if not await docs.is_current_run(tenant_id, doc_id, run_seq):
                raise StaleRunError(f"run {run_seq} superseded before write")

            with _timer(timings, "store_ms"):
                wrote = True
                stats = await self.storage.chunks.replace_document_chunks(tenant_id, doc_id, run_seq, chunks)

            completed = await docs.complete_run(
                tenant_id,
                doc_id,
                run_seq,
                fields={
                    **identity,  # what this run actually indexed
                    "title": parsed.title,
                    "source_type": parsed.source_type,
                    "chunk_count": len(chunks),
                    "parser_name": parsed.parser_name,
                    "parser_version": parsed.parser_version,
                    "chunker_version": self.chunker.version,
                    "embedding_model": self.embedder.model_id,
                },
            )
            if not completed:
                raise StaleRunError(f"run {run_seq} superseded during write")
        except Exception as exc:
            if isinstance(exc, StaleRunError):
                if wrote:  # remove anything we wrote that a newer run won't overwrite
                    await self.storage.chunks.delete_run_chunks(tenant_id, doc_id, run_seq)
            else:
                await docs.fail_run(tenant_id, doc_id, run_seq, error=f"{type(exc).__name__}: {exc}")
            log.warning("ingest failed", extra=log_ctx, exc_info=True)
            raise

        result = IngestResult(
            tenant_id=tenant_id,
            doc_id=doc_id,
            status=IngestStatus.READY,
            chunk_count=len(chunks),
            embedding_model=self.embedder.model_id,
            embedded=embedded,
            reused_embeddings=reused,
            deleted_stale=stats.deleted,
            timings_ms=timings,
        )
        log.info("ingest done", extra=result.model_dump(mode="json"))
        return result

    async def delete(self, tenant_id: str, doc_id: str) -> int:
        # Tombstone first: advances the fence so an in-flight run can't complete
        # (it then cleans up its own writes), then drop the chunks.
        await self.storage.documents.delete(tenant_id, doc_id)
        return await self.storage.chunks.delete_document_chunks(tenant_id, doc_id)

    # ------------------------------------------------------------------ helpers

    def _is_unchanged(self, existing: DocumentRecord | None, source: SourceFile) -> bool:
        return (
            existing is not None
            and existing.status == IngestStatus.READY
            and existing.sha256 == source.sha256
            and existing.chunker_version == self.chunker.version
            and existing.embedding_model == self.embedder.model_id
            and existing.metadata == source.metadata  # tags etc. are copied onto chunks
            and self._same_parser(existing, source)
        )

    def _same_parser(self, existing: DocumentRecord, source: SourceFile) -> bool:
        """Whether the parser that would read it now read it last: a new
        parser, or a new version of it, reads it again. A converting parser
        records itself first ("office+pptx")."""
        try:
            parser = self.registry.resolve(source)
        except Exception:  # unsupported now: the run says so
            return False
        recorded = ((existing.parser_name or "").split("+")[0], (existing.parser_version or "").split("+")[0])
        return recorded == (parser.name, parser.version)

    def _propagate_metadata(self, chunks: Iterable[Chunk], metadata: dict[str, object]) -> None:
        carried = {k: metadata[k] for k in self.config.propagate_metadata if k in metadata}
        if carried:
            for c in chunks:
                c.metadata.update(carried)

    async def _embed(self, tenant_id: str, chunks: list[Chunk]) -> tuple[int, int]:
        stats = await EmbeddingStage(self.embedder).embed(
            chunks, lookup=lambda hashes: self.storage.chunks.get_embeddings_by_hash(tenant_id, hashes)
        )
        return stats.embedded, stats.reused


class _timer:
    def __init__(self, sink: dict[str, float], key: str) -> None:
        self.sink, self.key = sink, key

    def __enter__(self) -> None:
        self.t0 = time.perf_counter()

    def __exit__(self, *exc: object) -> None:
        self.sink[self.key] = round((time.perf_counter() - self.t0) * 1000, 2)
