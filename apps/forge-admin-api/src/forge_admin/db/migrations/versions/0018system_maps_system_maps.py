"""system_maps: where a system design knowledge base's applications are

A system design knowledge base's map is the same for everyone who opens it:
each application's place (knowledge_base_repositories.map_x and map_y,
NULL until it's placed) and the layout last picked
(knowledge_bases.map_layout, NULL until it's arranged). An application
removed from the knowledge base takes its place with it.

Revision ID: 0018system_maps
Revises: 0017system_knowledge_bases
Create Date: 2026-10-08 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0018system_maps"
down_revision: str | Sequence[str] | None = "0017system_knowledge_bases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def ascii_id(length: int) -> sa.String:
    """An ASCII identifier column (forge_admin.db.base.ascii_string)."""
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=length), "mysql"
    )


def upgrade() -> None:
    op.add_column(
        "knowledge_bases", sa.Column("map_layout", ascii_id(16), nullable=True)
    )
    op.add_column(
        "knowledge_base_repositories", sa.Column("map_x", sa.Float(), nullable=True)
    )
    op.add_column(
        "knowledge_base_repositories", sa.Column("map_y", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("knowledge_base_repositories", "map_y")
    op.drop_column("knowledge_base_repositories", "map_x")
    op.drop_column("knowledge_bases", "map_layout")
