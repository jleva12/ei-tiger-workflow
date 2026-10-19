"""exactly_once_starts: chat turns one at a time, workflow starts once

- chat_turn_leases: each conversation's turn lease (forge_agent_runtime.leases):
  the turn running in it, held until expires_at and renewed while it runs,
  shared by every process of the hosted runtime, so a conversation's turns
  are one at a time across them all.
- workflow_start_intents: the workflow runs an intake starts when it
  completes, written before the change that completes it is kept and started
  once it is (the start's outbox); each under the ID of the run it makes, so
  starting it again finds the run.
- intake_requests.version: each change to a request is made only to the
  version it was read at (compare-and-set); a request being submitted is
  ``submitting`` until its workflows have started.

Revision ID: 0033exactly_once_starts
Revises: 0032helper_jobs
Create Date: 2026-10-18 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0033exactly_once_starts"
down_revision: str | Sequence[str] | None = "0032helper_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
#: When a row is written, as MySQL keeps it to the microsecond.
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def ascii_id(length: int) -> sa.String:
    """An ASCII identifier, compared byte by byte (forge_admin.db.base.ascii_string)."""
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=length), "mysql"
    )


def json_text() -> sa.Text:
    """A JSON value kept as text, as written (forge_admin.db.base.JSONText)."""
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def audit_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column("updated_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_turn_leases",
        sa.Column("id", ascii_id(64), nullable=False),
        sa.Column("app_name", ascii_id(64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("owner", ascii_id(32), nullable=False),
        sa.Column("expires_at", DATETIME, nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_turn_leases")),
    )
    op.create_table(
        "workflow_start_intents",
        sa.Column("id", ascii_id(32), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("agent_id", ascii_id(64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("workflow", ascii_id(80), nullable=False),
        sa.Column("workflow_name", sa.String(length=255), nullable=False),
        sa.Column("input", json_text(), nullable=False),
        sa.Column("origin", json_text(), nullable=False),
        sa.Column("source", ascii_id(16), nullable=False),
        sa.Column("source_id", ascii_id(64), nullable=False),
        sa.Column("state_key", sa.String(length=255), nullable=True),
        sa.Column("status", ascii_id(16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_until", DATETIME, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dispatched_at", DATETIME, nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_start_intents")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_workflow_start_intents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_workflow_start_intents_status",
        "workflow_start_intents",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_workflow_start_intents_source",
        "workflow_start_intents",
        ["source", "source_id"],
    )
    op.add_column(
        "intake_requests",
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("intake_requests", "version")
    op.drop_index(
        "ix_workflow_start_intents_source", table_name="workflow_start_intents"
    )
    op.drop_index(
        "ix_workflow_start_intents_status", table_name="workflow_start_intents"
    )
    op.drop_table("workflow_start_intents")
    op.drop_table("chat_turn_leases")
