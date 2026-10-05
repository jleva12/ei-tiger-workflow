"""knowledge_bases: organizations' knowledge bases, their documents and
collections, and the permission to manage them

knowledge_bases holds an organization's named sets of documents its chat
agents search; knowledge_documents the files uploaded to each (stored in the
documents bucket; parsed, chunked and embedded by the async worker, whose
job's outcome is kept here as last read); knowledge_collections the nested
folders they're filed in. Each goes with its knowledge base, and a knowledge
base with its organization.

Creating, changing and deleting knowledge bases, and uploading, filing,
retrying and removing their documents, needs knowledge_bases:manage, granted
here to the organization administrator and member roles as mcp_servers:manage
is. Written once, so later edits made through the API are never reverted.

Revision ID: 0008knowledge
Revises: 0007group_links
Create Date: 2026-10-04 12:40:29.887316
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008knowledge"
down_revision: str | Sequence[str] | None = "0007group_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "knowledge_bases", "manage"
DESCRIPTION = (
    "Create, change and delete the organization's knowledge bases, and upload, "
    "file and remove their documents"
)
ROLES = ("org:admin", "org:member")

permissions_table = sa.table(
    "authz_permissions",
    sa.column("resource", sa.String),
    sa.column("action", sa.String),
    sa.column("description", sa.Text),
)
rules_table = sa.table(
    "casbin_rule",
    sa.column("ptype", sa.String),
    sa.column("v0", sa.String),
    sa.column("v1", sa.String),
    sa.column("v2", sa.String),
)


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column(
            "id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_knowledge_bases_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_knowledge_bases_organization_id")
        ),
    )
    op.create_index(
        op.f("ix_knowledge_bases_organization_id"),
        "knowledge_bases",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "knowledge_collections",
        sa.Column(
            "id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_collections_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["knowledge_collections.id"],
            name=op.f("fk_knowledge_collections_parent_id_knowledge_collections"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_collections")),
        sa.UniqueConstraint(
            "knowledge_base_id",
            "parent_id",
            "name",
            name="uq_knowledge_collections_sibling",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_collections_knowledge_base_id"),
        "knowledge_collections",
        ["knowledge_base_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_collections_parent_id"),
        "knowledge_collections",
        ["parent_id"],
        unique=False,
    )
    op.create_table(
        "knowledge_documents",
        sa.Column(
            "id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "collection_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=True,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "sha256",
            sa.String(length=64).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=64),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column(
            "job_key",
            sa.String(length=255).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=255),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.String(length=16).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=16),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("error", sa.String(length=1000), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["knowledge_collections.id"],
            name=op.f("fk_knowledge_documents_collection_id_knowledge_collections"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_documents_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
    )
    op.create_index(
        op.f("ix_knowledge_documents_collection_id"),
        "knowledge_documents",
        ["collection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_documents_knowledge_base_id"),
        "knowledge_documents",
        ["knowledge_base_id"],
        unique=False,
    )
    op.bulk_insert(
        permissions_table,
        [{"resource": RESOURCE, "action": ACTION, "description": DESCRIPTION}],
    )
    op.bulk_insert(
        rules_table,
        [{"ptype": "p", "v0": role, "v1": RESOURCE, "v2": ACTION} for role in ROLES],
    )


def downgrade() -> None:
    # Every grant of the permission goes with it, the defaults and any since.
    op.execute(
        rules_table.delete().where(
            rules_table.c.ptype == "p",
            rules_table.c.v1 == RESOURCE,
            rules_table.c.v2 == ACTION,
        )
    )
    op.execute(
        permissions_table.delete().where(
            permissions_table.c.resource == RESOURCE,
            permissions_table.c.action == ACTION,
        )
    )
    # Each table takes its indexes with it; the documents and collections
    # first, which refer to the knowledge bases.
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_collections")
    op.drop_table("knowledge_bases")
