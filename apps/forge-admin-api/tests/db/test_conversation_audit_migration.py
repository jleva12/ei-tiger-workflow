"""0024conversation_audit without MySQL: the column and indexes it adds are
the usage store's (``forge_task_adk_workflows.usage_store.metadata``), as
MySQL's DDL; it grants agents:audit to the organization's admins; and the
downgrade takes all of it back."""

import importlib.util
import io
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.usage_store import calls, invocations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateColumn

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0024conversation_audit_conversation_audit.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0024", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_api_credentials() -> None:
    assert revision().down_revision == "0023api_credentials"


def test_on_mysql_it_adds_the_usage_stores_channel_and_session_indexes() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = " ".join(output.getvalue().split())
    column = " ".join(
        str(
            CreateColumn(invocations.c.channel).compile(dialect=mysql.dialect())
        ).split()
    )
    assert f"ALTER TABLE usage_invocations ADD COLUMN {column}" in made
    for index in ("ix_usage_invocations_session", "ix_usage_calls_session"):
        assert {i.name for i in (*invocations.indexes, *calls.indexes)} >= {index}
        assert f"CREATE INDEX {index}" in made


before = sa.MetaData()
sa.Table(
    "usage_invocations",
    before,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("session_id", sa.String(128)),
)
sa.Table(
    "usage_calls",
    before,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("session_id", sa.String(128)),
)
sa.Table(
    "authz_permissions",
    before,
    sa.Column("resource", sa.String(64), primary_key=True),
    sa.Column("action", sa.String(64), primary_key=True),
    sa.Column("description", sa.Text),
)
sa.Table(
    "casbin_rule",
    before,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("ptype", sa.String(255)),
    sa.Column("v0", sa.String(255)),
    sa.Column("v1", sa.String(255)),
    sa.Column("v2", sa.String(255)),
)


def test_on_sqlite_it_grants_agents_audit_to_admins_and_goes_back() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        before.create_all(conn)
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        columns = {c["name"] for c in sa.inspect(conn).get_columns("usage_invocations")}
        assert "channel" in columns
        rules = conn.execute(sa.text("SELECT v0, v1, v2 FROM casbin_rule")).all()
        assert [tuple(r) for r in rules] == [("org:admin", "agents", "audit")]
        granted = conn.execute(
            sa.text("SELECT resource, action FROM authz_permissions")
        ).all()
        assert [tuple(r) for r in granted] == [("agents", "audit")]
        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        columns = {c["name"] for c in sa.inspect(conn).get_columns("usage_invocations")}
        assert columns == {"id", "session_id"}
        assert conn.execute(sa.text("SELECT COUNT(*) FROM casbin_rule")).scalar() == 0
        assert (
            conn.execute(sa.text("SELECT COUNT(*) FROM authz_permissions")).scalar()
            == 0
        )
