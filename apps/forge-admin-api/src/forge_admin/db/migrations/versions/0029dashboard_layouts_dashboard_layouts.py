"""dashboard_layouts: how people lay out the web console's dashboards

One row per person (user_id, their subject) and dashboard (e.g.
organization-overview): the layout document the web console saves, which
widgets the dashboard shows, where and how big, as JSON text. The same in
every organization. Without a row, the dashboard shows its default; deleting
it resets the layout.

Revision ID: 0029dashboard_layouts
Revises: 0028intake_requests
Create Date: 2026-10-16 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0029dashboard_layouts"
down_revision: str | Sequence[str] | None = "0028intake_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
        "dashboard_layouts",
        sa.Column("user_id", ascii_id(255), nullable=False),
        sa.Column("dashboard", ascii_id(64), nullable=False),
        sa.Column("layout", json_text(), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint(
            "user_id", "dashboard", name=op.f("pk_dashboard_layouts")
        ),
    )


def downgrade() -> None:
    op.drop_table("dashboard_layouts")
