"""0011api_keys without MySQL: the table it makes is the model's, as MySQL's
DDL; it adds api_keys:manage, the API caller role and agents:run's new
description; and the downgrade takes all of it away, keys' roles included."""

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
from forge_admin.models import ApiKey

REVISION = MIGRATIONS / "versions" / "0011api_keys_api_keys.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0011", REVISION)
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


def test_it_follows_the_a2a_tasks() -> None:
    assert revision().down_revision == "0010a2a"


def test_on_mysql_it_makes_the_models_table() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    expected = statements(
        str(CreateTable(ApiKey.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
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
    roles = sa.Table(
        "authz_roles",
        meta,
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("name", sa.String),
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
        conn.execute(
            permissions.insert().values(
                resource="agents",
                action="run",
                description="Run the organization's ADK workflows",
            )
        )
        migration = sqlite_revision()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert "api_keys" in sa.inspect(conn).get_table_names()
        assert conn.execute(sa.select(roles.c.name)).scalars().all() == ["API caller"]
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {
            ("org:admin", "api_keys", "manage"),
            ("org:api", "agents", "run"),
        }
        run = conn.execute(
            sa.select(permissions.c.description).where(permissions.c.action == "run")
        ).scalar_one()
        assert "through the runtime" in run

        conn.execute(
            rules.insert().values(ptype="g", v0="apikey:k1", v1="org:api", v2="org:o*")
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "api_keys" not in sa.inspect(conn).get_table_names()
        assert conn.execute(sa.select(rules)).all() == []
        assert conn.execute(sa.select(roles)).all() == []
        assert conn.execute(sa.select(permissions.c.description)).scalars().all() == [
            "Run the organization's ADK workflows"
        ]
