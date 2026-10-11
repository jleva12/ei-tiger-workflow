"""0019system_designs without MySQL: the tables it makes and the columns it
adds are the models', as MySQL's DDL, indexes included; a connection's ends
may then be components; and the downgrade takes it all back, with the
connections to and from components."""

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
from forge_admin.models import KnowledgeBaseComponent, KnowledgeBaseGroup

VERSIONS = MIGRATIONS / "versions"
TABLES = ("knowledge_base_groups", "knowledge_base_components")


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revision() -> ModuleType:
    return load("0019system_designs_system_designs")


def statements(sql: str) -> set[str]:
    """DDL as comparable text: one line each, without constraint names."""
    found = set()
    for statement in sql.split(";"):
        text = re.sub(r"CONSTRAINT \w+ ", "", " ".join(statement.split()))
        if text.startswith(("CREATE", "ALTER")):
            found.add(text)
    return found


def test_it_follows_system_maps() -> None:
    assert revision().down_revision == "0018system_maps"


def test_on_mysql_it_makes_the_models_tables_and_columns() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = statements(output.getvalue())
    dialect = mysql.dialect()
    for model in (KnowledgeBaseGroup, KnowledgeBaseComponent):
        table = model.__table__
        expected = statements(str(CreateTable(table).compile(dialect=dialect)))  # type: ignore[arg-type]
        for index in table.indexes:  # type: ignore[attr-defined]
            expected |= statements(str(CreateIndex(index).compile(dialect=dialect)))
        assert expected <= made, model
    text = " ".join(output.getvalue().split())
    for side in ("source", "target"):
        assert (
            f"ALTER TABLE knowledge_base_connections MODIFY {side}_repository_id "
            "VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL" in text
        )
        assert (
            f"ALTER TABLE knowledge_base_connections ADD FOREIGN KEY"
            f"({side}_component_id) REFERENCES knowledge_base_components (id) "
            "ON DELETE CASCADE" in re.sub(r"CONSTRAINT \w+ ", "", text)
        )
    assert (
        "ALTER TABLE knowledge_base_repositories ADD COLUMN group_id VARCHAR(36) "
        "CHARACTER SET ascii COLLATE ascii_bin" in text
    )
    # MySQL names constraints in at most 64 characters.
    names = re.findall(r"CONSTRAINT (\w+)", output.getvalue())
    assert names and all(len(name) <= 64 for name in names)


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    sa.Table(
        "knowledge_bases",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16)),
    )
    sa.Table(
        "knowledge_base_repositories",
        meta,
        sa.Column("knowledge_base_id", sa.String(36), primary_key=True),
        sa.Column("repository_id", sa.String(36), primary_key=True),
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        earlier = [
            load("0017system_knowledge_bases_system_knowledge_bases"),
            load("0018system_maps_system_maps"),
        ]
        migration = revision()
        for module in (*earlier, migration):
            module.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            for module in earlier:
                module.upgrade()
            migration.upgrade()
        assert set(TABLES) <= set(sa.inspect(conn).get_table_names())
        conn.execute(
            sa.text(
                "INSERT INTO knowledge_base_repositories (knowledge_base_id, "
                "repository_id) VALUES ('kb', 'r1'), ('kb', 'r2')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO knowledge_base_components (id, knowledge_base_id, kind, "
                "name, technology, description) VALUES ('c1', 'kb', 'database', "
                "'db', '', '')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO knowledge_base_connections (id, knowledge_base_id, "
                "source_repository_id, target_repository_id, target_component_id, "
                "kind, description) VALUES ('apps', 'kb', 'r1', 'r2', NULL, 'calls', "
                "''), ('db', 'kb', 'r1', NULL, 'c1', 'writes_to', '')"
            )
        )

        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert not set(TABLES) & set(sa.inspect(conn).get_table_names())
        # The connection to a component goes; the one between applications stays.
        ids = conn.execute(sa.text("SELECT id FROM knowledge_base_connections"))
        assert [row[0] for row in ids] == ["apps"]
        columns = {
            c["name"]: c
            for c in sa.inspect(conn).get_columns("knowledge_base_connections")
        }
        assert "source_component_id" not in columns
        assert not columns["source_repository_id"]["nullable"]
        assert "group_id" not in {
            c["name"]
            for c in sa.inspect(conn).get_columns("knowledge_base_repositories")
        }
