"""Organizations' knowledge bases: named sets of documents (RAG) or of a
system's applications (system design) their chat agents search, the
documents uploaded to each, the collections they're filed in, and a system
design knowledge base's applications (code repositories), how they connect
and where in their code."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, ascii_string

#: A knowledge base of uploaded documents, chunked and embedded by the async
#: worker; and a system design one: applications (code repositories), the
#: connections between them, and their code, searched in the code graph.
RAG = "rag"
SYSTEM = "system"
Kind = Literal["rag", "system"]


def new_id() -> str:
    """:return: A random UUID, the ID of a new knowledge base, document or collection."""
    return str(uuid4())


class KnowledgeBase(AuditBase):
    """
    A named set of an organization's documents, e.g. "HR policies", or of its
    applications: its chat agents search it with a knowledge base tool. A
    RAG one is the async worker's tenant for its documents' chunks, so a
    search of it finds only its own; a system design one searches the code
    graph of the repositories it includes (:class:`KnowledgeBaseRepository`),
    and tells its agents how they connect (:class:`KnowledgeBaseConnection`).

    :ivar organization_id: The organization.
    :ivar kind: :data:`RAG` or :data:`SYSTEM`; it never changes.
    :ivar name: Unique in the organization.
    :ivar description: What it holds, optionally; agents' tools describe it so.
    :ivar map_layout: A system design one's map layout, as last picked.
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
    # A system design one's map layout (:data:`MAP_LAYOUTS`), as last
    # picked; None until someone arranges it.
    map_layout: Mapped[str | None] = mapped_column(ascii_string(16), nullable=True)


class KnowledgeBaseRepository(AuditBase):
    """
    An application a system design knowledge base includes: one of the
    organization's code repositories (``code_repositories``), which other
    knowledge bases may include too. Removing either removes the link, and
    the knowledge base's connections to and from it; unlinking leaves the
    repository in the organization.

    :ivar knowledge_base_id: The system design knowledge base.
    :ivar repository_id: The repository.
    :ivar map_x: Where it is on the knowledge base's system map, as people
        left it; None until it's placed.
    :ivar map_y: The same, down.
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
    map_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    map_y: Mapped[float | None] = mapped_column(Float, nullable=True)


#: How a system map arranges applications it has no place for: force-directed,
#: in layers along the connections, or in a circle.
MAP_LAYOUTS = ("cose", "breadthfirst", "circle")
MapLayout = Literal["cose", "breadthfirst", "circle"]


class KnowledgeBaseConnection(AuditBase):
    """
    How one of a system design knowledge base's applications connects to
    another: calling its API, depending on it, sending it events or sharing
    its data. People draw them on the knowledge base's system map; ingestion
    doesn't find them. Both ends must be in the knowledge base: removing
    either from it removes the connection.

    :ivar knowledge_base_id: The system design knowledge base.
    :ivar source_repository_id: The application it's from, e.g. the caller.
    :ivar target_repository_id: The application it's to, e.g. the one called.
    :ivar kind: One of :data:`CONNECTION_KINDS`.
    :ivar description: A note on it, e.g. which API; may be empty.
    """

    __tablename__ = "knowledge_base_connections"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id",
            "source_repository_id",
            "target_repository_id",
            "kind",
            name="uq_knowledge_base_connections_ends",
        ),
        ForeignKeyConstraint(
            ["knowledge_base_id", "source_repository_id"],
            [
                "knowledge_base_repositories.knowledge_base_id",
                "knowledge_base_repositories.repository_id",
            ],
            name="fk_knowledge_base_connections_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["knowledge_base_id", "target_repository_id"],
            [
                "knowledge_base_repositories.knowledge_base_id",
                "knowledge_base_repositories.repository_id",
            ],
            name="fk_knowledge_base_connections_target",
            ondelete="CASCADE",
        ),
        # The unique key covers the source's foreign key; this, the target's.
        Index(
            "ix_knowledge_base_connections_target",
            "knowledge_base_id",
            "target_repository_id",
        ),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(ascii_string(36))
    source_repository_id: Mapped[str] = mapped_column(ascii_string(36))
    target_repository_id: Mapped[str] = mapped_column(ascii_string(36))
    kind: Mapped[str] = mapped_column(ascii_string(32))
    description: Mapped[str] = mapped_column(String(1000), default="")


#: How one application connects to another: unspecified, calling its API,
#: using it as a library, sending it events or messages, or sharing a
#: database or storage with it.
CONNECTION_KINDS = ("connects_to", "calls", "depends_on", "events", "shares_data")
ConnectionKind = Literal["connects_to", "calls", "depends_on", "events", "shares_data"]


class KnowledgeBaseCodeLink(AuditBase):
    """
    Where a connection happens in the code: a node of each application's
    code graph, such as the client method that calls an API and the handler
    serving it. The knowledge base's code links are written into the code
    graph as cross-repository links (owner ``kb:<id>``), which its queries
    follow from one repository into the other. Each end keeps the node's
    kind, names and file as read when it was linked.

    :ivar connection_id: The connection.
    :ivar label: What connects them, e.g. ``POST /v1/orders``; may be empty.
    """

    __tablename__ = "knowledge_base_code_links"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "source_node_id",
            "target_node_id",
            name="uq_knowledge_base_code_links_nodes",
        ),
        # The convention's name is longer than MySQL's 64 characters.
        ForeignKeyConstraint(
            ["connection_id"],
            ["knowledge_base_connections.id"],
            name="fk_knowledge_base_code_links_connection",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ascii_string(36))
    source_node_id: Mapped[str] = mapped_column(ascii_string(256))
    source_kind: Mapped[str] = mapped_column(String(64), default="")
    source_name: Mapped[str] = mapped_column(String(255), default="")
    source_qualified_name: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str] = mapped_column(String(1000), default="")
    target_node_id: Mapped[str] = mapped_column(ascii_string(256))
    target_kind: Mapped[str] = mapped_column(String(64), default="")
    target_name: Mapped[str] = mapped_column(String(255), default="")
    target_qualified_name: Mapped[str] = mapped_column(Text, default="")
    target_path: Mapped[str] = mapped_column(String(1000), default="")
    label: Mapped[str] = mapped_column(String(500), default="")


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
