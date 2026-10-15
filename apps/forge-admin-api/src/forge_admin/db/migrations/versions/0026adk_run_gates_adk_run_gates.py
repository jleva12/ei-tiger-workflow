"""adk_run_gates: a run waits at several approvals and questions at once

A run waited at one approval or question at a time (adk_runs.pause): its ways
each at an approval of their own were asked one after another, and a
Decision board's choices couldn't be. adk_runs.gates holds every approval
and question the run waits at, each decided on its own, while the run is
paused, waiting for a time, or carrying on with a decision made at another
(pause stays the first of them while it's paused, for what reads it). A run
from before has none: its pause is its only one. The run store
(forge_task_adk_workflows.run_store) reads and writes it, so this runs before
the workers and the admin API that use it.

Revision ID: 0026adk_run_gates
Revises: 0025skills
Create Date: 2026-10-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026adk_run_gates"
down_revision: str | Sequence[str] | None = "0025skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("adk_runs", sa.Column("gates", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("adk_runs", "gates")
