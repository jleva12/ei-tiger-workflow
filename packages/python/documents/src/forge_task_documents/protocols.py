"""Seams of the *documents* task type (files -> chunks). Shared model-client
protocols (Tokenizer, Embedder, Reranker, LLMClient) live in core.protocols.

Implementations never inherit from these; they only need matching members
(structural typing). ``runtime_checkable`` lets tests and the composition root
assert conformance with ``isinstance``.

Seams:
  SourceLoader  -> where bytes come from (local disk, S3, ...)
  Parser        -> one file format -> IR (ParsedDocument)
  ChunkStrategy -> one block kind (prose/table/code/graph) -> chunk drafts
  Chunker       -> ParsedDocument -> Chunks
  Enricher      -> adds breadcrumb / LLM context / identifiers to Chunks
  DocumentStore -> document records (status, hashes, versions)
  ChunkStore    -> chunk persistence + embedding reuse lookups
  SearchBackend -> BM25 + vector + fusion
  StorageBackend-> bundles the three stores so the DB is one swappable unit
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Protocol, runtime_checkable

from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import (
    Chunk,
    DocumentRecord,
    HybridSearchRequest,
    IngestStatus,
    ParsedDocument,
    ReplaceStats,
    SearchHit,
    SourceFile,
)

# --------------------------------------------------------------------------- input


@runtime_checkable
class SourceLoader(Protocol):
    schemes: frozenset[str]  # e.g. {"s3"} or {"file", ""}

    async def load(
        self,
        uri: str,
        *,
        tenant_id: str,
        doc_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SourceFile: ...


@runtime_checkable
class Parser(Protocol):
    """Converts one file format to the IR. Sync on purpose: parsing is CPU-bound
    and the pipeline runs it in a worker thread."""

    name: str
    version: str
    extensions: frozenset[str]
    media_types: frozenset[str]

    def parse(self, source: SourceFile) -> ParsedDocument: ...


# --------------------------------------------------------------------------- chunking


@runtime_checkable
class ChunkStrategy(Protocol):
    block_kind: BlockKind
    version: str  # bump when output changes; feeds the chunker version

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]: ...


@runtime_checkable
class Chunker(Protocol):
    @property
    def version(self) -> str:
        """Changes whenever output could change (config, strategy versions)."""
        ...

    def chunk(self, doc: ParsedDocument) -> list[Chunk]: ...


# --------------------------------------------------------------------------- enrichment / models


@runtime_checkable
class Enricher(Protocol):
    async def enrich(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]: ...


# --------------------------------------------------------------------------- storage


@runtime_checkable
class DocumentStore(Protocol):
    """Document records + the run fence.

    Every ingest run gets a *fencing token* ``run_seq``: a per-document counter
    that only ever increases (deletes leave a tombstone so it never resets).
    Writes tagged with an older token than the current one are rejected or
    cleaned up, so a slow/stale worker can never overwrite a newer version.
    """

    async def get(self, tenant_id: str, doc_id: str) -> DocumentRecord | None:
        """The record, or None if missing or deleted."""
        ...

    async def register(self, record: DocumentRecord) -> None:
        """Create/refresh the record as PENDING (called by the API at upload time)."""
        ...

    async def begin_run(self, record: DocumentRecord) -> int:
        """Mark PROCESSING, bump and return the fencing token ``run_seq``."""
        ...

    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool: ...

    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        """Mark READY + set fields, only if ``run_seq`` is still current."""
        ...

    async def fail_run(self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str) -> bool:
        """Mark FAILED if ``run_seq`` is current. ``None`` fails the record only
        while it is still PENDING (no run claimed it, e.g. the file failed to load)."""
        ...

    async def list(
        self,
        tenant_id: str,
        *,
        status: IngestStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentRecord]: ...

    async def delete(self, tenant_id: str, doc_id: str) -> bool:
        """Tombstone the record and advance the fence so in-flight runs lose."""
        ...


@runtime_checkable
class ChunkStore(Protocol):
    async def get_embeddings_by_hash(self, tenant_id: str, content_hashes: Collection[str]) -> dict[str, list[float]]:
        """Existing vectors for these content hashes (tenant-scoped), for reuse."""
        ...

    async def replace_document_chunks(
        self, tenant_id: str, doc_id: str, run_seq: int, chunks: Sequence[Chunk]
    ) -> ReplaceStats:
        """Upsert ``chunks`` tagged ``run_seq`` without overwriting chunks owned
        by a newer run, then delete the doc's chunks from older runs."""
        ...

    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        """Remove what a superseded run wrote."""
        ...

    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int: ...

    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]: ...

    async def get_section(self, tenant_id: str, doc_id: str, section_key: str) -> list[Chunk]: ...


@runtime_checkable
class SearchBackend(Protocol):
    async def hybrid_search(self, request: HybridSearchRequest) -> list[SearchHit]: ...

    # The single legs are exposed for offline evaluation and debugging.
    async def text_search(self, request: HybridSearchRequest) -> list[SearchHit]: ...

    async def vector_search(self, request: HybridSearchRequest) -> list[SearchHit]: ...


@runtime_checkable
class StorageBackend(Protocol):
    # read-only properties so implementations can expose concrete subtypes
    @property
    def documents(self) -> DocumentStore: ...

    @property
    def chunks(self) -> ChunkStore: ...

    @property
    def search(self) -> SearchBackend: ...

    async def ensure_schema(self, *, embedding_dimensions: int) -> None:
        """Create collections/tables and indexes. Must be idempotent."""
        ...

    async def close(self) -> None: ...
