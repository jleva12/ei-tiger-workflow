"""0030shared_widgets without MySQL: the table it makes is the model's, as
MySQL's DDL; it grants shared_widgets:manage to organization administrators;
and the downgrade takes all of it away."""

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
from forge_admin.models import SharedWidget

REVISION = MIGRATIONS / "versions" / "0030shared_widgets_shared_widgets.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0030", REVISION)
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


def test_it_follows_the_dashboard_layouts() -> None:
    assert revision().down_revision == "0029dashboard_layouts"


def test_on_mysql_it_makes_the_models_table() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    expected = statements(
        str(CreateTable(SharedWidget.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
    )
    assert expected <= statements(output.getvalue())


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
        migration = revision()
        # Timestamps' defaults as SQLite writes them.
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert "shared_widgets" in sa.inspect(conn).get_table_names()
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {("org:admin", "shared_widgets", "manage")}
        assert conn.execute(sa.select(permissions.c.resource)).scalars().all() == [
            "shared_widgets"
        ]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "shared_widgets" not in sa.inspect(conn).get_table_names()
        assert conn.execute(sa.select(rules)).all() == []
        assert conn.execute(sa.select(permissions)).all() == []
