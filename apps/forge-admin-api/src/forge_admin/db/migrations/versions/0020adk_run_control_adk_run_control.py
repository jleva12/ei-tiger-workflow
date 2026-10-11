"""adk_run_control: abandoning a running run, and counting a run's takings

A run can be abandoned while it runs now: adk_runs.cancel_requested_at marks
it, and its worker stops it and abandons it (or the maintenance does, should
the worker go first). adk_runs.lease_epoch counts the times a worker took the
run, for the keys that make an attempt's effects happen once. The run store
(forge_task_adk_workflows.run_store) reads and writes both, so this runs
before the workers and the admin API that use them.

Revision ID: 0020adk_run_control
Revises: 0019system_designs
Create Date: 2026-10-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0020adk_run_control"
down_revision: str | Sequence[str] | None = "0019system_designs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.add_column(
        "adk_runs",
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("adk_runs", sa.Column("cancel_requested_at", DATETIME, nullable=True))


def downgrade() -> None:
    op.drop_column("adk_runs", "cancel_requested_at")
    op.drop_column("adk_runs", "lease_epoch")
