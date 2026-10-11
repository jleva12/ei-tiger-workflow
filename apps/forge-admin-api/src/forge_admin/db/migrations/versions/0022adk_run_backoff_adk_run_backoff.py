"""adk_run_backoff: a run waits out a hiccup's backoff, whatever job comes for it

A run queued again after a hiccup was held to its backoff only by the job
queued for its end: any other job for it (the maintenance's, a duplicate)
took it at once, using up its attempts faster. adk_runs.next_attempt_at says
when it may be taken, and the run store's claim holds to it. A run without
one may be taken at once, as before. The run store
(forge_task_adk_workflows.run_store) reads and writes it, so this runs before
the workers and the admin API that use it.

Revision ID: 0022adk_run_backoff
Revises: 0021adk_run_children
Create Date: 2026-10-10 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0022adk_run_backoff"
down_revision: str | Sequence[str] | None = "0021adk_run_children"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.add_column("adk_runs", sa.Column("next_attempt_at", DATETIME, nullable=True))


def downgrade() -> None:
    op.drop_column("adk_runs", "next_attempt_at")
