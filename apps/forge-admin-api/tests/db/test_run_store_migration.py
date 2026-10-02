"""0005adk_run_store without MySQL: the tables it makes are the run store's
(``forge_task_adk_workflows.run_store.metadata``), as MySQL's DDL and on
SQLite, and it renames background_tasks:manage to agents:manage_runs with
every grant of it, and back."""

import importlib.util
import io
import re
from collections.abc import Iterator
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.run_store import metadata
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0005adk_run_store_adk_run_store.py"
OLD_DESCRIPTION = "Resubmit, restart and abandon the organization's ADK workflow runs"
NEW_DESCRIPTION = "Retry, resubmit and abandon the organization's ADK workflow runs"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0005", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statements(sql: str) -> set[str]:
    """DDL as comparable text: one line each, without constraint names (MySQL
    names a primary key PRIMARY whatever it's called)."""
    found = set()
    for statement in sql.split(";"):
        text = re.sub(r"CONSTRAINT \w+ ", "", " ".join(statement.split()))
        if text.startswith("CREATE"):
            found.add(text)
    return found


def test_on_mysql_it_makes_the_run_stores_tables() -> None:
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
    # DATETIME(6), JSON and the cascade, as the run store relies on them.
    assert any("created_at DATETIME(6) NOT NULL" in line for line in made)
    assert any("payload JSON NOT NULL" in line for line in made)
    assert any("ON DELETE CASCADE" in line for line in made)


# ------------------------------------------------------------------ SQLite

before = sa.MetaData()
permissions = sa.Table(
    "authz_permissions",
    before,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("resource", sa.String(255), nullable=False),
    sa.Column("action", sa.String(100), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.UniqueConstraint("resource", "action"),
)
rules = sa.Table(
    "casbin_rule",
    before,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("ptype", sa.String(8), nullable=False),
    sa.Column("v0", sa.String(255)),
    sa.Column("v1", sa.String(255)),
    sa.Column("v2", sa.String(255)),
    sa.UniqueConstraint("ptype", "v0", "v1", "v2"),
)


@pytest.fixture
def connection() -> Iterator[sa.Connection]:
    """A database as 0004adk_only leaves it, abridged: the permission, the
    default grant of it and another role's, and what isn't it."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        before.create_all(conn)
        conn.execute(
            permissions.insert(),
            [
                {
                    "resource": "background_tasks",
                    "action": "manage",
                    "description": OLD_DESCRIPTION,
                },
                {"resource": "agents", "action": "run", "description": "Run"},
            ],
        )
        conn.execute(
            rules.insert(),
            [
                {
                    "ptype": "p",
                    "v0": "org:admin",
                    "v1": "background_tasks",
                    "v2": "manage",
                },
                {
                    "ptype": "p",
                    "v0": "org:ops",
                    "v1": "background_tasks",
                    "v2": "manage",
                },
                {"ptype": "p", "v0": "org:member", "v1": "agents", "v2": "run"},
                {"ptype": "g", "v0": "u-1", "v1": "org:admin", "v2": "org:1*"},
            ],
        )
        yield conn
    engine.dispose()


def migrate(conn: sa.Connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        getattr(revision(), step)()


def grants(conn: sa.Connection) -> set[tuple[str | None, ...]]:
    found = conn.execute(sa.select(rules.c.ptype, rules.c.v0, rules.c.v1, rules.c.v2))
    return {tuple(row) for row in found}


def permission_rows(conn: sa.Connection) -> set[tuple[str, str, str]]:
    found = conn.execute(
        sa.select(
            permissions.c.resource, permissions.c.action, permissions.c.description
        )
    )
    return {tuple(row) for row in found}


def test_on_sqlite_it_round_trips(connection: sa.Connection) -> None:
    migrate(connection, "upgrade")
    inspector = sa.inspect(connection)
    for table in metadata.sorted_tables:
        columns = inspector.get_columns(table.name)
        assert [(c["name"], c["nullable"]) for c in columns] == [
            (c.name, c.nullable) for c in table.columns
        ]
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == ["id"]
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(table.name)
        } == {
            (index.name, tuple(c.name for c in index.columns))
            for index in table.indexes
        }
    [foreign_key] = inspector.get_foreign_keys("adk_run_events")
    assert (foreign_key["constrained_columns"], foreign_key["referred_table"]) == (
        ["run_id"],
        "adk_runs",
    )
    assert foreign_key["options"].get("ondelete") == "CASCADE"

    assert permission_rows(connection) == {
        ("agents", "manage_runs", NEW_DESCRIPTION),
        ("agents", "run", "Run"),
    }
    assert grants(connection) == {
        ("p", "org:admin", "agents", "manage_runs"),
        ("p", "org:ops", "agents", "manage_runs"),
        ("p", "org:member", "agents", "run"),
        ("g", "u-1", "org:admin", "org:1*"),
    }

    migrate(connection, "downgrade")
    assert not {"adk_runs", "adk_run_events"} & set(
        sa.inspect(connection).get_table_names()
    )
    assert permission_rows(connection) == {
        ("background_tasks", "manage", OLD_DESCRIPTION),
        ("agents", "run", "Run"),
    }
    assert ("p", "org:ops", "background_tasks", "manage") in grants(connection)


def test_an_edited_description_is_left_alone(connection: sa.Connection) -> None:
    connection.execute(
        permissions.update()
        .where(permissions.c.resource == "background_tasks")
        .values(description="Ours")
    )
    migrate(connection, "upgrade")
    assert ("agents", "manage_runs", "Ours") in permission_rows(connection)
    migrate(connection, "downgrade")
    assert ("background_tasks", "manage", "Ours") in permission_rows(connection)
