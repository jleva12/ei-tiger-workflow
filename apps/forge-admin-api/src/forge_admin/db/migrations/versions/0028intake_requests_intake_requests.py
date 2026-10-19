"""intake_requests: the requests chat agents file to their intakes for people

A chat agent with intake agents on its Tools way files the person's requests
to them itself (forge_agent_runtime.requests): one request per intake, its
record whole as JSON (forge_agent_runtime.intake.IntakeRecord), kept for the
person (user_id) with the agent (agent_id, whichever version), so it outlives
the conversation it was started in (session_id, where it was last worked on)
and an open one can be picked up in another. A request's intake is its intake
node's ID; requests started together share a basket. They go with their
organization.

Revision ID: 0028intake_requests
Revises: 0027agent_access
Create Date: 2026-10-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0028intake_requests"
down_revision: str | Sequence[str] | None = "0027agent_access"
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
        "intake_requests",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("agent_id", ascii_id(64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("intake", ascii_id(64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("basket", ascii_id(36), nullable=False),
        sa.Column("status", ascii_id(16), nullable=False),
        sa.Column("record", json_text(), nullable=False),
        sa.Column("submitted_at", DATETIME, nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_requests")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_intake_requests_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    # A person's requests with an agent, by status, newest first.
    op.create_index(
        "ix_intake_requests_owner",
        "intake_requests",
        ["organization_id", "agent_id", "user_id", "status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_intake_requests_owner", table_name="intake_requests")
    op.drop_table("intake_requests")
