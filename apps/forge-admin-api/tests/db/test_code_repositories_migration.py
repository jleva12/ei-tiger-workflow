"""0014code_repositories without MySQL: the tables it makes are the models',
as MySQL's DDL, indexes included; it adds repositories:manage for the
organization's administrators and members; and the downgrade takes all of
it away."""

import importlib.util
import io
import re
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from forge_admin.db.migrate import MIGRATIONS
from forge_admin.models import CodeIngestionJob, CodeRepository

REVISION = MIGRATIONS / "versions" / "0014code_repositories_code_repositories.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0014", REVISION)
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


def test_it_follows_the_knowledge_models() -> None:
    assert revision().down_revision == "0013knowledge_models"


def test_on_mysql_it_makes_the_models_tables() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    expected: set[str] = set()
    for table in (CodeRepository.__table__, CodeIngestionJob.__table__):
        expected |= statements(str(CreateTable(table).compile(dialect=mysql.dialect())))  # type: ignore[arg-type]
        for index in table.indexes:  # type: ignore[attr-defined]
            expected |= statements(
                str(CreateIndex(index).compile(dialect=mysql.dialect()))
            )
    assert expected <= statements(output.getvalue())


def sqlite_revision() -> ModuleType:
    """The revision, its timestamps' defaults as SQLite writes them."""
    module = revision()
    module.NOW = sa.text("CURRENT_TIMESTAMP")
    return module


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    sa.Table("organizations", meta, sa.Column("id", sa.String(36), primary_key=True))
    permissions = sa.Table(
        "authz_permissions",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("resource", sa.String),
        sa.Column("action", sa.String),
        sa.Column("description", sa.Text),
    )
    rules = sa.Table(
        "casbin_rule",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ptype", sa.String),
        sa.Column("v0", sa.String),
        sa.Column("v1", sa.String),
        sa.Column("v2", sa.String),
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        migration = sqlite_revision()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        tables = set(sa.inspect(conn).get_table_names())
        assert {"code_repositories", "code_ingestion_jobs"} <= tables
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {
            ("org:admin", "repositories", "manage"),
            ("org:member", "repositories", "manage"),
        }
        assert conn.execute(sa.select(permissions.c.resource)).scalars().all() == [
            "repositories"
        ]

        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        tables = set(sa.inspect(conn).get_table_names())
        assert not {"code_repositories", "code_ingestion_jobs"} & tables
        assert conn.execute(sa.select(rules)).all() == []
        assert conn.execute(sa.select(permissions)).all() == []
