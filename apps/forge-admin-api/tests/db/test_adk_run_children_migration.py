"""0021adk_run_children without MySQL: the column and index it adds are the
run store's (``forge_task_adk_workflows.run_store.metadata``), as MySQL's DDL,
and it reads a run's parent from its payload with MySQL's JSON functions; on
SQLite a run a workflow's tool started before has its parent filled in, and
no other; and the downgrade takes them back."""

import importlib.util
import io
import json
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.run_store import runs
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateColumn, CreateIndex

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0021adk_run_children_adk_run_children.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0021", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flat(sql: str) -> str:
    return " ".join(sql.split())


def test_it_follows_run_control() -> None:
    assert revision().down_revision == "0020adk_run_control"


def test_on_mysql_it_adds_the_run_stores_column_and_index_and_fills_them_in() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = flat(output.getvalue())
    dialect = mysql.dialect()
    column = flat(str(CreateColumn(runs.c.parent_run_id).compile(dialect=dialect)))
    assert f"ALTER TABLE adk_runs ADD COLUMN {column}" in made
    [index] = [index for index in runs.indexes if index.name == "ix_adk_runs_parent"]
    assert flat(str(CreateIndex(index).compile(dialect=dialect))) in made
    assert (
        "UPDATE adk_runs SET parent_run_id = "
        "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.trigger.run_id'))" in made
    )


def test_on_sqlite_a_run_a_workflows_tool_started_has_its_parent() -> None:
    engine = sa.create_engine("sqlite://")
    before = sa.MetaData()
    sa.Table(
        "adk_runs",
        before,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    trigger = {"type": "workflow", "run_id": "p" * 32}
    with engine.begin() as conn:
        before.create_all(conn)
        for row in (
            {"id": "child", "payload": {"trigger": trigger}},
            {"id": "manual", "payload": {"trigger": {"type": "manual", "by": "u-1"}}},
            {"id": "odd", "payload": {"trigger": {**trigger, "run_id": "x" * 40}}},
            {"id": "none", "payload": {}},
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO adk_runs (id, payload, created_at) "
                    "VALUES (:id, :payload, '2026-10-01 12:00:00')"
                ),
                {"id": row["id"], "payload": json.dumps(row["payload"])},
            )
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        found = dict(
            conn.execute(sa.text("SELECT id, parent_run_id FROM adk_runs")).all()
        )
        assert found == {"child": "p" * 32, "manual": None, "odd": None, "none": None}
        assert "ix_adk_runs_parent" in {
            index["name"] for index in sa.inspect(conn).get_indexes("adk_runs")
        }
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        assert [c["name"] for c in sa.inspect(conn).get_columns("adk_runs")] == [
            "id",
            "payload",
            "created_at",
        ]
