"""adk_run_provenance: how each ADK workflow run was started, for an audit

A run kept who requested it (requested_by: a member, an API key, a chat agent,
"anonymous") and, in its payload, a free-form trigger. adk_runs now keeps how
it was started, as columns an audit can filter by
(forge_task_adk_workflows.run_store.Provenance): what started it
(trigger_type), through what (channel), for whom (on_behalf_of, its name, and
whether their own credential said so: on_behalf_of_verified), via which agent
and version (via_agent_id, via_agent_version), with which API key
(api_key_id) and credential (auth_method), from where (client_ip, user_agent,
request_id), and the top of its parent chain (root_run_id: itself, for a run
nothing started). The runs from before have what their trigger and requester
tell filled in (a run a run started takes its starter's person, key and
root), the rest left null: whether a chat agent's person was verified, and
any request's origin. adk_run_refusals keeps the starts the admin API refused
(403, 422), with the same columns.

The run store reads and writes them, so this runs before the workers and the
admin API that use them.

Revision ID: 0031adk_run_provenance
Revises: 0030shared_widgets
Create Date: 2026-10-17 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0031adk_run_provenance"
down_revision: str | Sequence[str] | None = "0030shared_widgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
EVENT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

#: How deep the runs runs start go, as far as a migration written out as SQL
#: (offline) fills them in; online it carries on until none is left.
OFFLINE_DEPTH = 16


def provenance_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    """As the run store has them (run_store._provenance_columns)."""
    return [
        sa.Column("trigger_type", sa.String(16), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("on_behalf_of", sa.String(255), nullable=True),
        sa.Column("on_behalf_of_name", sa.String(255), nullable=True),
        sa.Column("on_behalf_of_verified", sa.Boolean, nullable=True),
        sa.Column("via_agent_id", sa.String(64), nullable=True),
        sa.Column("via_agent_version", sa.String(16), nullable=True),
        sa.Column("api_key_id", sa.String(64), nullable=True),
        sa.Column("auth_method", sa.String(16), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
    ]


def trigger(dialect: str, field: str) -> str:
    """:return: The dialect's read of a run's ``payload.trigger.<field>``, as
    text; null when it's missing or null."""
    if dialect == "mysql":
        found = f"JSON_UNQUOTE(JSON_EXTRACT(payload, '$.trigger.{field}'))"
        # JSON's null unquotes to 'null'.
        return f"NULLIF({found}, 'null')"
    return f"json_extract(payload, '$.trigger.{field}')"


def backfill(dialect: str) -> str:
    """
    :return: What fills in a run from before from its trigger and requester:
        a resubmission is its resubmitter's (it carried its original's
        trigger); a run a run started (``workflow``) takes its starter's
        person, key and credential afterwards (:func:`from_parents`); a
        manual run by an API key is the key's; a runtime run is its caller's
        (unverified when anonymous); a chat agent's is its conversation's
        user's (verified or not, nobody knows now).
    """
    kind = trigger(dialect, "type")
    protocol = trigger(dialect, "protocol")
    caller = trigger(dialect, "caller")
    user = trigger(dialect, "user")
    agent = trigger(dialect, "agent_id")
    key = "SUBSTR(requested_by, 1, 7) = 'apikey:'"
    resubmit = "resubmit_of IS NOT NULL"
    workflow = f"({kind} = 'workflow' OR parent_run_id IS NOT NULL)"
    manual = f"{kind} = 'manual'"
    runtime = f"{kind} = 'runtime'"
    chat = f"{kind} = 'chat_agent'"
    # Requested by a person or key, who said who they were (or nobody, anonymous).
    own = f"({resubmit} OR {manual} OR {runtime})"
    return f"""
UPDATE adk_runs SET
  trigger_type = CASE
    WHEN {resubmit} THEN 'resubmit'
    WHEN {workflow} THEN 'workflow'
    WHEN {manual} AND {key} THEN 'api_key'
    WHEN {manual} THEN 'manual'
    WHEN {runtime} AND {protocol} = 'a2a' THEN 'a2a'
    WHEN {runtime} THEN 'runtime_rest'
    WHEN {chat} THEN 'chat_agent'
  END,
  channel = CASE
    WHEN {resubmit} THEN NULL
    WHEN {workflow} THEN 'worker'
    WHEN {manual} AND {key} THEN 'api'
    WHEN {runtime} AND {protocol} = 'a2a' THEN 'a2a'
    WHEN {runtime} THEN 'api'
  END,
  on_behalf_of = CASE
    WHEN {resubmit} THEN requested_by
    WHEN {workflow} THEN NULL
    WHEN {own} THEN requested_by
    WHEN {chat} THEN SUBSTR({user}, 1, 255)
  END,
  on_behalf_of_name = CASE
    WHEN {resubmit} THEN requested_by_name
    WHEN {workflow} THEN NULL
    WHEN {own} THEN requested_by_name
  END,
  on_behalf_of_verified = CASE
    WHEN {resubmit} THEN 1
    WHEN {workflow} THEN NULL
    WHEN {manual} THEN 1
    WHEN {runtime} AND {caller} IS NULL THEN 0
    WHEN {runtime} THEN 1
  END,
  via_agent_id = CASE
    WHEN {resubmit} OR {workflow} THEN NULL
    WHEN {chat} THEN SUBSTR({agent}, 1, 64)
  END,
  api_key_id = CASE
    WHEN {resubmit} AND {key} THEN SUBSTR(requested_by, 8, 64)
    WHEN {workflow} THEN NULL
    WHEN ({manual} OR {runtime}) AND {key} THEN SUBSTR(requested_by, 8, 64)
  END,
  auth_method = CASE
    WHEN {resubmit} AND {key} THEN 'api_key'
    WHEN {resubmit} THEN 'jwt'
    WHEN {workflow} THEN NULL
    WHEN {manual} AND {key} THEN 'api_key'
    WHEN {manual} THEN 'jwt'
    WHEN {runtime} AND {caller} IS NULL THEN 'anonymous'
    WHEN {runtime} AND {key} THEN 'api_key'
    WHEN {runtime} THEN 'jwt'
  END
WHERE trigger_type IS NULL
"""


