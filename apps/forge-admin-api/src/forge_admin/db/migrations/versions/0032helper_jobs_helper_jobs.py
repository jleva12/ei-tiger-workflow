"""helper_jobs: every background helper agent's jobs, in one table

The skill creator kept its jobs in skill_creator_jobs. It's now one of the
helper agents (forge_admin.helpers, configured in helpers.yaml), and every
helper that runs in the background keeps its jobs in helper_jobs: which
helper, what it was asked (its kit's input, as JSON) and what it made (its
result, as JSON). The skill creator's jobs move over (its brief and the
skill it reworks as the input, the skill it saved as the result), and
skill_creator_jobs goes. The downgrade brings the skill creator's back.

The admin API reads and writes it, so this runs before the API that uses it.

Revision ID: 0032helper_jobs
Revises: 0031adk_run_provenance
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0032helper_jobs"
down_revision: str | Sequence[str] | None = "0031adk_run_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
#: When a row is written, as MySQL keeps it to the microsecond.
NOW = sa.text("CURRENT_TIMESTAMP(6)")
AUDIT = "created_at, created_by, updated_at, updated_by"


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
        sa.Column("created_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "created_by", sa.String(length=255), server_default="system", nullable=False
        ),
        sa.Column("updated_at", DATETIME, server_default=NOW, nullable=False),
        sa.Column(
            "updated_by", sa.String(length=255), server_default="system", nullable=False
        ),
    ]


def text_of(dialect: str, column: str, key: str) -> str:
    """:return: The dialect's read of ``column``'s ``key``, as text; null when
    it's missing or isn't a string."""
    found = f"JSON_EXTRACT({column}, '$.{key}')"
    if dialect == "mysql":
        return f"CASE WHEN JSON_TYPE({found}) = 'STRING' THEN JSON_UNQUOTE({found}) END"
    return f"CASE WHEN json_type({column}, '$.{key}') = 'text' THEN {found} END"


def upgrade() -> None:
    op.create_table(
        "helper_jobs",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("helper", ascii_id(64), nullable=False),
        sa.Column("input", json_text(), nullable=False),
        sa.Column("status", ascii_id(16), nullable=False),
        sa.Column("result", json_text(), nullable=True),
        sa.Column("notes", json_text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_helper_jobs")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_helper_jobs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_helper_jobs_organization_id"),
        "helper_jobs",
        ["organization_id"],
        unique=False,
    )
    # JSON_OBJECT is MySQL's and SQLite's alike.
    op.execute(
        f"""
INSERT INTO helper_jobs
  (id, organization_id, helper, input, status, result, notes, error, model, {AUDIT})
SELECT
  id, organization_id, 'skill_creator',
  JSON_OBJECT('brief', brief, 'revises', revises),
  status,
  CASE WHEN skill_id IS NULL THEN NULL ELSE JSON_OBJECT('skill_id', skill_id) END,
  notes, error, model, {AUDIT}
FROM skill_creator_jobs
"""
    )
    # The table takes its indexes with it.
    op.drop_table("skill_creator_jobs")


def downgrade() -> None:
    op.create_table(
        "skill_creator_jobs",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("revises", ascii_id(36), nullable=True),
        sa.Column("status", ascii_id(16), nullable=False),
        sa.Column("skill_id", ascii_id(36), nullable=True),
        sa.Column("notes", json_text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_creator_jobs")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_skill_creator_jobs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_skill_creator_jobs_skill_id_skills"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        op.f("ix_skill_creator_jobs_organization_id"),
        "skill_creator_jobs",
        ["organization_id"],
        unique=False,
    )
    dialect = op.get_context().dialect.name
    if dialect in ("mysql", "sqlite"):
        brief = text_of(dialect, "input", "brief")
        revises = text_of(dialect, "input", "revises")
        skill = text_of(dialect, "result", "skill_id")
        # Only the skill creator's jobs go back; a skill deleted since is
        # forgotten, as its foreign key would have.
        op.execute(
            f"""
INSERT INTO skill_creator_jobs
  (id, organization_id, brief, revises, status, skill_id, notes, error, model, {AUDIT})
SELECT
  id, organization_id, COALESCE({brief}, ''), SUBSTR({revises}, 1, 36), status,
  (SELECT skills.id FROM skills WHERE skills.id = {skill}),
  notes, error, model, {AUDIT}
FROM helper_jobs
WHERE helper = 'skill_creator'
"""
        )
    op.drop_table("helper_jobs")
