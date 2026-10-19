"""0031adk_run_provenance without MySQL: the columns, indexes and table it
adds are the run store's (``forge_task_adk_workflows.run_store.metadata``), as
MySQL's DDL, and it fills the runs from before in with MySQL's JSON functions;
on SQLite each kind of run from before has what its trigger and requester
tell, the runs runs started their starters' person and root, and the
downgrade takes it all back."""

import importlib.util
import io
import json
import re
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge_task_adk_workflows.run_store import (
    PROVENANCE_COLUMNS,
    refusals,
    runs,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

from forge_admin.db.migrate import MIGRATIONS

REVISION = MIGRATIONS / "versions" / "0031adk_run_provenance_adk_run_provenance.py"
INDEXES = ("ix_adk_runs_trigger", "ix_adk_runs_on_behalf_of")


def revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revision_0031", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flat(sql: str) -> str:
    return re.sub(r"CONSTRAINT \w+ ", "", " ".join(sql.split()))


def test_it_follows_shared_widgets() -> None:
    assert revision().down_revision == "0030shared_widgets"


def test_on_mysql_it_adds_the_run_stores_columns_indexes_and_table() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        revision().upgrade()
    made = flat(output.getvalue())
    dialect = mysql.dialect()
    for name in (*PROVENANCE_COLUMNS, "root_run_id"):
        column = flat(str(CreateColumn(runs.c[name]).compile(dialect=dialect)))
        assert f"ALTER TABLE adk_runs ADD COLUMN {column}" in made
    for index in runs.indexes:
        if index.name in INDEXES:
            assert flat(str(CreateIndex(index).compile(dialect=dialect))) in made
    assert flat(str(CreateTable(refusals).compile(dialect=dialect))) in made
    [index] = refusals.indexes
    assert flat(str(CreateIndex(index).compile(dialect=dialect))) in made
    # Filled in from the payload's trigger; null JSON is null, not 'null'.
    assert (
        "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.trigger.type')), 'null')" in made
    )
    # Each level of runs runs started, as deep as written-out SQL goes.
    joined = "UPDATE adk_runs AS c JOIN adk_runs AS p ON c.parent_run_id = p.id"
    assert made.count(joined) == revision().OFFLINE_DEPTH


# ------------------------------------------------------------------ SQLite

before = sa.MetaData()
runs_before = sa.Table(
    "adk_runs",
    before,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("organization_id", sa.String(64), nullable=False),
    sa.Column("agent_id", sa.String(64), nullable=False),
    sa.Column("version", sa.String(16), nullable=True),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("requested_by", sa.String(64), nullable=False),
    sa.Column("requested_by_name", sa.String(255), nullable=False),
    sa.Column("resubmit_of", sa.String(32), nullable=True),
    sa.Column("parent_run_id", sa.String(32), nullable=True),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

ORG = "org-1"
#: Runs as each start path left them before: (id, requested_by, name, trigger,
#: what's more).
BEFORE: list[tuple[str, str, str, dict[str, Any] | None, dict[str, Any]]] = [
    ("manual", "u-ada", "Ada", {"type": "manual", "by": "u-ada"}, {}),
    (
        "keyed",
        "apikey:k-1",
        "API key Billing",
        {"type": "manual", "by": "apikey:k-1"},
        {},
    ),
    (
        "rest",
        "u-bo",
        "Bo",
        {"type": "runtime", "protocol": "rest", "caller": "u-bo", "ref": "ag_x"},
        {},
    ),
    (
        "anon",
        "anonymous",
        "Anonymous caller",
        {"type": "runtime", "protocol": "a2a", "caller": None, "ref": "ag_x"},
        {},
    ),
    (
        "keyrest",
        "apikey:k-2",
        "API key Ops",
        {"type": "runtime", "protocol": "rest", "caller": "apikey:k-2"},
        {},
    ),
    (
        "chat",
        "chat-agent:ca_desk",
        "Chat agent desk",
        {"type": "chat_agent", "agent_id": "ca_desk", "user": "visitor-9"},
        {"version": "3"},
    ),
    (
        "child",
        "chat-agent:ca_desk",
        "Chat agent desk",
        {"type": "workflow", "run_id": "chat", "agent_id": "ag-1"},
        {"parent_run_id": "chat", "version": "draft"},
    ),
    (
        "grandchild",
        "chat-agent:ca_desk",
        "Chat agent desk",
        {"type": "workflow", "run_id": "child", "agent_id": "ag-2"},
        {"parent_run_id": "child"},
    ),
    (
        "orphan",
        "u-ada",
        "Ada",
        {"type": "workflow", "run_id": "gone"},
        {"parent_run_id": "gone"},
    ),
    # Resubmitted by Cy from a chat agent's run: it carried that one's trigger.
    (
        "again",
        "u-cy",
        "Cy",
        {"type": "chat_agent", "agent_id": "ca_desk", "user": "visitor-9"},
        {"resubmit_of": "chat"},
    ),
    ("bare", "u-ada", "Ada", None, {}),
]


def test_on_sqlite_runs_from_before_are_filled_in_from_their_triggers() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        before.create_all(conn)
        for run_id, by, name, trigger, more in BEFORE:
            conn.execute(
                runs_before.insert().values(
                    id=run_id,
                    organization_id=ORG,
                    agent_id="ag-1" if run_id != "grandchild" else "ag-3",
                    payload={} if trigger is None else {"trigger": trigger},
                    requested_by=by,
                    requested_by_name=name,
                    created_at=sa.func.current_timestamp(),
                    **more,
                )
            )
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        columns = (*PROVENANCE_COLUMNS, "root_run_id")
        found = {
            row["id"]: {k: row[k] for k in columns if row[k] is not None}
            for row in conn.execute(
                sa.text(f"SELECT id, {', '.join(columns)} FROM adk_runs")
            ).mappings()
        }

        assert found["manual"] == {
            "trigger_type": "manual",
            "on_behalf_of": "u-ada",
            "on_behalf_of_name": "Ada",
            "on_behalf_of_verified": 1,
            "auth_method": "jwt",
            "root_run_id": "manual",
        }
        assert found["keyed"] == {
            "trigger_type": "api_key",
            "channel": "api",
            "on_behalf_of": "apikey:k-1",
            "on_behalf_of_name": "API key Billing",
            "on_behalf_of_verified": 1,
            "api_key_id": "k-1",
            "auth_method": "api_key",
            "root_run_id": "keyed",
        }
        assert found["rest"] == {
            "trigger_type": "runtime_rest",
            "channel": "api",
            "on_behalf_of": "u-bo",
            "on_behalf_of_name": "Bo",
            "on_behalf_of_verified": 1,
            "auth_method": "jwt",
            "root_run_id": "rest",
        }
        assert found["anon"] == {
            "trigger_type": "a2a",
            "channel": "a2a",
            "on_behalf_of": "anonymous",
            "on_behalf_of_name": "Anonymous caller",
            "on_behalf_of_verified": 0,
            "auth_method": "anonymous",
            "root_run_id": "anon",
        }
        assert (found["keyrest"]["api_key_id"], found["keyrest"]["auth_method"]) == (
            "k-2",
            "api_key",
        )
        # The conversation's person, not the agent; whether they were verified, nobody knows.
        assert found["chat"] == {
            "trigger_type": "chat_agent",
            "on_behalf_of": "visitor-9",
            "via_agent_id": "ca_desk",
            "root_run_id": "chat",
        }
        # Its starter's person and root, via its starter's workflow.
        assert found["child"] == {
            "trigger_type": "workflow",
            "channel": "worker",
            "on_behalf_of": "visitor-9",
            "via_agent_id": "ag-1",
            "via_agent_version": "3",
            "root_run_id": "chat",
        }
        assert found["grandchild"] == {
            "trigger_type": "workflow",
            "channel": "worker",
            "on_behalf_of": "visitor-9",
            "via_agent_id": "ag-1",
            "via_agent_version": "draft",
            "root_run_id": "chat",
        }
        assert found["orphan"] == {
            "trigger_type": "workflow",
            "channel": "worker",
            "root_run_id": "gone",
        }
        # The resubmitter's, whatever trigger it carried.
        assert found["again"] == {
            "trigger_type": "resubmit",
            "on_behalf_of": "u-cy",
            "on_behalf_of_name": "Cy",
            "on_behalf_of_verified": 1,
            "auth_method": "jwt",
            "root_run_id": "again",
        }
        assert found["bare"] == {"root_run_id": "bare"}

        indexes = {index["name"] for index in sa.inspect(conn).get_indexes("adk_runs")}
        assert set(INDEXES) <= indexes
        assert "adk_run_refusals" in sa.inspect(conn).get_table_names()

        with Operations.context(MigrationContext.configure(conn)):
            revision().downgrade()
        inspector = sa.inspect(conn)
        assert [c["name"] for c in inspector.get_columns("adk_runs")] == [
            c.name for c in runs_before.columns
        ]
        assert "adk_run_refusals" not in inspector.get_table_names()


def test_on_sqlite_a_run_store_fresh_from_it_takes_runs() -> None:
    """What the run store writes fits what the migration made."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        before.create_all(conn)
        with Operations.context(MigrationContext.configure(conn)):
            revision().upgrade()
        conn.execute(
            runs_before.insert().values(
                id="r1",
                organization_id=ORG,
                agent_id="ag-1",
                payload=json.loads("{}"),
                requested_by="u-ada",
                requested_by_name="Ada",
                created_at=sa.func.current_timestamp(),
            )
        )
        conn.execute(
            sa.text(
                "UPDATE adk_runs SET trigger_type = 'manual', "
                "on_behalf_of_verified = 1 WHERE id = 'r1'"
            )
        )
        assert conn.execute(
            sa.select(runs.c.trigger_type, runs.c.on_behalf_of_verified)
        ).one() == ("manual", True)
