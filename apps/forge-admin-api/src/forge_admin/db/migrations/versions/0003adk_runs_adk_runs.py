"""adk_runs: the permissions to run ADK workflows and decide their approvals

ADK workflows (the organization's agents) run on the async worker, apart from
Forge workflows, with permissions of their own: agents:run starts a run and
answers its questions (and decides the approvals any member may), granted to
the organization administrator and member roles as workflows:run is;
agents:approve decides the approvals the administrators decide, granted to
the administrator role as workflows:approve is. Written once, so later edits
made through the API are never reverted.

Revision ID: 0003adk_runs
Revises: 0002agents
Create Date: 2026-09-28 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003adk_runs"
down_revision: str | Sequence[str] | None = "0002agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE = "agents"
# Each permission's action, description and the roles granted it.
PERMISSIONS = (
    ("run", "Run the organization's ADK workflows", ("org:admin", "org:member")),
    (
        "approve",
        "Decide the approvals of the organization's ADK workflow runs",
        ("org:admin",),
    ),
)

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


def upgrade() -> None:
    op.bulk_insert(
        permissions_table,
        [
            {"resource": RESOURCE, "action": action, "description": description}
            for action, description, _ in PERMISSIONS
        ],
    )
    op.bulk_insert(
        rules_table,
        [
            {"ptype": "p", "v0": role, "v1": RESOURCE, "v2": action}
            for action, _, roles in PERMISSIONS
            for role in roles
        ],
    )


def downgrade() -> None:
    # Every grant of the permissions goes with them, the defaults and any since.
    actions = [action for action, _, _ in PERMISSIONS]
    op.execute(
        rules_table.delete().where(
            rules_table.c.ptype == "p",
            rules_table.c.v1 == RESOURCE,
            rules_table.c.v2.in_(actions),
        )
    )
    op.execute(
        permissions_table.delete().where(
            permissions_table.c.resource == RESOURCE,
            permissions_table.c.action.in_(actions),
        )
    )
