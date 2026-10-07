"""0016graph_knowledge_bases without MySQL: the link table it makes is the
model's, as MySQL's DDL, index included; knowledge bases gain their kind,
the ones there were being of documents; and the downgrade takes both away."""

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
from forge_admin.models import KnowledgeBase, KnowledgeBaseRepository

REVISION = (
    MIGRATIONS / "versions" / "0016graph_knowledge_bases_graph_knowledge_bases.py"
)


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0016", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def statements(sql: str) -> set[str]:
    """DDL as comparable text: one line each, without constraint names."""
    found = set()
    for statement in sql.split(";"):
        text = re.sub(r"CONSTRAINT \w+ ", "", " ".join(statement.split()))
        if text.startswith(("CREATE", "ALTER")):
            found.add(text)
    return found


def test_it_follows_repositories_read() -> None:
    assert revision().down_revision == "0015repositories_read"


def test_on_mysql_it_makes_the_models_table_and_column() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    table = KnowledgeBaseRepository.__table__
    expected = statements(str(CreateTable(table).compile(dialect=mysql.dialect())))  # type: ignore[arg-type]
    for index in table.indexes:  # type: ignore[attr-defined]
        expected |= statements(str(CreateIndex(index).compile(dialect=mysql.dialect())))
    made = statements(output.getvalue())
    assert expected <= made
    # The kind column as the model has it.
    kind = KnowledgeBase.__table__.c.kind  # type: ignore[attr-defined]
    column = kind.type.compile(dialect=mysql.dialect())
    assert (
        f"ALTER TABLE knowledge_bases ADD COLUMN kind {column} NOT NULL DEFAULT 'rag'"
        in made
    )


def test_on_sqlite_it_goes_up_and_down() -> None:
    meta = sa.MetaData()
    knowledge_bases = sa.Table(
        "knowledge_bases",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200)),
    )
    sa.Table(
        "code_repositories", meta, sa.Column("id", sa.String(36), primary_key=True)
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        meta.create_all(conn)
        conn.execute(knowledge_bases.insert().values(id="kb1", name="HR policies"))
        migration = revision()
        migration.NOW = sa.text("CURRENT_TIMESTAMP")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        inspector = sa.inspect(conn)
        assert "knowledge_base_repositories" in inspector.get_table_names()
        assert conn.execute(sa.text("SELECT kind FROM knowledge_bases")).all() == [
            ("rag",)
        ]

        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        inspector = sa.inspect(conn)
        assert "knowledge_base_repositories" not in inspector.get_table_names()
        assert "kind" not in {
            c["name"] for c in inspector.get_columns("knowledge_bases")
        }
