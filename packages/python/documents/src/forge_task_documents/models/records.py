from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class IngestStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"  # tombstone: keeps the run fence monotonic


class DocumentRecord(BaseModel):
    """One row per source file. Chunks reference it by (tenant_id, doc_id)."""

    doc_id: str
    tenant_id: str
    filename: str
    media_type: str | None = None
    source_uri: str | None = None
    sha256: str
    size_bytes: int
    title: str | None = None
    source_type: str | None = None
    status: IngestStatus = IngestStatus.PENDING
    error: str | None = None
    chunk_count: int = 0
    parser_name: str | None = None
    parser_version: str | None = None
    chunker_version: str | None = None
    embedding_model: str | None = None
    run_seq: int = 0  # fencing token of the latest run
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    ingested_at: datetime | None = None


class ReplaceStats(BaseModel):
    upserted: int
    deleted: int


class IngestResult(BaseModel):
    tenant_id: str
    doc_id: str
    status: IngestStatus
    skipped: bool = False
    reason: str | None = None
    chunk_count: int = 0
    embedding_model: str | None = None  # what its chunks are embedded with
    embedded: int = 0
    reused_embeddings: int = 0
    deleted_stale: int = 0
    timings_ms: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
