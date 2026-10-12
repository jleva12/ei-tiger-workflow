"""conversation_audit: how each turn came in, conversations' usage by session, and agents:audit

usage_invocations gets ``channel``: console (the builder's chat), api (the
hosted run API), a2a or assistant, so an organization's admins can tell a
conversation's turns apart by where they came from. Turns before this have
none. usage_invocations and usage_calls get an index on ``session_id``, which
a conversation's audit reads them by.

Reading every conversation of the organization's agents and of the
assistant asked from it (their messages, state, tool calls and usage) needs
agents:audit, granted here to the organization administrator role; site
administrators hold every permission. Written once, so later edits made
through the API are never reverted.

Revision ID: 0024conversation_audit
Revises: 0023api_credentials
Create Date: 2026-10-11 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024conversation_audit"
down_revision: str | Sequence[str] | None = "0023api_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "agents", "audit"
DESCRIPTION = (
    "Read every conversation of the organization's agents and assistant: "
    "messages, state, tool calls and usage"
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
    op.add_column(
        "usage_invocations", sa.Column("channel", sa.String(16), nullable=True)
    )
    op.create_index("ix_usage_invocations_session", "usage_invocations", ["session_id"])
    op.create_index("ix_usage_calls_session", "usage_calls", ["session_id"])
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
    op.drop_index("ix_usage_calls_session", table_name="usage_calls")
    op.drop_index("ix_usage_invocations_session", table_name="usage_invocations")
    op.drop_column("usage_invocations", "channel")
