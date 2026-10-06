"""api_keys: organizations' API keys, the API caller role, and managing keys

api_keys holds the keys outside apps send instead of a person's sign-in:
each key's name, its secret's SHA-256 and last four characters, and when it
expires and was last used. A key goes with its organization. What a key may
do is the role it holds there, a g line for the subject apikey:<id> like a
member's; the downgrade removes those lines with the table.

api_keys:manage creates, changes and deletes them, granted here to the
organization administrator. The new role org:api ("API caller") is what a key
usually needs: agents:run, which now also covers calling the organization's
agents and workflows through the runtime (its description says so, unless it
was changed since). Written once, so later edits made through the API are
never reverted.

Revision ID: 0011api_keys
Revises: 0010a2a
Create Date: 2026-10-05 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0011api_keys"
down_revision: str | Sequence[str] | None = "0010a2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "api_keys", "manage"
DESCRIPTION = "Create, change and delete the organization's API keys"
MANAGERS = ("org:admin",)

ROLE = "org:api"
ROLE_NAME = "API caller"
ROLE_DESCRIPTION = (
    "What an API key usually needs: calls the organization's agents and "
    "workflows through the runtime."
)
ROLE_GRANTS = (("agents", "run"),)

RUN_BEFORE = "Run the organization's ADK workflows"
RUN_AFTER = (
    "Run the organization's workflows, and call its agents and workflows "
    "through the runtime"
)

permissions_table = sa.table(
    "authz_permissions",
    sa.column("resource", sa.String),
    sa.column("action", sa.String),
    sa.column("description", sa.Text),
)
roles_table = sa.table(
    "authz_roles",
    sa.column("key", sa.String),
    sa.column("name", sa.String),
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


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("secret_sha256", ascii_id(64), nullable=False),
        sa.Column("hint", ascii_id(8), nullable=False),
        sa.Column("expires_at", timestamp(), nullable=True),
        sa.Column("last_used_at", timestamp(), nullable=True),
        sa.Column(
            "created_at",
            timestamp(),
            server_default=NOW,
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column(
            "updated_at",
            timestamp(),
            server_default=NOW,
            nullable=False,
        ),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_api_keys_organization_id")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_api_keys_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("secret_sha256", name=op.f("uq_api_keys_secret_sha256")),
    )
    op.bulk_insert(
        permissions_table,
        [{"resource": RESOURCE, "action": ACTION, "description": DESCRIPTION}],
    )
    op.bulk_insert(
        roles_table,
        [{"key": ROLE, "name": ROLE_NAME, "description": ROLE_DESCRIPTION}],
    )
    op.bulk_insert(
        rules_table,
        [
            *(
                {"ptype": "p", "v0": role, "v1": RESOURCE, "v2": ACTION}
                for role in MANAGERS
            ),
            *(
                {"ptype": "p", "v0": ROLE, "v1": resource, "v2": action}
                for resource, action in ROLE_GRANTS
            ),
        ],
    )
    _describe_run(RUN_BEFORE, RUN_AFTER)


def downgrade() -> None:
    _describe_run(RUN_AFTER, RUN_BEFORE)
    # The keys' roles, then the role (its grants and every assignment of it),
    # then the permission (every grant of it, the defaults and any since).
    op.execute(
        rules_table.delete().where(
            rules_table.c.ptype == "g", rules_table.c.v0.like("apikey:%")
        )
    )
    op.execute(
        rules_table.delete().where(
            ((rules_table.c.ptype == "p") & (rules_table.c.v0 == ROLE))
            | ((rules_table.c.ptype == "g") & (rules_table.c.v1 == ROLE))
        )
    )
    op.execute(roles_table.delete().where(roles_table.c.key == ROLE))
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
    op.drop_table("api_keys")


def _describe_run(old: str, new: str) -> None:
    """Reword agents:run, unless someone has described it their own way."""
    op.execute(
        permissions_table.update()
        .where(
            permissions_table.c.resource == "agents",
            permissions_table.c.action == "run",
            permissions_table.c.description == old,
        )
        .values(description=new)
    )
