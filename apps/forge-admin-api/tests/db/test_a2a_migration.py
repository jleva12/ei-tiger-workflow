"""0010a2a without MySQL: the table it makes is the one a2a-sdk's task store
reads and writes (``forge_agent_runtime.a2a.task_store``), as MySQL's DDL,
and the downgrade drops it."""

import importlib.util
import io
import re
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_agent_runtime.a2a import TASKS_TABLE, _task_model
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0010a2a_a2a_tasks.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0010", REVISION)
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


def test_it_follows_the_usage_tables() -> None:
    assert revision().down_revision == "0009usage"


def test_on_mysql_it_makes_a2a_sdks_task_table() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    dialect = mysql.dialect()
    table = _task_model(TASKS_TABLE).__table__
    expected = statements(str(CreateTable(table).compile(dialect=dialect)))
    for index in table.indexes:
        expected |= statements(str(CreateIndex(index).compile(dialect=dialect)))
    assert statements(output.getvalue()) == expected


def test_on_sqlite_it_goes_up_and_down() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        assert TASKS_TABLE in sa.inspect(conn).get_table_names()
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        assert TASKS_TABLE not in sa.inspect(conn).get_table_names()
