"""0020adk_run_control without MySQL: the columns it adds are the run store's
(``forge_task_adk_workflows.run_store.metadata``), as MySQL's DDL; on SQLite
a run from before has taken nobody's lease yet and isn't being abandoned;
and the downgrade takes both back."""

import importlib.util
import io
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.run_store import runs
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateColumn

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0020adk_run_control_adk_run_control.py"
COLUMNS = ("lease_epoch", "cancel_requested_at")


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0020", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_system_designs() -> None:
    assert revision().down_revision == "0019system_designs"


def test_on_mysql_it_adds_the_run_stores_columns() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = " ".join(output.getvalue().split())
    dialect = mysql.dialect()
    for name in COLUMNS:
        column = " ".join(
            str(CreateColumn(runs.c[name]).compile(dialect=dialect)).split()
        )
        assert f"ALTER TABLE adk_runs ADD COLUMN {column}" in made, column


def test_on_sqlite_a_run_from_before_is_nobodys_and_isnt_being_abandoned() -> None:
    engine = sa.create_engine("sqlite://")
    before = sa.MetaData()
    sa.Table("adk_runs", before, sa.Column("id", sa.String(32), primary_key=True))
    with engine.begin() as conn:
        before.create_all(conn)
        conn.execute(sa.text("INSERT INTO adk_runs (id) VALUES ('run-1')"))
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        row = conn.execute(
            sa.text("SELECT lease_epoch, cancel_requested_at FROM adk_runs")
        ).one()
        assert tuple(row) == (0, None)
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        assert [c["name"] for c in sa.inspect(conn).get_columns("adk_runs")] == ["id"]
