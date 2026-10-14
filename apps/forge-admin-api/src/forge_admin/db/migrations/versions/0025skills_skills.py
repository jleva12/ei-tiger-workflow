"""skills: organizations' skills vault, the skill creator's jobs, and skills:manage

skills holds an organization's skills (Agent Skills: a SKILL.md and the files
beside it), each whole as JSON (forge_agent_runtime.skills.SkillBundle), its
name unique in the organization, and where it came from: written in the web
console, uploaded, imported from GitHub or written by the skill creator.
Agents' Skills tools and workflows' LLM agents name them by ID.
skill_creator_jobs holds what the skill creator was asked to write in the
background and how it went. Both go with their organization; a job's skill
is forgotten when the skill is deleted.

Adding, changing, importing and deleting skills, and having the skill
creator write one, needs skills:manage, granted here to the organization
administrator and member roles as credentials:manage is. Written once, so
later edits made through the API are never reverted.

Revision ID: 0025skills
Revises: 0024conversation_audit
Create Date: 2026-10-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0025skills"
down_revision: str | Sequence[str] | None = "0024conversation_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE, ACTION = "skills", "manage"
DESCRIPTION = (
    "Add, change, import and delete the organization's skills, and have the "
    "skill creator write one"
)
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
        "skills",
        sa.Column("id", ascii_id(36), nullable=False),
        sa.Column("organization_id", ascii_id(36), nullable=False),
        sa.Column("name", ascii_id(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("bundle", json_text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source", json_text(), nullable=False),
        *audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_skills_organization_id")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_skills_organization_id_organizations"),
            ondelete="CASCADE",
        ),
    )
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
    op.drop_table("skill_creator_jobs")
    op.drop_table("skills")
