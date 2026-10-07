"""0015repositories_read without MySQL: it adds repositories:read for the
organization's roles and API caller, and the downgrade takes away every
grant of it, then it."""

import importlib.util
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0015repositories_read_repositories_read.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0015", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_the_code_repositories() -> None:
    assert revision().down_revision == "0014code_repositories"


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
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
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {
            (role, "repositories", "read")
            for role in ("org:admin", "org:member", "org:viewer", "org:api")
        }
        assert conn.execute(
            sa.select(permissions.c.resource, permissions.c.action)
        ).all() == [("repositories", "read")]

        # A grant made since goes with it; other permissions' grants stay.
        conn.execute(
            rules.insert(),
            [
                {"ptype": "p", "v0": "org:custom", "v1": "repositories", "v2": "read"},
                {"ptype": "p", "v0": "org:api", "v1": "agents", "v2": "run"},
            ],
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all() == [
            ("org:api", "agents", "run")
        ]
        assert conn.execute(sa.select(permissions)).all() == []
