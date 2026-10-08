"""system_knowledge_bases: graph knowledge bases become system design ones

A knowledge base of code repositories is now a system design knowledge
base (kind system, was graph): its applications (the repositories it
includes), how they connect, and their code graphs.
knowledge_base_connections are the edges of its system map: one of its
applications calling another's API, depending on it, sending it events or
sharing its data, drawn by hand. Both ends are the knowledge base's
repositories, so removing either from it removes the connection.
knowledge_base_code_links say where a connection happens in the code: a node
of each application's code graph, written into the graph as cross-repository
links.

Revision ID: 0017system_knowledge_bases
Revises: 0016graph_knowledge_bases
Create Date: 2026-10-07 23:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0017system_knowledge_bases"
down_revision: str | Sequence[str] | None = "0016graph_knowledge_bases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: When a row's written, to the microsecond (forge_admin.db.base).
NOW = sa.text("CURRENT_TIMESTAMP(6)")
CONNECTIONS = "knowledge_base_connections"
CODE_LINKS = "knowledge_base_code_links"


def ascii_id(length: int) -> sa.String:
    """An ASCII identifier column (forge_admin.db.base.ascii_string)."""
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=length), "mysql"
    )


def timestamp() -> sa.DateTime:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def audit_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", timestamp(), server_default=NOW, nullable=False),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column("updated_at", timestamp(), server_default=NOW, nullable=False),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def end_columns(side: str) -> list[sa.Column[Any]]:
    """One end of a code link: the node, as read when it was linked."""
    return [
        sa.Column(f"{side}_node_id", ascii_id(256), nullable=False),
        sa.Column(f"{side}_kind", sa.String(length=64), nullable=False),
        sa.Column(f"{side}_name", sa.String(length=255), nullable=False),
        sa.Column(f"{side}_qualified_name", sa.Text(), nullable=False),
        sa.Column(f"{side}_path", sa.String(length=1000), nullable=False),
    ]


def upgrade() -> None:
    op.execute("UPDATE knowledge_bases SET kind = 'system' WHERE kind = 'graph'")
    op.create_table(
        CONNECTIONS,
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("knowledge_base_id", ascii_id(36), nullable=False),
        sa.Column("source_repository_id", ascii_id(36), nullable=False),
        sa.Column("target_repository_id", ascii_id(36), nullable=False),
        sa.Column("kind", ascii_id(32), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_base_connections")),
        sa.UniqueConstraint(
            "knowledge_base_id",
            "source_repository_id",
            "target_repository_id",
            "kind",
            name="uq_knowledge_base_connections_ends",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "source_repository_id"],
            [
                "knowledge_base_repositories.knowledge_base_id",
                "knowledge_base_repositories.repository_id",
            ],
            name="fk_knowledge_base_connections_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "target_repository_id"],
            [
                "knowledge_base_repositories.knowledge_base_id",
                "knowledge_base_repositories.repository_id",
            ],
            name="fk_knowledge_base_connections_target",
            ondelete="CASCADE",
        ),
    )
    # The unique key covers the source's foreign key; this, the target's.
    op.create_index(
        "ix_knowledge_base_connections_target",
        CONNECTIONS,
        ["knowledge_base_id", "target_repository_id"],
    )
    op.create_table(
        CODE_LINKS,
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("connection_id", ascii_id(36), nullable=False),
        *end_columns("source"),
        *end_columns("target"),
        sa.Column("label", sa.String(length=500), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_base_code_links")),
        sa.UniqueConstraint(
            "connection_id",
            "source_node_id",
            "target_node_id",
            name="uq_knowledge_base_code_links_nodes",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            [f"{CONNECTIONS}.id"],
            name="fk_knowledge_base_code_links_connection",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table(CODE_LINKS)
    op.drop_table(CONNECTIONS)
    op.execute("UPDATE knowledge_bases SET kind = 'graph' WHERE kind = 'system'")
