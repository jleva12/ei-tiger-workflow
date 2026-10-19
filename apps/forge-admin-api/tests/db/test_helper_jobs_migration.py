"""0032helper_jobs without MySQL: the table it makes is the model's, as
MySQL's DDL; the skill creator's jobs move over (and back on the downgrade)
with what they were asked and what they made."""

import importlib.util
import io
import json
import re
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from forge_admin.db.migrate import MIGRATIONS
from forge_admin.models import HelperJob

REVISION = MIGRATIONS / "versions" / "0032helper_jobs_helper_jobs.py"
ORG = "org-1"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0032", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statements(sql: str) -> set[str]:
    """DDL as comparable text: one line each, without constraint names."""
    found = set()
    for statement in sql.split(";"):
        text = re.sub(r"CONSTRAINT \w+ ", "", " ".join(statement.split()))
        if text.startswith("CREATE"):
            found.add(text)
    return found


def test_it_follows_the_adk_run_provenance() -> None:
    assert revision().down_revision == "0031adk_run_provenance"


def test_on_mysql_it_makes_the_models_table_and_moves_the_jobs() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    sql = output.getvalue()
    expected = statements(
        str(CreateTable(HelperJob.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
    )
    assert expected <= statements(sql)
    assert "INSERT INTO helper_jobs" in sql
    assert "DROP TABLE skill_creator_jobs" in sql


def old_tables(meta: sa.MetaData) -> sa.Table:
    sa.Table("organizations", meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("skills", meta, sa.Column("id", sa.String(36), primary_key=True))
    return sa.Table(
        "skill_creator_jobs",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("brief", sa.Text, nullable=False),
        sa.Column("revises", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skill_id", sa.String(36)),
        sa.Column("notes", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("model", sa.String(200)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), server_default="system"),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(255), server_default="system"),
    )


def test_on_sqlite_the_skill_creators_jobs_go_up_and_down() -> None:
    meta = sa.MetaData()
    jobs = old_tables(meta)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        conn.execute(meta.tables["organizations"].insert(), [{"id": ORG}])
        conn.execute(meta.tables["skills"].insert(), [{"id": "skill-1"}])
        conn.execute(
            jobs.insert(),
            [
                {
                    "id": "job-done",
                    "organization_id": ORG,
                    "brief": 'Triage "urgent" tickets',
                    "revises": "skill-1",
                    "status": "done",
                    "skill_id": "skill-1",
                    "notes": '["Check it."]',
                    "error": None,
                    "model": "openai/gpt",
                    "created_by": "member-1",
                },
                {
                    "id": "job-failed",
                    "organization_id": ORG,
                    "brief": "Anything",
                    "revises": None,
                    "status": "failed",
                    "skill_id": None,
                    "notes": "[]",
                    "error": "It took too long.",
                    "model": None,
                    "created_by": "member-1",
                },
            ],
        )
        migration = revision()
        # Timestamps' defaults as SQLite writes them.
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        tables = sa.inspect(conn).get_table_names()
        assert "helper_jobs" in tables and "skill_creator_jobs" not in tables
        moved = {
            row.id: row
            for row in conn.execute(sa.text("SELECT * FROM helper_jobs")).all()
        }
        done, failed = moved["job-done"], moved["job-failed"]
        assert done.helper == failed.helper == "skill_creator"
        assert json.loads(done.input) == {
            "brief": 'Triage "urgent" tickets',
            "revises": "skill-1",
        }
        assert json.loads(done.result) == {"skill_id": "skill-1"}
        assert (done.status, json.loads(done.notes), done.model, done.created_by) == (
            "done",
            ["Check it."],
            "openai/gpt",
            "member-1",
        )
        assert json.loads(failed.input) == {"brief": "Anything", "revises": None}
        assert failed.result is None and failed.error == "It took too long."

        # Another helper's job, and a skill deleted since, before going back.
        conn.execute(
            sa.text(
                "INSERT INTO helper_jobs (id, organization_id, helper, input, "
                "status, notes) VALUES ('job-other', :org, 'other', '{}', "
                "'running', '[]')"
            ),
            {"org": ORG},
        )
        conn.execute(sa.text("DELETE FROM skills"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        tables = sa.inspect(conn).get_table_names()
        assert "skill_creator_jobs" in tables and "helper_jobs" not in tables
        back = {
            row.id: row
            for row in conn.execute(sa.text("SELECT * FROM skill_creator_jobs")).all()
        }
        assert set(back) == {"job-done", "job-failed"}
        assert back["job-done"].brief == 'Triage "urgent" tickets'
        assert back["job-done"].revises == "skill-1"
        assert back["job-done"].skill_id is None
        assert back["job-failed"].revises is None
