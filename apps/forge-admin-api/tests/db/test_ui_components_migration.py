"""0034ui_components without MySQL: it makes no table (the components are in
MongoDB), grants ui_components:manage to the organization's administrators
alone, and the downgrade takes the permission away with every grant of it,
later ones too."""

import importlib.util
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0034ui_components_ui_components.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0034", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_the_exactly_once_starts() -> None:
    migration = revision()
    assert migration.revision == "0034ui_components"
    assert migration.down_revision == "0033exactly_once_starts"


def test_on_sqlite_it_grants_administrators_alone_and_goes_down() -> None:
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
        # Another permission's grant, which the downgrade leaves.
        conn.execute(
            rules.insert().values(ptype="p", v0="org:member", v1="skills", v2="manage")
        )
        before = set(sa.inspect(conn).get_table_names())
        migration = revision()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert set(sa.inspect(conn).get_table_names()) == before
        grants = set(conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all())
        assert grants == {
            ("org:member", "skills", "manage"),
            ("org:admin", "ui_components", "manage"),
        }
        [(resource, action, description)] = conn.execute(
            sa.select(
                permissions.c.resource, permissions.c.action, permissions.c.description
            )
        ).all()
        assert (resource, action) == ("ui_components", "manage")
        assert "team UI components" in description
        # Given to a member through the API since: the downgrade takes it too.
        conn.execute(
            rules.insert().values(
                ptype="p", v0="org:member", v1="ui_components", v2="manage"
            )
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert set(
            conn.execute(sa.select(rules.c.v0, rules.c.v1, rules.c.v2)).all()
        ) == {("org:member", "skills", "manage")}
        assert conn.execute(sa.select(permissions)).all() == []
