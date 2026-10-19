"""ui_components: ui_components:manage, for organizations' team UI components

Team UI components are organizations' own compositions of the base
components chats draw, which their agents' Rich answers nodes pick by key
(forge_admin.ui_components). They're kept in MongoDB with their versions, so
this makes no table. Writing, previewing, publishing, disabling and archiving
them needs ui_components:manage, granted here to the organization
administrator role only: a component's description is told to every agent
that picks it, and its layout is what their users see. Members have
skills:manage by default; this, an administrator gives whom they choose.
Reading the organization's published components (its catalog) needs
organizations:read. Written once, so later edits made through the API are
never reverted.

Revision ID: 0034ui_components
Revises: 0033exactly_once_starts
Create Date: 2026-10-18 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034ui_components"
down_revision: str | Sequence[str] | None = "0033exactly_once_starts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "ui_components", "manage"
DESCRIPTION = (
    "Write, preview, publish, disable and archive the organization's team UI "
    "components, which its agents' rich answers draw"
)
ROLES = ("org:admin",)

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
    # Every grant of the permission goes with it, the default and any since.
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
