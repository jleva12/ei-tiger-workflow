"""0035adk_run_interruptions without MySQL: the column it adds is the run
store's (``forge_task_adk_workflows.run_store.metadata``), as MySQL's DDL; on
SQLite a run from before was never interrupted; and the downgrade takes it
back."""

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

REVISION = (
    MIGRATIONS / "versions" / "0035adk_run_interruptions_adk_run_interruptions.py"
)


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0035", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_the_ui_components() -> None:
    assert revision().down_revision == "0034ui_components"


def test_on_mysql_it_adds_the_run_stores_column() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = " ".join(output.getvalue().split())
    column = " ".join(
        str(CreateColumn(runs.c.interruptions).compile(dialect=mysql.dialect())).split()
    )
    assert f"ALTER TABLE adk_runs ADD COLUMN {column}" in made


def test_on_sqlite_a_run_from_before_was_never_interrupted() -> None:
    engine = sa.create_engine("sqlite://")
    before = sa.MetaData()
    sa.Table("adk_runs", before, sa.Column("id", sa.String(32), primary_key=True))
    with engine.begin() as conn:
        before.create_all(conn)
        conn.execute(sa.text("INSERT INTO adk_runs (id) VALUES ('run-1')"))
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        found = conn.execute(sa.text("SELECT interruptions FROM adk_runs")).scalar_one()
        assert found == 0
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        assert [c["name"] for c in sa.inspect(conn).get_columns("adk_runs")] == ["id"]
