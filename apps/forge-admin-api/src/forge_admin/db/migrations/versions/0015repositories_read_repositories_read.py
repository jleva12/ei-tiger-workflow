"""repositories_read: the permission to read the organization's code in the
code graph

The code graph's MCP server (apps/forge-codegraph-mcp) serves an
organization's repositories to whoever holds repositories:read there, which
it asks the admin API about for every credential it's sent
(GET /code-graph/access). Granted here to the organization's administrators,
members and viewers, and to API caller (org:api), the role keys usually
hold, so an organization's API key reads its code. Written once, so later
edits made through the API are never reverted.

Revision ID: 0015repositories_read
Revises: 0014code_repositories
Create Date: 2026-10-07 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015repositories_read"
down_revision: str | Sequence[str] | None = "0014code_repositories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "repositories", "read"
DESCRIPTION = (
    "Search and read the code of the organization's repositories in the code "
    "graph, through its MCP server"
)
ROLES = ("org:admin", "org:member", "org:viewer", "org:api")

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
    # Every grant of the permission, the defaults and any since, then it.
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
