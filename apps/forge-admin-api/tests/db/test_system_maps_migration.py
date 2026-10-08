"""0018system_maps without MySQL: the columns it adds are the models', and
the downgrade takes them away."""

import importlib.util
import io
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql

from forge_admin.db.migrate import MIGRATIONS
from forge_admin.models import KnowledgeBase, KnowledgeBaseRepository

REVISION = MIGRATIONS / "versions" / "0018system_maps_system_maps.py"


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0018", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_system_knowledge_bases() -> None:
    assert revision().down_revision == "0017system_knowledge_bases"


def test_on_mysql_it_adds_the_models_columns() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = " ".join(output.getvalue().split())
    dialect = mysql.dialect()
    for table, column in (
        (KnowledgeBase.__table__, "map_layout"),
        (KnowledgeBaseRepository.__table__, "map_x"),
        (KnowledgeBaseRepository.__table__, "map_y"),
    ):
        kind = table.c[column].type.compile(dialect=dialect)  # type: ignore[attr-defined]
        assert f"ALTER TABLE {table.name} ADD COLUMN {column} {kind};" in made  # type: ignore[attr-defined]


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    sa.Table("knowledge_bases", meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table(
        "knowledge_base_repositories",
        meta,
        sa.Column("knowledge_base_id", sa.String(36), primary_key=True),
        sa.Column("repository_id", sa.String(36), primary_key=True),
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        migration = revision()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        columns = {
            c["name"]
            for c in sa.inspect(conn).get_columns("knowledge_base_repositories")
        }
        assert {"map_x", "map_y"} <= columns
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        columns = {
            c["name"]
            for c in sa.inspect(conn).get_columns("knowledge_base_repositories")
        }
        assert not {"map_x", "map_y"} & columns
