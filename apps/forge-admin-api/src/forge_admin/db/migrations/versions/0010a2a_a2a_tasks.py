"""a2a: chat agents' A2A tasks

The tasks callers of chat agents start over Google's A2A protocol (the
hosted runtime's /runtime/a2a/{agent}): each message's status, history and
reply, and whose it is (the agent, and the caller when signed in). The table
is a2a-sdk's DatabaseTaskStore's, as a2a-sdk 1.x declares it
(forge_agent_runtime.a2a.task_store); a newer a2a-sdk that changes it needs
a migration of its own.

The downgrade drops the table, with the tasks in it; the conversations they
were in stay, as ADK sessions.

Revision ID: 0010a2a
Revises: 0009usage
Create Date: 2026-10-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010a2a"
down_revision: str | Sequence[str] | None = "0009usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("context_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("status", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        sa.Column("protocol_version", sa.String(length=16), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_a2a_tasks")),
    )
    op.create_index("ix_a2a_tasks_id", "a2a_tasks", ["id"], unique=False)
    op.create_index(
        "idx_a2a_tasks_owner_last_updated",
        "a2a_tasks",
        ["owner", "last_updated"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_a2a_tasks_owner_last_updated", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_id", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
