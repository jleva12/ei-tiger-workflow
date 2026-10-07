"""graph_knowledge_bases: knowledge bases of code repositories

knowledge_bases.kind says what a knowledge base holds: rag, documents
uploaded to it, chunked and embedded by the async worker (every knowledge
base so far); or graph, code repositories, searched in the code graph.
knowledge_base_repositories is what a graph knowledge base includes: the
organization's code repositories (code_repositories), each in any number of
knowledge bases. A link goes with either end.

Revision ID: 0016graph_knowledge_bases
Revises: 0015repositories_read
Create Date: 2026-10-07 20:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0016graph_knowledge_bases"
down_revision: str | Sequence[str] | None = "0015repositories_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: When a row's written, to the microsecond (forge_admin.db.base).
NOW = sa.text("CURRENT_TIMESTAMP(6)")


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


def upgrade() -> None:
    # Every knowledge base so far is of documents.
    op.add_column(
        "knowledge_bases",
        sa.Column("kind", ascii_id(16), server_default="rag", nullable=False),
    )
    op.create_table(
        "knowledge_base_repositories",
        sa.Column("knowledge_base_id", ascii_id(36), nullable=False),
        sa.Column("repository_id", ascii_id(36), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint(
            "knowledge_base_id",
            "repository_id",
            name=op.f("pk_knowledge_base_repositories"),
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f(
                "fk_knowledge_base_repositories_knowledge_base_id_knowledge_bases"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["code_repositories.id"],
            name=op.f("fk_knowledge_base_repositories_repository_id_code_repositories"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_base_repositories_repository_id"),
        "knowledge_base_repositories",
        ["repository_id"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_base_repositories")
    op.drop_column("knowledge_bases", "kind")
