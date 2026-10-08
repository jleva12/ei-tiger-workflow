"""0017system_knowledge_bases without MySQL: the tables it makes are the
models', as MySQL's DDL, indexes included; graph knowledge bases become
system design ones; and the downgrade takes it all back."""

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
from forge_admin.models import KnowledgeBaseCodeLink, KnowledgeBaseConnection

REVISION = (
    MIGRATIONS / "versions" / "0017system_knowledge_bases_system_knowledge_bases.py"
)
TABLES = ("knowledge_base_connections", "knowledge_base_code_links")


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0017", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statements(sql: str) -> set[str]:
    """DDL as comparable text: one line each, without constraint names."""
    found = set()
    for statement in sql.split(";"):
        text = re.sub(r"CONSTRAINT \w+ ", "", " ".join(statement.split()))
        if text.startswith(("CREATE", "ALTER", "UPDATE")):
            found.add(text)
    return found


def test_it_follows_graph_knowledge_bases() -> None:
    assert revision().down_revision == "0016graph_knowledge_bases"


def test_on_mysql_it_makes_the_models_tables() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = statements(output.getvalue())
    for model in (KnowledgeBaseConnection, KnowledgeBaseCodeLink):
        table = model.__table__
        expected = statements(str(CreateTable(table).compile(dialect=mysql.dialect())))  # type: ignore[arg-type]
        for index in table.indexes:  # type: ignore[attr-defined]
            expected |= statements(
                str(CreateIndex(index).compile(dialect=mysql.dialect()))
            )
        assert expected <= made, model
    assert "UPDATE knowledge_bases SET kind = 'system' WHERE kind = 'graph'" in made
    # MySQL names constraints in at most 64 characters.
    names = re.findall(r"CONSTRAINT (\w+)", output.getvalue())
    assert names and all(len(name) <= 64 for name in names)


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    knowledge_bases = sa.Table(
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
        conn.execute(
            knowledge_bases.insert(),
            [{"id": "kb1", "kind": "graph"}, {"id": "kb2", "kind": "rag"}],
        )
        migration = revision()
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert set(TABLES) <= set(sa.inspect(conn).get_table_names())
        kinds = "SELECT id, kind FROM knowledge_bases ORDER BY id"
        assert conn.execute(sa.text(kinds)).all() == [("kb1", "system"), ("kb2", "rag")]

        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert not set(TABLES) & set(sa.inspect(conn).get_table_names())
        assert conn.execute(sa.text(kinds)).all() == [("kb1", "graph"), ("kb2", "rag")]
