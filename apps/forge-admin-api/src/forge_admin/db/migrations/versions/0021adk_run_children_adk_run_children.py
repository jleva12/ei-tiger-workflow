"""adk_run_children: the runs a run starts, linked to it

A run's LLM agent can call a workflow as a tool, which starts a run of its own:
adk_runs.parent_run_id names the run that started it, so the run's page lists
the runs it started, and abandoning a run abandons them too. The runs started
before this have their parent in their payload (trigger.run_id, a workflow's
trigger), which fills the column in. The run store
(forge_task_adk_workflows.run_store) reads and writes it, so this runs before
the workers and the admin API that use it.

Revision ID: 0021adk_run_children
Revises: 0020adk_run_control
Create Date: 2026-10-09 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021adk_run_children"
down_revision: str | Sequence[str] | None = "0020adk_run_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each dialect's JSON path read of a run's parent from its payload, as text.
PARENT = {
    "mysql": "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.trigger.run_id'))",
    "sqlite": "json_extract(payload, '$.trigger.run_id')",
}
TRIGGER = {
    "mysql": "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.trigger.type'))",
    "sqlite": "json_extract(payload, '$.trigger.type')",
}
LENGTH = {"mysql": "CHAR_LENGTH", "sqlite": "LENGTH"}


def upgrade() -> None:
    op.add_column("adk_runs", sa.Column("parent_run_id", sa.String(32), nullable=True))
    op.create_index("ix_adk_runs_parent", "adk_runs", ["parent_run_id", "created_at"])
    dialect = op.get_context().dialect.name
    if dialect in PARENT:
        parent, trigger, length = PARENT[dialect], TRIGGER[dialect], LENGTH[dialect]
        op.execute(
            f"UPDATE adk_runs SET parent_run_id = {parent} "
            f"WHERE parent_run_id IS NULL AND {trigger} = 'workflow' "
            f"AND {parent} IS NOT NULL AND {length}({parent}) <= 32"
        )


def downgrade() -> None:
    op.drop_index("ix_adk_runs_parent", table_name="adk_runs")
    op.drop_column("adk_runs", "parent_run_id")
