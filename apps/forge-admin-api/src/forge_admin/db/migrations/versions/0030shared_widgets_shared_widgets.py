"""shared_widgets: widgets people share with their organization, and shared_widgets:manage

shared_widgets holds the widgets people defined and shared with their
organization (organization_id) for a dashboard (e.g. organization-overview):
its type (custom), title and settings as a layout keeps a widget's, as JSON
text. Members add a copy each to their own layout, so taking one away from
the organization leaves theirs be. Who shared it is created_by. They go with
their organization.

Who shared a widget may take it away; so may whoever holds
shared_widgets:manage in the organization, granted here to its
administrators. Written once, so later edits made through the API are never
reverted.

Revision ID: 0030shared_widgets
Revises: 0029dashboard_layouts
Create Date: 2026-10-16 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0030shared_widgets"
down_revision: str | Sequence[str] | None = "0029dashboard_layouts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "shared_widgets", "manage"
DESCRIPTION = "Take away the widgets members shared with the organization"
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


def audit_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column("updated_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "shared_widgets",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("dashboard", ascii_id(64), nullable=False),
        sa.Column("type", ascii_id(64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("settings", json_text(), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shared_widgets")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_shared_widgets_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    # An organization's library for a dashboard, newest first.
    op.create_index(
        "ix_shared_widgets_library",
        "shared_widgets",
        ["organization_id", "dashboard", "created_at"],
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
    op.drop_index("ix_shared_widgets_library", table_name="shared_widgets")
    op.drop_table("shared_widgets")
