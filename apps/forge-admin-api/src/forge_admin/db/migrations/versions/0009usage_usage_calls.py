"""usage: what organizations' workflows, agents and assistant used

Every model and tool call an organization's workflows, agents and assistant
make (usage_calls: its model, tokens and cost) and every invocation of its
agents and assistant (usage_invocations: when, by whom, how it ended), as
forge_task_adk_workflows.usage_store describes them. The worker and the admin
API write them as runs go; the organization's overview reads them.

The downgrade drops the tables, with everything recorded in them.

Revision ID: 0009usage
Revises: 0008knowledge
Create Date: 2026-10-04 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0009usage"
down_revision: str | Sequence[str] | None = "0008knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# UTC, to the microsecond, as the usage store keeps its times.
DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
# SQLite numbers a row only for an INTEGER primary key.
CALL_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "usage_calls",
        sa.Column("id", CALL_ID, autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("subject_name", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("invocation_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("call", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("at", DATETIME, nullable=False),
        sa.Column("hour", sa.DateTime(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("thinking_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Double(), nullable=True),
        sa.Column("failed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_calls")),
    )
    op.create_index(
        "ix_usage_calls_organization",
        "usage_calls",
        ["organization_id", "hour"],
        unique=False,
    )
    op.create_index(
        "ix_usage_calls_subject",
        "usage_calls",
        ["organization_id", "kind", "subject_id", "hour"],
        unique=False,
    )
    op.create_table(
        "usage_invocations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("subject_name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("started_at", DATETIME, nullable=False),
        sa.Column("hour", sa.DateTime(), nullable=False),
        sa.Column("finished_at", DATETIME, nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_invocations")),
    )
    op.create_index(
        "ix_usage_invocations_organization",
        "usage_invocations",
        ["organization_id", "hour"],
        unique=False,
    )
    op.create_index(
        "ix_usage_invocations_subject",
        "usage_invocations",
        ["organization_id", "kind", "subject_id", "hour"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("usage_invocations")
    op.drop_table("usage_calls")
