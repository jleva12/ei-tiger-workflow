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
        """Create/refresh as PENDING, advancing any existing fence. This is
        the only operation that explicitly recreates a tombstoned document."""
        ...

    async def begin_run(self, record: DocumentRecord, *, expected_run_seq: int | None = None) -> int:
        """Mark PROCESSING, bump and return ``run_seq``. Reject tombstones;
        only register explicitly recreates a deleted document. When supplied,
        expected_run_seq must match (0 for missing), including across source loading."""
        ...

    async def is_current_run(self, tenant_id: str, doc_id: str, run_seq: int) -> bool: ...

    async def complete_run(self, tenant_id: str, doc_id: str, run_seq: int, *, fields: dict[str, Any]) -> bool:
        """Atomically mark READY and select published_run_seq=run_seq, only
        if the fence is current. Staged chunks are complete before this call."""
        ...

    async def fail_run(
        self, tenant_id: str, doc_id: str, run_seq: int | None, *, error: str, expected_run_seq: int | None = None
    ) -> bool:
        """Mark FAILED if ``run_seq`` is current. ``None`` fails the record only
        while it is still PENDING (no run claimed it, e.g. the file failed to load),
        and, when provided, its fence still matches expected_run_seq."""
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
        """Tombstone even an unknown ID and advance its fence. Return whether
        a live record existed; pre-registration in-flight runs must also lose."""
        ...


@runtime_checkable
class ChunkStore(Protocol):
    async def get_embeddings_by_hash(self, tenant_id: str, content_hashes: Collection[str]) -> dict[str, list[float]]:
        """Existing vectors for these content hashes (tenant-scoped), for reuse."""
        ...

    async def replace_document_chunks(
        self, tenant_id: str, doc_id: str, run_seq: int, chunks: Sequence[Chunk]
    ) -> ReplaceStats:
        """Stage an immutable generation, retaining public chunk IDs. Existing
        published generations are never overwritten, even for identical IDs.
        DocumentStore.complete_run atomically makes the whole generation visible."""
        ...

    async def publish_run(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        """Collect generations older than the authoritative published pointer.
        Publication already committed in DocumentStore.complete_run; cleanup
        failure is logged, never changes visibility or fails that completed run.

        :return: How many older chunks were removed.
        """
        ...

    async def delete_run_chunks(self, tenant_id: str, doc_id: str, run_seq: int) -> int:
        """Remove what a superseded run wrote and never published."""
        ...

    async def delete_document_chunks(self, tenant_id: str, doc_id: str) -> int:
        """Clean up tombstoned contents without deleting a later recreation."""
        ...

    async def get_chunks(self, tenant_id: str, chunk_ids: Sequence[str]) -> list[Chunk]:
        """Only published chunks, preserving public IDs and requested order."""
        ...

    async def get_section(
        self, tenant_id: str, doc_id: str, section_key: str, *, generation: int | None = None
    ) -> list[Chunk]:
        """Read a published section, restricted to the exact generation when
        supplied. Return no rows if that generation is no longer available."""
        ...


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
