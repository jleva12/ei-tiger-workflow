"""adk_run_store: ADK workflow runs in this database

ADK workflow runs leave the async worker's task framework: they're kept here,
beside their ADK sessions, as forge_task_adk_workflows.run_store describes
them. adk_runs holds one row a run (its status, what it runs, what it waits
at, its result or failure, the worker that has it); adk_run_events its
activity, which goes with it. The admin API starts, lists and acts on them;
the worker runs them.

background_tasks:manage becomes agents:manage_runs, with every grant of it:
retrying, resubmitting and abandoning ADK workflow runs. Its description
changes where it's still the default.

The downgrade drops the tables, with every run in them, and gives the
permission its old key back.

Revision ID: 0005adk_run_store
Revises: 0004adk_only
Create Date: 2026-10-01 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005adk_run_store"
down_revision: str | Sequence[str] | None = "0004adk_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The permission's (resource, action) before and after, and its default
# description before and after.
OLD = ("background_tasks", "manage")
NEW = ("agents", "manage_runs")
DESCRIPTIONS = (
    "Resubmit, restart and abandon the organization's ADK workflow runs",
    "Retry, resubmit and abandon the organization's ADK workflow runs",
)

# UTC, to the microsecond, as the run store keeps its times.
DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
# SQLite numbers a row only for an INTEGER primary key.
EVENT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

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


def rename(old: tuple[str, str], new: tuple[str, str], texts: tuple[str, str]) -> None:
    """Give the permission and every grant of it the new key, and its
    description the second text where it's still the first."""
    op.execute(
        rules_table.update()
        .where(
            rules_table.c.ptype == "p",
            rules_table.c.v1 == old[0],
            rules_table.c.v2 == old[1],
        )
        .values(v1=new[0], v2=new[1])
    )
    op.execute(
        permissions_table.update()
        .where(
            permissions_table.c.resource == old[0],
            permissions_table.c.action == old[1],
            permissions_table.c.description == texts[0],
        )
        .values(description=texts[1])
    )
    op.execute(
        permissions_table.update()
        .where(
            permissions_table.c.resource == old[0],
            permissions_table.c.action == old[1],
        )
        .values(resource=new[0], action=new[1])
    )


def upgrade() -> None:
    op.create_table(
        "adk_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("decisions", sa.JSON(), nullable=True),
        sa.Column("pause", sa.JSON(), nullable=True),
        sa.Column("waiting_until", DATETIME, nullable=True),
        sa.Column("waiting_reason", sa.String(length=500), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("requested_by_name", sa.String(length=255), nullable=False),
        sa.Column("resubmit_of", sa.String(length=32), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", DATETIME, nullable=True),
        sa.Column("created_at", DATETIME, nullable=False),
        sa.Column("updated_at", DATETIME, nullable=False),
        sa.Column("started_at", DATETIME, nullable=True),
        sa.Column("finished_at", DATETIME, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adk_runs")),
    )
    op.create_index(
        "ix_adk_runs_organization",
        "adk_runs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_adk_runs_agent",
        "adk_runs",
        ["organization_id", "agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_adk_runs_status", "adk_runs", ["status", "updated_at"], unique=False
    )
    op.create_table(
        "adk_run_events",
        sa.Column("id", EVENT_ID, autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("at", DATETIME, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["adk_runs.id"],
            name=op.f("fk_adk_run_events_run_id_adk_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adk_run_events")),
    )
    op.create_index(
        "ix_adk_run_events_run", "adk_run_events", ["run_id", "id"], unique=False
    )
    rename(OLD, NEW, DESCRIPTIONS)


def downgrade() -> None:
    rename(NEW, OLD, DESCRIPTIONS[::-1])
    # Dropping a table drops its indexes; the events first, for their foreign
    # key to the runs.
    op.drop_table("adk_run_events")
    op.drop_table("adk_runs")
