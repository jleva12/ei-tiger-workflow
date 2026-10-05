"""permission_group_links: the company's groups linked to permissions

authz_group_links holds the groups from the company's external directory
that are linked to permissions: someone whose token names one holds its
permissions on the whole site. The grants are casbin_rule p lines for the
subject group:<name>, written by the API.

The downgrade drops the table, and every grant to a group with it.

Revision ID: 0007group_links
Revises: 0006mcp_servers
Create Date: 2026-10-04 12:33:04.601803
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007group_links"
down_revision: str | Sequence[str] | None = "0006mcp_servers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

rules_table = sa.table(
    "casbin_rule",
    sa.column("ptype", sa.String),
    sa.column("v0", sa.String),
)


def upgrade() -> None:
    op.create_table(
        "authz_group_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "group_name",
            sa.String(length=200).with_variant(
                mysql.VARCHAR(charset="ascii", collation="ascii_bin", length=200),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authz_group_links")),
        sa.UniqueConstraint("group_name", name=op.f("uq_authz_group_links_group_name")),
    )


def downgrade() -> None:
    op.execute(
        rules_table.delete().where(
            rules_table.c.ptype == "p", rules_table.c.v0.like("group:%")
        )
    )
    op.drop_table("authz_group_links")
