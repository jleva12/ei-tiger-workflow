"""Organizations' knowledge bases: named sets of documents (RAG) or code
repositories (graph) their chat agents search, the documents uploaded to
each, the collections they're filed in, and the repositories a graph
knowledge base includes."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, ascii_string

#: A knowledge base of uploaded documents, chunked and embedded by the async
#: worker; and one of code repositories, searched in the code graph.
RAG = "rag"
GRAPH = "graph"
Kind = Literal["rag", "graph"]


def new_id() -> str:
    """:return: A random UUID, the ID of a new knowledge base, document or collection."""
    return str(uuid4())


class KnowledgeBase(AuditBase):
    """
    A named set of an organization's documents, e.g. "HR policies", or of its
    code repositories: its chat agents search it with a knowledge base tool.
    A RAG one is the async worker's tenant for its documents' chunks, so a
    search of it finds only its own; a graph one searches the code graph of
    the repositories it includes (:class:`KnowledgeBaseRepository`).

    :ivar organization_id: The organization.
    :ivar kind: :data:`RAG` or :data:`GRAPH`; it never changes.
    :ivar name: Unique in the organization.
    :ivar description: What it holds, optionally; agents' tools describe it so.
    """

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(ascii_string(16), default=RAG, server_default=RAG)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")


class KnowledgeBaseRepository(AuditBase):
    """
    A code repository a graph knowledge base includes: the organization's
    (``code_repositories``), which other knowledge bases may include too.
    Removing either removes the link; unlinking leaves the repository in the
    organization.

    :ivar knowledge_base_id: The graph knowledge base.
    :ivar repository_id: The repository.
    """

    __tablename__ = "knowledge_base_repositories"

    knowledge_base_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True,
    )
    repository_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("code_repositories.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class KnowledgeCollection(AuditBase):
    """
    A named group of a knowledge base's documents, like a folder: it organizes
    them for people and doesn't change what the agents search. Collections
    nest; deleting one moves what's in it (collections and documents) up to
    its parent.

    :ivar knowledge_base_id: The knowledge base.
    :ivar parent_id: The collection it's in, or None at the top.
    :ivar name: Unique among its siblings.
    :ivar description: What it holds, optionally.
    """

    __tablename__ = "knowledge_collections"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id",
            "parent_id",
            "name",
            name="uq_knowledge_collections_sibling",
        ),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ascii_string(36),
        ForeignKey("knowledge_collections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")


class KnowledgeDocument(AuditBase):
    """
    A file uploaded to a knowledge base: stored in the documents bucket, then
    parsed, chunked and embedded by the async worker's documents task,
    submitted by ``created_by``. The job's progress lives in the worker; this
    row is the knowledge base's record of the upload, with the job's outcome
    as last read from the worker, which reading an unfinished one refreshes.

    :ivar knowledge_base_id: The knowledge base; the worker files the
        document's chunks under it.
    :ivar collection_id: The collection it's filed in, or None when unfiled.
    :ivar filename: The name it was uploaded with.
    :ivar media_type: Its media type, as uploaded.
    :ivar size_bytes: Its size.
    :ivar sha256: Its SHA-256, hex.
    :ivar storage_uri: Where it's stored, ``s3://<bucket>/<key>``.
    :ivar job_key: The worker's ingest job.
    :ivar phase: The job's phase when last read: ``QUEUED``, ``RUNNING``,
        ``SUCCEEDED``, ``FAILED``, or ``MISSING`` when the worker no longer
        has it.
    :ivar error: Why it failed.
    :ivar chunk_count: The chunks it was split into, once it succeeded.
    :ivar embedding_model: What its chunks are embedded with, once it
        succeeded (e.g. ``text-embedding-3-large@1024``); empty when unknown.
    :ivar finished_at: When it finished (UTC), once it has.
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    # Deleting a knowledge base drops its documents' records with it; its
    # routes remove the stored files and embedded chunks first.
    knowledge_base_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
    )
    collection_id: Mapped[str | None] = mapped_column(
        ascii_string(36),
        ForeignKey("knowledge_collections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(255), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(ascii_string(64))
    storage_uri: Mapped[str] = mapped_column(String(1024))
    job_key: Mapped[str] = mapped_column(ascii_string(255))
    phase: Mapped[str] = mapped_column(ascii_string(16), default="QUEUED")
    error: Mapped[str] = mapped_column(String(1000), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str] = mapped_column(String(255), default="")
    finished_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, nullable=True)
