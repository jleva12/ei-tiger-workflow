"""code_repositories: organizations' code repositories, the queue of their
ingestions into the code graph, and the permission to manage them

code_repositories holds the GitHub repositories an organization ingests,
each on one branch; the graph itself is the code graph worker's, in Spanner,
one per GitHub URL. code_ingestion_jobs is the worker's queue: the admin API
queues an ingestion here, the code graph worker (apps/forge-codegraph-worker)
claims it with FOR UPDATE SKIP LOCKED, holds it with a lease fenced on its
claim token, and writes its outcome back. A repository goes with its
organization, and its jobs with it.

Adding and removing repositories and starting and retrying their ingestions
needs repositories:manage, granted here to the organization administrator
and member roles as knowledge_bases:manage is. Written once, so later edits
made through the API are never reverted.

Revision ID: 0014code_repositories
Revises: 0013knowledge_models
Create Date: 2026-10-07 12:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0014code_repositories"
down_revision: str | Sequence[str] | None = "0013knowledge_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "repositories", "manage"
DESCRIPTION = (
    "Add and remove the organization's code repositories, and ingest them into "
    "the code graph"
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
        "code_repositories",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("url", ascii_id(300), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_repositories")),
        sa.UniqueConstraint(
            "organization_id", "url", name=op.f("uq_code_repositories_organization_id")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_code_repositories_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_code_repositories_organization_id"),
        "code_repositories",
        ["organization_id"],
    )
    op.create_index(op.f("ix_code_repositories_url"), "code_repositories", ["url"])
    op.create_table(
        "code_ingestion_jobs",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("repository_id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("url", ascii_id(300), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", ascii_id(64), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("queue", ascii_id(128), nullable=False),
        sa.Column("status", ascii_id(16), nullable=False),
        sa.Column("eligible_at", timestamp(), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=False),
        sa.Column("lease_until", timestamp(), nullable=True),
        sa.Column("claim_token", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("codegraph_repository_id", ascii_id(256), nullable=True),
        sa.Column("run_id", ascii_id(256), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.String(length=2048), nullable=False),
        sa.Column(
            "metrics",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=True,
        ),
        sa.Column("started_at", timestamp(), nullable=True),
        sa.Column("finished_at", timestamp(), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_ingestion_jobs")),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["code_repositories.id"],
            name=op.f("fk_code_ingestion_jobs_repository_id_code_repositories"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_code_ingestion_jobs_claim", "code_ingestion_jobs", ["queue", "eligible_at"]
    )
    op.create_index(
        "ix_code_ingestion_jobs_repository",
        "code_ingestion_jobs",
        ["repository_id", "created_at"],
    )
    op.create_index(
        op.f("ix_code_ingestion_jobs_organization_id"),
        "code_ingestion_jobs",
        ["organization_id"],
    )
    op.create_index(
        "ix_code_ingestion_jobs_url", "code_ingestion_jobs", ["url", "status"]
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
    # Every grant of the permission, the defaults and any since, then it.
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
    op.drop_table("code_ingestion_jobs")
    op.drop_table("code_repositories")
