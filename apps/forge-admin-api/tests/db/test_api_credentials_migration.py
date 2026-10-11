"""0023api_credentials without MySQL: the tables it makes are the models', as
MySQL's DDL; it grants credentials:manage to the organization's admins and
members; and the downgrade takes all of it away."""

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
from forge_admin.models import ApiCredential, ApiCredentialOAuthFlow

REVISION = MIGRATIONS / "versions" / "0023api_credentials_api_credentials.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0023", REVISION)
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


def test_it_follows_the_run_backoff() -> None:
    assert revision().down_revision == "0022adk_run_backoff"


def test_on_mysql_it_makes_the_models_tables() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = statements(output.getvalue())
    for model in (ApiCredential, ApiCredentialOAuthFlow):
        expected = statements(
            str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]
        )
        assert expected <= made, model.__tablename__


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
        tables = sa.inspect(conn).get_table_names()
        assert {"api_credentials", "api_credential_oauth_flows"} <= set(tables)
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {
            ("org:admin", "credentials", "manage"),
            ("org:member", "credentials", "manage"),
        }
        assert conn.execute(sa.select(permissions.c.resource)).scalars().all() == [
            "credentials"
        ]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        tables = sa.inspect(conn).get_table_names()
        assert "api_credentials" not in tables
        assert "api_credential_oauth_flows" not in tables
        assert conn.execute(sa.select(rules)).all() == []
        assert conn.execute(sa.select(permissions)).all() == []
