"""system_designs: components and groups on a system design knowledge base's map

A system design knowledge base's map holds more than its applications (code
repositories): knowledge_base_components are the other parts of the system
people add by hand (a database, a queue or topic, a cache, storage, an
external system, a service without a repository, a client), and
knowledge_base_groups are named parts of the system drawn around some of its
applications and components (a domain, a team's services, a network), each
saying what it is and what it's for. An application or a component is in at
most one group (group_id; deleting the group leaves it in none).

A connection's end may now be a component instead of an application:
knowledge_base_connections.source_repository_id and target_repository_id
become nullable, with source_component_id and target_component_id beside
them (deleting a component removes its connections). Downgrading removes
the connections to and from components.

Revision ID: 0019system_designs
Revises: 0018system_maps
Create Date: 2026-10-08 12:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0019system_designs"
down_revision: str | Sequence[str] | None = "0018system_maps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GROUPS = "knowledge_base_groups"
COMPONENTS = "knowledge_base_components"
REPOSITORIES = "knowledge_base_repositories"
CONNECTIONS = "knowledge_base_connections"

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
    op.create_table(
        GROUPS,
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("knowledge_base_id", ascii_id(36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("map_x", sa.Float(), nullable=True),
        sa.Column("map_y", sa.Float(), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_base_groups")),
        sa.UniqueConstraint(
            "knowledge_base_id",
            "name",
            name=op.f("uq_knowledge_base_groups_knowledge_base_id"),
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_base_groups_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        COMPONENTS,
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("knowledge_base_id", ascii_id(36), nullable=False),
        sa.Column("kind", ascii_id(32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("technology", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("group_id", ascii_id(36), nullable=True),
        sa.Column("map_x", sa.Float(), nullable=True),
        sa.Column("map_y", sa.Float(), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_base_components")),
        sa.UniqueConstraint(
            "knowledge_base_id",
            "name",
            name=op.f("uq_knowledge_base_components_knowledge_base_id"),
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_base_components_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            [f"{GROUPS}.id"],
            name=op.f("fk_knowledge_base_components_group_id_knowledge_base_groups"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_base_components_group_id"), COMPONENTS, ["group_id"]
    )

    # Batched, so SQLite (the tests) copies the tables; MySQL alters them.
    # Each index comes before its foreign key, which MySQL would otherwise
    # index itself.
    with op.batch_alter_table(REPOSITORIES) as batch:
        batch.add_column(sa.Column("group_id", ascii_id(36), nullable=True))
        batch.create_index(
            op.f("ix_knowledge_base_repositories_group_id"), ["group_id"]
        )
        batch.create_foreign_key(
            op.f("fk_knowledge_base_repositories_group_id_knowledge_base_groups"),
            GROUPS,
            ["group_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table(CONNECTIONS) as batch:
        for side in ("source", "target"):
            batch.alter_column(
                f"{side}_repository_id", existing_type=ascii_id(36), nullable=True
            )
            batch.add_column(
                sa.Column(f"{side}_component_id", ascii_id(36), nullable=True)
            )
            batch.create_index(
                op.f(f"ix_knowledge_base_connections_{side}_component_id"),
                [f"{side}_component_id"],
            )
            # The convention's name is longer than MySQL's 64 characters.
            batch.create_foreign_key(
                f"fk_knowledge_base_connections_{side}_component",
                COMPONENTS,
                [f"{side}_component_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    # A connection to or from a component has nowhere to go.
    op.execute(
        f"DELETE FROM {CONNECTIONS} "
        "WHERE source_repository_id IS NULL OR target_repository_id IS NULL"
    )
    with op.batch_alter_table(CONNECTIONS) as batch:
        for side in ("source", "target"):
            batch.drop_constraint(
                f"fk_knowledge_base_connections_{side}_component", type_="foreignkey"
            )
            batch.drop_index(op.f(f"ix_knowledge_base_connections_{side}_component_id"))
            batch.drop_column(f"{side}_component_id")
            batch.alter_column(
                f"{side}_repository_id", existing_type=ascii_id(36), nullable=False
            )
    with op.batch_alter_table(REPOSITORIES) as batch:
        batch.drop_constraint(
            op.f("fk_knowledge_base_repositories_group_id_knowledge_base_groups"),
            type_="foreignkey",
        )
        batch.drop_index(op.f("ix_knowledge_base_repositories_group_id"))
        batch.drop_column("group_id")
    op.drop_table(COMPONENTS)
    op.drop_table(GROUPS)
