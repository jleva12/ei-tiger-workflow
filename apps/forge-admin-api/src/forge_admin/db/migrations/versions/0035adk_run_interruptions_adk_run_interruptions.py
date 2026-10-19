"""adk_run_interruptions: an interrupted run spends no attempt

A run whose worker stopped (a deploy) or went (its lease ran out) while it
ran was queued again as its next attempt, so deploys, a worker's watchdog
taking its other runs down with it, and Redis outages spent the attempts its
hiccups have (ten), and failed it. adk_runs.interruptions counts them apart:
how often in a row the run was interrupted, since it last waited (for people
or a time) or someone retried it. The worker fails a run interrupted too often
in a row (one that takes its worker down each time it runs); its attempts are
its hiccups' and people's retries only. A run from before has none. The run
store (forge_task_adk_workflows.run_store) reads and writes it, so this runs
before the workers and the admin API that use it.

Revision ID: 0035adk_run_interruptions
Revises: 0034ui_components
Create Date: 2026-10-18 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035adk_run_interruptions"
down_revision: str | Sequence[str] | None = "0034ui_components"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adk_runs",
        sa.Column("interruptions", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("adk_runs", "interruptions")
