"""0029dashboard_layouts without MySQL: the table it makes is the model's, as
MySQL's DDL, and the downgrade takes it away."""

import importlib.util
import io
import re
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from forge_admin.db.migrate import MIGRATIONS
from forge_admin.models import DashboardLayout

REVISION = MIGRATIONS / "versions" / "0029dashboard_layouts_dashboard_layouts.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0029", REVISION)
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


def test_it_follows_the_intake_requests() -> None:
    assert revision().down_revision == "0028intake_requests"


def test_on_mysql_it_makes_the_models_table() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    expected = statements(
        str(CreateTable(DashboardLayout.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
    )
    assert expected <= statements(output.getvalue())


def test_on_sqlite_it_goes_up_and_down() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        migration = revision()
        # Timestamps' defaults as SQLite writes them.
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert "dashboard_layouts" in sa.inspect(conn).get_table_names()
        assert sa.inspect(conn).get_pk_constraint("dashboard_layouts")[
            "constrained_columns"
        ] == ["user_id", "dashboard"]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "dashboard_layouts" not in sa.inspect(conn).get_table_names()
