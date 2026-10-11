"""api_credentials: organizations' API credentials, and the permission to manage them

api_credentials holds how an organization's workflows and agents sign in to
its HTTP APIs (their HTTP steps, HTTP tools and OpenAPI tools name one by
ID): an auth method's settings in the clear, its secrets and grant encrypted
with FORGE_ADMIN_SECRETS_KEY, and the URLs it may be sent to.
api_credential_oauth_flows holds the OAuth sign-ins people began and haven't
finished. Both go with their organization, and the flows with their
credential.

Adding, changing, signing in with and deleting them needs credentials:manage,
granted here to the organization administrator and member roles as
mcp_servers:manage is. Written once, so later edits made through the API are
never reverted.

Revision ID: 0023api_credentials
Revises: 0022adk_run_backoff
Create Date: 2026-10-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0023api_credentials"
down_revision: str | Sequence[str] | None = "0022adk_run_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "credentials", "manage"
DESCRIPTION = "Add, change, sign in with and delete the organization's API credentials"
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

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
#: When a row is written, as MySQL keeps it to the microsecond.
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def ascii_id(length: int) -> sa.String:
    """An ASCII identifier, compared byte by byte (forge_admin.db.base.ascii_string)."""
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=length), "mysql"
    )


def json_text() -> sa.Text:
    """A JSON value kept as text, as written (forge_admin.db.base.JSONText)."""
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def audit_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            DATETIME,
            server_default=NOW,
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            DATETIME,
            server_default=NOW,
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "api_credentials",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("urls", json_text(), nullable=False),
        sa.Column("auth_kind", ascii_id(64), nullable=False),
        sa.Column("auth_settings", json_text(), nullable=False),
        sa.Column("auth_secrets", sa.Text(), nullable=True),
        sa.Column("auth_grant", sa.Text(), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_credentials")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_api_credentials_organization_id")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_api_credentials_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "api_credential_oauth_flows",
        sa.Column("state", ascii_id(64), nullable=False),
        sa.Column("credential_id", ascii_id(36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("pending", sa.Text(), nullable=False),
        sa.Column("expires_at", DATETIME, nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_api_credential_oauth_flows")),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["api_credentials.id"],
            name=op.f("fk_api_credential_oauth_flows_credential_id_api_credentials"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_api_credential_oauth_flows_credential_id"),
        "api_credential_oauth_flows",
        ["credential_id"],
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
    op.drop_table("api_credential_oauth_flows")
    op.drop_table("api_credentials")
