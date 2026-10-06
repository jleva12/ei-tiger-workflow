"""adk_run_versions: which version of a workflow each run ran

Workflows have versions now, as chat agents do: a draft, and published
versions nothing changes. adk_runs.version says which a run ran: "draft", or
a published version's number. Runs from before have none. The run store
(forge_task_adk_workflows.run_store) writes it only when it's known, so a
worker older than this migration still starts runs.

Revision ID: 0012adk_run_versions
Revises: 0011api_keys
Create Date: 2026-10-05 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012adk_run_versions"
down_revision: str | Sequence[str] | None = "0011api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("adk_runs", sa.Column("version", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("adk_runs", "version")