#: What a run a run started takes from its starter.
INHERITED = (
    "on_behalf_of",
    "on_behalf_of_name",
    "on_behalf_of_verified",
    "api_key_id",
    "auth_method",
)


def from_parents(dialect: str) -> str:
    """:return: One level of what fills in the runs runs started from their
    starters: their root, person, key and credential, and the workflow they
    were started via; for those whose starter has its root already."""
    taken = {
        "root_run_id": "p.root_run_id",
        **{column: f"p.{column}" for column in INHERITED},
        "via_agent_id": "p.agent_id",
        "via_agent_version": "p.version",
    }
    if dialect == "mysql":
        assignments = ", ".join(
            f"c.{column} = {value}" for column, value in taken.items()
        )
        return (
            "UPDATE adk_runs AS c JOIN adk_runs AS p ON c.parent_run_id = p.id "
            f"SET {assignments} "
            "WHERE c.root_run_id IS NULL AND p.root_run_id IS NOT NULL"
        )
    assignments = ", ".join(f"{column} = {value}" for column, value in taken.items())
    return (
        f"UPDATE adk_runs SET {assignments} FROM adk_runs AS p "
        "WHERE adk_runs.parent_run_id = p.id "
        "AND adk_runs.root_run_id IS NULL AND p.root_run_id IS NOT NULL"
    )


def upgrade() -> None:
    for column in provenance_columns():
        op.add_column("adk_runs", column)
    op.add_column("adk_runs", sa.Column("root_run_id", sa.String(32), nullable=True))
    op.create_index(
        "ix_adk_runs_trigger",
        "adk_runs",
        ["organization_id", "trigger_type", "created_at"],
    )
    op.create_index(
        "ix_adk_runs_on_behalf_of",
        "adk_runs",
        ["organization_id", "on_behalf_of", "created_at"],
    )
    op.create_table(
        "adk_run_refusals",
        sa.Column("id", EVENT_ID, primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(16), nullable=True),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("requested_by_name", sa.String(255), nullable=True),
        *provenance_columns(),
        sa.Column("at", DATETIME, nullable=False),
    )
    op.create_index(
        "ix_adk_run_refusals_organization",
        "adk_run_refusals",
        ["organization_id", "at"],
    )
    context = op.get_context()
    dialect = context.dialect.name
    if dialect not in ("mysql", "sqlite"):
        return
    op.execute(backfill(dialect))
    # A run nothing started is its own root; those runs started take their
    # starters', a level at a time.
    op.execute(
        "UPDATE adk_runs SET root_run_id = id "
        "WHERE root_run_id IS NULL AND parent_run_id IS NULL"
    )
    if context.as_sql:
        for _ in range(OFFLINE_DEPTH):
            op.execute(from_parents(dialect))
    else:
        bind = op.get_bind()
        for _ in range(1000):
            if not bind.execute(sa.text(from_parents(dialect))).rowcount:
                break
    # One whose starter is gone: under its starter's ID all the same.
    op.execute(
        "UPDATE adk_runs SET root_run_id = parent_run_id "
        "WHERE root_run_id IS NULL AND parent_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_adk_run_refusals_organization", table_name="adk_run_refusals")
    op.drop_table("adk_run_refusals")
    op.drop_index("ix_adk_runs_on_behalf_of", table_name="adk_runs")
    op.drop_index("ix_adk_runs_trigger", table_name="adk_runs")
    op.drop_column("adk_runs", "root_run_id")
    for column in reversed(provenance_columns()):
        op.drop_column("adk_runs", column.name)
