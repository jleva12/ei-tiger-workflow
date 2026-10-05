"""
What an organization's workflows, agents and assistant used, kept in the admin
MySQL beside their runs: what its overview reads.

- ``usage_calls``: every model and tool call (``forge_common.adk.usage``'s
  plugin hands them over): for which workflow, agent or assistant, by which
  of its ADK agents, on which model, with its tokens and what they cost.
- ``usage_invocations``: every invocation of an agent or the assistant (a
  turn of a conversation, from the hosted API, a test in the builder or a
  workflow): when it started and finished, and whether it succeeded.
  Workflow runs are counted from the run store (``adk_runs``).

Each row keeps its UTC ``hour`` too, which the overview groups by and then
buckets into the reader's days. The admin API owns the tables: its Alembic
migration ``0009usage`` creates them as :data:`metadata` describes them. Times
are UTC: stored naive, read back aware.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from forge_common.adk.usage import Attribution, UsageCall
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
CALL_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
#: The caller went before it finished (a closed stream).
CANCELLED = "cancelled"

metadata = sa.MetaData()

calls = sa.Table(
    "usage_calls",
    metadata,
    sa.Column("id", CALL_ID, primary_key=True, autoincrement=True),
    sa.Column("organization_id", sa.String(64), nullable=False),
    # workflow, agent or assistant.
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("subject_id", sa.String(64), nullable=False),
    sa.Column("subject_name", sa.String(255), nullable=False),
    sa.Column("run_id", sa.String(128), nullable=True),
    sa.Column("session_id", sa.String(128), nullable=True),
    sa.Column("invocation_id", sa.String(128), nullable=True),
    sa.Column("user_id", sa.String(128), nullable=True),
    # The ADK agent that called: a workflow's step, an agent or a sub-agent.
    sa.Column("author", sa.String(255), nullable=False),
    # model or tool.
    sa.Column("call", sa.String(8), nullable=False),
    # The model (provider/model) or the tool.
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("at", DATETIME, nullable=False),
    sa.Column("hour", sa.DateTime(), nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("cached_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
    sa.Column("thinking_tokens", sa.Integer, nullable=False),
    # USD; null when the model has no known price (and for tools).
    sa.Column("cost", sa.Double(), nullable=True),
    sa.Column("failed", sa.Boolean, nullable=False),
    sa.Index("ix_usage_calls_organization", "organization_id", "hour"),
    sa.Index("ix_usage_calls_subject", "organization_id", "kind", "subject_id", "hour"),
)

invocations = sa.Table(
    "usage_invocations",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("organization_id", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("subject_id", sa.String(64), nullable=False),
    sa.Column("subject_name", sa.String(255), nullable=False),
    # Which version ran: 3, draft.
    sa.Column("version", sa.String(32), nullable=True),
    sa.Column("session_id", sa.String(128), nullable=True),
    sa.Column("user_id", sa.String(128), nullable=True),
    # running, succeeded, failed or cancelled.
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error", sa.String(500), nullable=True),
    sa.Column("started_at", DATETIME, nullable=False),
    sa.Column("hour", sa.DateTime(), nullable=False),
    sa.Column("finished_at", DATETIME, nullable=True),
    sa.Column("duration_ms", sa.BigInteger, nullable=True),
    sa.Index("ix_usage_invocations_organization", "organization_id", "hour"),
    sa.Index("ix_usage_invocations_subject", "organization_id", "kind", "subject_id", "hour"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _naive(when: datetime) -> datetime:
    """UTC, without a zone, as the columns keep it. A naive time is UTC already."""
    return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when


def _aware(when: datetime | None) -> datetime | None:
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


def hour_of(when: datetime) -> datetime:
    """The UTC hour ``when`` is in, as the ``hour`` columns keep it."""
    return _naive(when).replace(minute=0, second=0, microsecond=0)


def _int(value: Any) -> int:
    return int(value or 0)


def _float(value: Any) -> float:
    return float(value or 0)


class UsageStore:
    """Model and tool calls and agent invocations, over the admin MySQL's engine."""

    def __init__(self, engine: AsyncEngine, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.clock = clock

    # ------------------------------------------------------------------ writing

    async def record(self, call: UsageCall) -> None:
        """Keeps a model or tool call (a ``UsagePlugin``'s sink)."""
        who = call.attribution
        values = {
            "organization_id": who.organization_id,
            "kind": who.kind,
            "subject_id": who.subject_id[:64],
            "subject_name": who.subject_name[:255],
            "run_id": _clip(call.run_id, 128),
            "session_id": _clip(call.session_id, 128),
            "invocation_id": _clip(call.invocation_id, 128),
            "user_id": _clip(call.user_id, 128),
            "author": call.author[:255],
            "call": call.call,
            "name": call.name[:255],
            "at": _naive(call.at),
            "hour": hour_of(call.at),
            "input_tokens": max(0, call.input_tokens),
            "cached_tokens": max(0, call.cached_tokens),
            "output_tokens": max(0, call.output_tokens),
            "thinking_tokens": max(0, call.thinking_tokens),
            "cost": call.cost,
            "failed": call.failed,
        }
        async with self.engine.begin() as conn:
            await conn.execute(sa.insert(calls).values(values))

    @contextlib.asynccontextmanager
    async def invocation(
        self,
        attribution: Attribution,
        *,
        version: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[None]:
        """
        An agent's or the assistant's invocation, kept while it runs: it
        succeeds when the block ends, fails when it raises, and is cancelled
        when its caller goes. Keeping it never fails the invocation.
        """
        invocation_id = uuid.uuid4().hex
        started = self.clock()
        kept = await self._start(invocation_id, attribution, started, version, session_id, user_id)
        status, error = SUCCEEDED, None
        try:
            yield
        except Exception as raised:
            status, error = FAILED, f"{type(raised).__name__}: {raised}"
            raise
        except BaseException:
            # A closed stream (GeneratorExit) or a cancelled task: the caller went.
            status = CANCELLED
            raise
        finally:
            if kept:
                await self._finish(invocation_id, started, status, error)

    async def _start(
        self,
        invocation_id: str,
        who: Attribution,
        started: datetime,
        version: str | None,
        session_id: str | None,
        user_id: str | None,
    ) -> bool:
        values = {
            "id": invocation_id,
            "organization_id": who.organization_id,
            "kind": who.kind,
            "subject_id": who.subject_id[:64],
            "subject_name": who.subject_name[:255],
            "version": _clip(version, 32),
            "session_id": _clip(session_id, 128),
            "user_id": _clip(user_id or who.user_id, 128),
            "status": RUNNING,
            "started_at": _naive(started),
            "hour": hour_of(started),
        }
        try:
            async with self.engine.begin() as conn:
                await conn.execute(sa.insert(invocations).values(values))
        except Exception:
            log.warning("usage: the invocation of %s %s wasn't recorded", who.kind, who.subject_id, exc_info=True)
            return False
        return True

    async def _finish(self, invocation_id: str, started: datetime, status: str, error: str | None) -> None:
        finished = self.clock()
        values = {
            "status": status,
            "error": _clip(error, 500),
            "finished_at": _naive(finished),
            "duration_ms": max(0, int((finished - started).total_seconds() * 1000)),
        }
        try:
            async with self.engine.begin() as conn:
                await conn.execute(sa.update(invocations).where(invocations.c.id == invocation_id).values(values))
        except Exception:
            log.warning("usage: the end of invocation %s wasn't recorded", invocation_id, exc_info=True)

    async def delete_organization(self, organization_id: str) -> int:
        """Forgets everything the organization used. :return: How many rows went."""
        async with self.engine.begin() as conn:
            gone = (await conn.execute(sa.delete(calls).where(calls.c.organization_id == organization_id))).rowcount
            gone += (
                await conn.execute(sa.delete(invocations).where(invocations.c.organization_id == organization_id))
            ).rowcount
        return int(gone or 0)

    # ------------------------------------------------------------------ reading

    async def model_hours(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """
        :return: The organization's model calls from ``since`` (an hour) to
            ``until``, summed by hour, workflow or agent, and model: ``hour``,
            ``kind``, ``subject_id``, ``model``, ``calls``, ``failed``,
            ``input``, ``cached``, ``output``, ``thinking``, ``cost`` (of the
            priced ones) and ``unpriced``.
        """
        c = calls.c
        query = (
            sa.select(
                c.hour,
                c.kind,
                c.subject_id,
                c.name.label("model"),
                sa.func.count().label("calls"),
                sa.func.sum(sa.case((c.failed, 1), else_=0)).label("failed"),
                sa.func.sum(c.input_tokens).label("input"),
                sa.func.sum(c.cached_tokens).label("cached"),
                sa.func.sum(c.output_tokens).label("output"),
                sa.func.sum(c.thinking_tokens).label("thinking"),
                sa.func.sum(c.cost).label("cost"),
                sa.func.sum(sa.case((c.cost.is_(None), 1), else_=0)).label("unpriced"),
            )
            .where(*self._window(calls, organization_id, since, until), c.call == "model")
            .group_by(c.hour, c.kind, c.subject_id, c.name)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "hour": _aware(row["hour"]),
                "kind": row["kind"],
                "subject_id": row["subject_id"],
                "model": row["model"],
                "calls": _int(row["calls"]),
                "failed": _int(row["failed"]),
                "input": _int(row["input"]),
                "cached": _int(row["cached"]),
                "output": _int(row["output"]),
                "thinking": _int(row["thinking"]),
                "cost": _float(row["cost"]),
                # A failed call before any reply costs nothing, priced or not.
                "unpriced": _int(row["unpriced"]),
            }
            for row in rows
        ]

    async def invocation_hours(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """
        :return: Invocations that started from ``since`` to ``until``, counted
            by hour, agent and status, with their summed ``duration_ms`` and
            how many had one (``timed``).
        """
        i = invocations.c
        query = (
            sa.select(
                i.hour,
                i.kind,
                i.subject_id,
                i.status,
                sa.func.count().label("count"),
                sa.func.sum(i.duration_ms).label("duration_ms"),
                sa.func.count(i.duration_ms).label("timed"),
            )
            .where(*self._window(invocations, organization_id, since, until))
            .group_by(i.hour, i.kind, i.subject_id, i.status)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "hour": _aware(row["hour"]),
                "kind": row["kind"],
                "subject_id": row["subject_id"],
                "status": row["status"],
                "count": _int(row["count"]),
                "duration_ms": _int(row["duration_ms"]),
                "timed": _int(row["timed"]),
            }
            for row in rows
        ]

    async def invocation_failures(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """:return: Failed invocations from ``since`` to ``until``, counted by agent and
        error, with the latest (``last_at``)."""
        i = invocations.c
        query = (
            sa.select(
                i.kind,
                i.subject_id,
                i.error,
                sa.func.count().label("count"),
                sa.func.max(i.started_at).label("last_at"),
            )
            .where(*self._window(invocations, organization_id, since, until), i.status == FAILED)
            .group_by(i.kind, i.subject_id, i.error)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "kind": row["kind"],
                "subject_id": row["subject_id"],
                "error": row["error"],
                "count": _int(row["count"]),
                "last_at": _aware(row["last_at"]),
            }
            for row in rows
        ]

    async def authors(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """:return: Model calls from ``since`` to ``until`` by workflow or agent and the ADK agent that made them."""
        c = calls.c
        query = (
            sa.select(
                c.kind,
                c.subject_id,
                c.author,
                sa.func.count().label("calls"),
                sa.func.sum(c.input_tokens + c.output_tokens).label("tokens"),
                sa.func.sum(c.cost).label("cost"),
            )
            .where(*self._window(calls, organization_id, since, until), c.call == "model")
            .group_by(c.kind, c.subject_id, c.author)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {**row, "calls": _int(row["calls"]), "tokens": _int(row["tokens"]), "cost": _float(row["cost"])}
            for row in map(dict, rows)
        ]

    async def tools(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """:return: Tool calls from ``since`` to ``until`` by workflow or agent and tool."""
        c = calls.c
        query = (
            sa.select(
                c.kind,
                c.subject_id,
                c.name.label("tool"),
                sa.func.count().label("calls"),
                sa.func.sum(sa.case((c.failed, 1), else_=0)).label("failed"),
            )
            .where(*self._window(calls, organization_id, since, until), c.call == "tool")
            .group_by(c.kind, c.subject_id, c.name)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [{**row, "calls": _int(row["calls"]), "failed": _int(row["failed"])} for row in map(dict, rows)]

    async def people(self, organization_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """
        :return: From ``since`` to ``until``, by workflow or agent and user:
            model calls, their tokens and cost, and invocations (``runs``).
        """
        c, i = calls.c, invocations.c
        used = (
            sa.select(
                c.kind,
                c.subject_id,
                c.user_id,
                sa.func.count().label("calls"),
                sa.func.sum(c.input_tokens + c.output_tokens).label("tokens"),
                sa.func.sum(c.cost).label("cost"),
            )
            .where(*self._window(calls, organization_id, since, until), c.call == "model")
            .group_by(c.kind, c.subject_id, c.user_id)
        )
        invoked = (
            sa.select(i.kind, i.subject_id, i.user_id, sa.func.count().label("runs"))
            .where(*self._window(invocations, organization_id, since, until))
            .group_by(i.kind, i.subject_id, i.user_id)
        )
        found: dict[tuple[str, str, str], dict[str, Any]] = {}

        def entry(kind: str, subject_id: str, user_id: str | None) -> dict[str, Any]:
            key = (kind, subject_id, user_id or "")
            return found.setdefault(
                key,
                {
                    "kind": kind,
                    "subject_id": subject_id,
                    "user_id": user_id or "",
                    "runs": 0,
                    "calls": 0,
                    "tokens": 0,
                    "cost": 0.0,
                },
            )

        async with self.engine.connect() as conn:
            for row in (await conn.execute(used)).mappings():
                person = entry(row["kind"], row["subject_id"], row["user_id"])
                person.update(calls=_int(row["calls"]), tokens=_int(row["tokens"]), cost=_float(row["cost"]))
            for row in (await conn.execute(invoked)).mappings():
                entry(row["kind"], row["subject_id"], row["user_id"])["runs"] = _int(row["runs"])
        return list(found.values())

    async def subjects(self, organization_id: str) -> list[dict[str, Any]]:
        """
        :return: Every workflow, agent and assistant the organization ever
            used something of: ``kind``, ``subject_id``, its latest
            ``subject_name`` and when it last did (``last_at``).
        """
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        async with self.engine.connect() as conn:
            for table, at in ((calls, calls.c.at), (invocations, invocations.c.started_at)):
                query = (
                    sa.select(table.c.kind, table.c.subject_id, table.c.subject_name, sa.func.max(at).label("last_at"))
                    .where(table.c.organization_id == organization_id)
                    .group_by(table.c.kind, table.c.subject_id, table.c.subject_name)
                )
                for row in (await conn.execute(query)).mappings():
                    when = _aware(row["last_at"])
                    key = (row["kind"], row["subject_id"])
                    seen = latest.get(key)
                    if seen is None or (when is not None and when > seen["last_at"]):
                        latest[key] = {
                            "kind": row["kind"],
                            "subject_id": row["subject_id"],
                            "subject_name": row["subject_name"],
                            "last_at": when,
                        }
        return list(latest.values())

    async def first_call(self, organization_id: str) -> datetime | None:
        """:return: When the organization's first call was recorded; None before any."""
        query = sa.select(sa.func.min(calls.c.at)).where(calls.c.organization_id == organization_id)
        async with self.engine.connect() as conn:
            return _aware((await conn.execute(query)).scalar_one_or_none())

    @staticmethod
    def _window(table: sa.Table, organization_id: str, since: datetime, until: datetime) -> list[Any]:
        return [
            table.c.organization_id == organization_id,
            table.c.hour >= hour_of(since),
            table.c.hour <= hour_of(until),
        ]


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if value is not None else None
