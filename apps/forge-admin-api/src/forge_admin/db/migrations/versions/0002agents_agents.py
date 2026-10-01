"""agents: the permission to make them

Organizations' agents live in MongoDB, beside their workflows; making,
changing and deleting them needs agents:manage, granted here to the
organization administrator and member roles as workflows:manage is. Written
once, so later edits made through the API are never reverted.

Revision ID: 0002agents
Revises: 0001baseline
Create Date: 2026-09-28 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002agents"
down_revision: str | Sequence[str] | None = "0001baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "agents", "manage"
DESCRIPTION = "Make, change and delete the organization's agents"
ROLES = ("org:admin", "org:member")

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
        [{"resource": RESOURCE, "action": ACTION, "description": DESCRIPTION}],
    )
    op.bulk_insert(
        rules_table,
        [{"ptype": "p", "v0": role, "v1": RESOURCE, "v2": ACTION} for role in ROLES],
    )


def downgrade() -> None:
    # Every grant of the permission goes with it, the defaults and any since.
    op.execute(
        rules_table.delete().where(
            rules_table.c.ptype == "p",
            rules_table.c.v1 == RESOURCE,
            rules_table.c.v2 == ACTION,
        )
    )
    op.execute(
        permissions_table.delete().where(
            permissions_table.c.resource == RESOURCE,
            permissions_table.c.action == ACTION,
        )
    )
