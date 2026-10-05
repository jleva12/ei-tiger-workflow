"""0009usage without MySQL: the tables it makes are the usage store's
(``forge_task_adk_workflows.usage_store.metadata``), as MySQL's DDL, and the
downgrade drops them."""

import importlib.util
import io
import re
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.usage_store import metadata
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0009usage_usage_calls.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0009", REVISION)
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


def test_it_follows_the_knowledge_bases() -> None:
    assert revision().down_revision == "0008knowledge"


def test_on_mysql_it_makes_the_usage_stores_tables() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    dialect = mysql.dialect()
    expected = set()
    for table in metadata.sorted_tables:
        expected |= statements(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            expected |= statements(str(CreateIndex(index).compile(dialect=dialect)))
    made = statements(output.getvalue())
    assert made == expected
    # Microseconds, and a double for dollars.
    assert any("at DATETIME(6) NOT NULL" in line for line in made)
    assert any("cost DOUBLE" in line for line in made)


def test_on_sqlite_it_goes_up_and_down() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        assert {"usage_calls", "usage_invocations"} <= set(
            sa.inspect(conn).get_table_names()
        )
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        assert not {"usage_calls", "usage_invocations"} & set(
            sa.inspect(conn).get_table_names()
        )
