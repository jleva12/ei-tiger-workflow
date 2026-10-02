"""adk_only: no more inbound events or Forge workflows

Forge now builds and runs Google ADK workflows only. Organizations' inbound
events go: their endpoints, event types and received events are dropped. So
do the permissions only Forge workflows and events used, events:manage and
workflows:manage, run and approve, with every grant of them. The defaults'
descriptions that named workflows and events now name ADK workflows, where
they're still the defaults; one edited through the API is left alone.
background_tasks:manage stays: it resubmits, restarts and abandons ADK
workflow runs.

The downgrade makes the tables again, empty, and gives the permissions back
to the roles the baseline granted them to.

Revision ID: 0004adk_only
Revises: 0003adk_runs
Create Date: 2026-10-01 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004adk_only"
down_revision: str | Sequence[str] | None = "0003adk_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The permissions that go, as the baseline described them, and the roles it
# granted each to.
PERMISSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "events:manage": (
        "Turn on an organization's inbound events endpoint, rotate its token, "
        "and define the event types it accepts with their schemas",
        ("org:admin",),
    ),
    "workflows:manage": (
        "Make, change and delete the organization's workflows",
        ("org:admin", "org:member"),
    ),
    "workflows:run": (
        "Run the organization's workflows, and decide approvals any member may",
        ("org:admin", "org:member"),
    ),
    "workflows:approve": (
        "Decide approvals of the organization's workflow runs that its "
        "administrators decide",
        ("org:admin",),
    ),
}
# The defaults' descriptions that change: (the baseline's, the new one).
PERMISSION_DESCRIPTIONS = {
    "organizations:read": (
        "View an organization, its workflows, events and runs",
        "View an organization, its ADK workflows and their runs",
    ),
    "background_tasks:manage": (
        "Resubmit, restart and abandon the organization's workflow runs",
        "Resubmit, restart and abandon the organization's ADK workflow runs",
    ),
}
ROLE_DESCRIPTIONS = {
    "org:admin": (
        "Runs the organization: its members, inbound events, workflows and "
        "their approvals, and their runs.",
        "Runs the organization: its members, its ADK workflows and their "
        "approvals, and their runs.",
    ),
    "org:member": (
        "Works in the organization: builds and runs its workflows.",
        "Works in the organization: builds and runs its ADK workflows.",
    ),
    "org:viewer": (
        "Sees the organization, its workflows and their runs.",
        "Sees the organization, its ADK workflows and their runs.",
    ),
}

permissions_table = sa.table(
    "authz_permissions",
    sa.column("resource", sa.String),
    sa.column("action", sa.String),
    sa.column("description", sa.Text),
)
roles_table = sa.table(
    "authz_roles",
    sa.column("key", sa.String),
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


def describe(old: int, new: int) -> None:
    """Set the defaults' descriptions from one of each pair to the other, where
    they're still the first."""
    for key, texts in PERMISSION_DESCRIPTIONS.items():
        resource, action = key.split(":")
        op.execute(
            permissions_table.update()
            .where(
                permissions_table.c.resource == resource,
                permissions_table.c.action == action,
                permissions_table.c.description == texts[old],
            )
            .values(description=texts[new])
        )
    for key, texts in ROLE_DESCRIPTIONS.items():
        op.execute(
            roles_table.update()
            .where(roles_table.c.key == key, roles_table.c.description == texts[old])
            .values(description=texts[new])
        )


def upgrade() -> None:
    # Dropping a table drops its indexes; the events first, for their foreign
    # key to the event types.
    op.drop_table("organization_events")
    op.drop_table("organization_event_types")
    op.drop_table("organization_event_endpoints")
    # Every grant of the permissions goes with them, the defaults and any since.
    for key in PERMISSIONS:
        resource, action = key.split(":")
        op.execute(
            rules_table.delete().where(
                rules_table.c.ptype == "p",
                rules_table.c.v1 == resource,
                rules_table.c.v2 == action,
            )
        )
        op.execute(
            permissions_table.delete().where(
                permissions_table.c.resource == resource,
                permissions_table.c.action == action,
            )
        )
    describe(0, 1)


def downgrade() -> None:
    describe(1, 0)
    op.bulk_insert(
        permissions_table,
        [
            {
                "resource": key.split(":")[0],
                "action": key.split(":")[1],
                "description": text,
            }
            for key, (text, _) in PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        rules_table,
        [
            {"ptype": "p", "v0": role, "v1": key.split(":")[0], "v2": key.split(":")[1]}
            for key, (_, roles) in PERMISSIONS.items()
            for role in roles
        ],
    )
    # The tables as the baseline made them.
    op.create_table(
        "organization_event_endpoints",
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
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "token_sha256",
            sa.String(length=64).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=64),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column(
            "token_hint",
            sa.String(length=8).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=8), "mysql"
            ),
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
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_event_endpoints_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_event_endpoints")),
        sa.UniqueConstraint(
            "organization_id",
            name=op.f("uq_organization_event_endpoints_organization_id"),
        ),
    )
    op.create_table(
        "organization_event_types",
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
        sa.Column(
            "key",
            sa.String(length=100).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=100),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=16),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("payload_schema", json_text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
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
            name=op.f("fk_organization_event_types_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_event_types")),
        sa.UniqueConstraint(
            "organization_id",
            "key",
            name=op.f("uq_organization_event_types_organization_id"),
        ),
    )
    op.create_table(
        "organization_events",
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
        sa.Column(
            "event_type_id",
            sa.String(length=36).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=36),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=16),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("payload", json_text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=False),
        sa.Column(
            "source_ip",
            sa.String(length=64).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=64),
                "mysql",
            ),
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
            ["event_type_id"],
            ["organization_event_types.id"],
            name=op.f("fk_organization_events_event_type_id_organization_event_types"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_events")),
        sa.UniqueConstraint(
            "event_type_id",
            "idempotency_key",
            name=op.f("uq_organization_events_event_type_id"),
        ),
    )
    op.create_index(
        "ix_organization_events_event_type_id_created_at",
        "organization_events",
        ["event_type_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_organization_events_organization_id_created_at",
        "organization_events",
        ["organization_id", "created_at"],
        unique=False,
    )
