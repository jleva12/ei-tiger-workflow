"""mcp_servers: organizations' MCP servers, and the permission to manage them

mcp_servers holds an organization's remote MCP servers, which its agents use
as toolsets: where each is, how Forge authenticates to it (the auth method's
settings in the clear; its secrets and grant encrypted with
FORGE_ADMIN_SECRETS_KEY), and the tools it listed when last checked.
mcp_oauth_flows holds the OAuth sign-ins people began and haven't finished.
Both go with their organization, and the flows with their server.

Adding, changing, checking, connecting and deleting them needs
mcp_servers:manage, granted here to the organization administrator and member
roles as agents:manage is. Written once, so later edits made through the API
are never reverted.

Revision ID: 0006mcp_servers
Revises: 0005adk_run_store
Create Date: 2026-10-04 11:21:03.763592
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0006mcp_servers"
down_revision: str | Sequence[str] | None = "0005adk_run_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "mcp_servers", "manage"
DESCRIPTION = "Add, change, connect and delete the organization's MCP servers"
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


def json_text() -> sa.Text:
    """A JSON value kept as text, as written (forge_admin.db.base.JSONText)."""
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column(
            "id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "transport",
            sa.String(length=32).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=32),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("headers", json_text(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column(
            "auth_kind",
            sa.String(length=64).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=64),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("auth_settings", json_text(), nullable=False),
        sa.Column("auth_secrets", sa.Text(), nullable=True),
        sa.Column("auth_grant", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=16),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("tools", json_text(), nullable=False),
        sa.Column("server_info", json_text(), nullable=True),
        sa.Column(
            "checked_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_mcp_servers_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_servers")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_mcp_servers_organization_id")
        ),
    )
    op.create_table(
        "mcp_oauth_flows",
        sa.Column(
            "state",
            sa.String(length=64).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=64),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "server_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("pending", sa.Text(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["mcp_servers.id"],
            name=op.f("fk_mcp_oauth_flows_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_mcp_oauth_flows")),
    )
    op.create_index(
        op.f("ix_mcp_oauth_flows_server_id"),
        "mcp_oauth_flows",
        ["server_id"],
        unique=False,
    )
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
    # Each table takes its indexes with it.
    op.drop_table("mcp_oauth_flows")
    op.drop_table("mcp_servers")
