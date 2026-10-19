"""agent_access: each organization's own agent permissions

Apart from the site's roles and permissions, which decide what people see
and do in Forge, each organization now sets up its own, for who may use its
agents and run its workflows: agent_permissions (resource:action, named by
the organization), linked to the company's groups (agent_group_links lists
them) and to its own API keys. There are no agent roles: the directory's
groups say who's who. Their policy lives in agent_casbin_rule, every line in
its organization's domain (org:<id>), read with
forge_admin/agent_access/agent_casbin_model.conf. They go with their
organization.

agent_access:manage sets them up, granted here to the organization
administrator; what's linked to what is read with members:read, as members are.
Written once, so later edits made through the API are never reverted.

Revision ID: 0027agent_access
Revises: 0026adk_run_gates
Create Date: 2026-10-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0027agent_access"
down_revision: str | Sequence[str] | None = "0026adk_run_gates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "agent_access", "manage"
DESCRIPTION = (
    "Set up the organization's agent permissions and link them to company "
    "groups and API keys"
)
MANAGERS = ("org:admin",)

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

#: When a row's written, to the microsecond (forge_admin.db.base).
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def ascii_id(length: int) -> sa.String:
    """An ASCII identifier column (forge_admin.db.base.ascii_string)."""
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=length), "mysql"
    )


def timestamp() -> sa.DateTime:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def audit_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    """Who made and last changed a row, and when (forge_admin.db.base.AuditBase)."""
    return [
        sa.Column("created_at", timestamp(), server_default=NOW, nullable=False),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column("updated_at", timestamp(), server_default=NOW, nullable=False),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def organization_key(table: str) -> sa.ForeignKeyConstraint:
    """The organization a row goes with, deleted with it."""
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "agent_casbin_rule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ptype", ascii_id(8), nullable=False),
        *(sa.Column(f"v{i}", ascii_id(255), nullable=True) for i in range(6)),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_casbin_rule")),
        sa.UniqueConstraint(
            "ptype", "v0", "v1", "v2", "v3", name="uq_agent_casbin_rule_line"
        ),
    )
    op.create_index(
        "ix_agent_casbin_rule_ptype_v1_v2", "agent_casbin_rule", ["ptype", "v1", "v2"]
    )
    op.create_index(
        "ix_agent_casbin_rule_ptype_v2", "agent_casbin_rule", ["ptype", "v2"]
    )
    op.create_table(
        "agent_permissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("resource", ascii_id(255), nullable=False),
        sa.Column("action", ascii_id(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_permissions")),
        sa.UniqueConstraint(
            "organization_id",
            "resource",
            "action",
            name=op.f("uq_agent_permissions_organization_id"),
        ),
        organization_key("agent_permissions"),
    )
    op.create_table(
        "agent_group_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("group_name", ascii_id(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_group_links")),
        sa.UniqueConstraint(
            "organization_id",
            "group_name",
            name=op.f("uq_agent_group_links_organization_id"),
        ),
        organization_key("agent_group_links"),
    )
    op.bulk_insert(
        permissions_table,
        [{"resource": RESOURCE, "action": ACTION, "description": DESCRIPTION}],
    )
    op.bulk_insert(
        rules_table,
        [{"ptype": "p", "v0": role, "v1": RESOURCE, "v2": ACTION} for role in MANAGERS],
    )


def downgrade() -> None:
    # Every grant of the permission (the defaults and any since), then it.
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
    op.drop_table("agent_group_links")
    op.drop_table("agent_permissions")
    op.drop_index("ix_agent_casbin_rule_ptype_v2", table_name="agent_casbin_rule")
    op.drop_index("ix_agent_casbin_rule_ptype_v1_v2", table_name="agent_casbin_rule")
    op.drop_table("agent_casbin_rule")
