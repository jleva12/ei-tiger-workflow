"""0033exactly_once_starts without MySQL: the tables it makes are the
models', as MySQL's DDL; requests get their version (0 for those kept
already); and the downgrade takes all of it away."""

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
from forge_admin.models import ChatTurnLease, WorkflowStartIntent

REVISION = MIGRATIONS / "versions" / "0033exactly_once_starts_exactly_once_starts.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0033", REVISION)
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


def test_it_follows_the_helper_jobs() -> None:
    assert revision().down_revision == "0032helper_jobs"


def test_on_mysql_it_makes_the_models_tables() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = statements(output.getvalue())
    for model in (ChatTurnLease, WorkflowStartIntent):
        expected = statements(
            str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
        )
        assert expected <= made, model.__tablename__
    assert (
        "ALTER TABLE intake_requests ADD COLUMN version INTEGER NOT NULL DEFAULT '0'"
        in (" ".join(output.getvalue().split()))
    )


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    sa.Table("organizations", meta, sa.Column("id", sa.String(36), primary_key=True))
    requests = sa.Table(
        "intake_requests",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16)),
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        conn.execute(sa.insert(requests).values(id="r-1", status="open"))
        migration = revision()
        # Timestamps' defaults as SQLite writes them.
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        tables = sa.inspect(conn).get_table_names()
        assert {"chat_turn_leases", "workflow_start_intents"} <= set(tables)
        # A request kept already is at its first version.
        assert (
            conn.execute(sa.text("SELECT version FROM intake_requests")).scalar() == 0
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        tables = sa.inspect(conn).get_table_names()
        assert (
            "chat_turn_leases" not in tables and "workflow_start_intents" not in tables
        )
        columns = {c["name"] for c in sa.inspect(conn).get_columns("intake_requests")}
        assert "version" not in columns
