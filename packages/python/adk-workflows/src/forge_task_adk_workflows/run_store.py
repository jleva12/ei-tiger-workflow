"""
ADK workflow runs, kept in the admin MySQL beside their ADK sessions: what the
admin API starts, lists, decides and acts on, and what the worker runs.

A run's ``status``:

- ``queued``: waiting for a worker. New, decided, retried, recovered, or a
  wait that's over. Queued again after a hiccup, it isn't taken before
  ``next_attempt_at`` (its backoff), whatever job comes for it first.
- ``running``: a worker has it, as ``lease_owner`` until ``lease_until``
  (renewed while it runs), its ``lease_epoch``-th taking of it. A run whose
  lease ran out was interrupted: the worker's maintenance queues it again.
  Abandoned while it runs, it's marked (``cancel_requested_at``): its worker
  stops it at its next step, or as it renews its lease, and abandons it; the
  maintenance does, should the worker go first.
- ``paused``: waiting for people, at the approvals and questions in
  ``gates`` (``pause`` is the first of them). A decision at any of them (or
  its deadline) queues it again; the others stay open.
- ``waiting``: waiting for a time (a delay), until ``waiting_until``; then a
  worker takes it again. It may wait at gates too meanwhile: a decision at
  one queues it at once.
- ``succeeded``, ``failed``, ``abandoned``: finished. A failed run can be
  retried (it carries on from where it was, as its next ``attempt``), unless
  another run of its ADK session is still going: two runs waiting at the same
  step of one session would take each other's answers (runs resubmitted before
  resubmissions had sessions of their own share their original's). A finished
  one can be resubmitted: a new run of the same payload, in a session of its
  own.

A run can start runs of its own (a workflow its LLM agent calls as a tool):
each names it (``parent_run_id``), under an ID its call makes for it, so a
call answered again (its worker stopped) finds the run it started rather than
starting another (``create_child``). Abandoning a run abandons the runs it
started, and theirs, that are still going; whatever else becomes of it, they go on.

A run's open gates (``gates``) are the approvals and questions it waits at,
each decided on its own (``decide``, by its ``id``), whatever the run is doing
meanwhile: paused, waiting, or carrying on with a decision made at another.
A decision made while a worker has the run is read by its next attempt: the
pause it ends at, finding it, queues the run again instead. A run from before
gates has none: its ``pause`` is its only one.

A run's decisions (``decisions``, by the gate's key) are what the worker's
control answers ``approval`` with; ``state`` is what the run keeps
(``control.keep``), made durable at each checkpoint. Every write a worker makes
while it has a run is fenced on its lease, owned and not run out: a worker
whose lease ran out (whether or not the run went to another yet) can't write
to the run, nor renew its lease, as any other worker may take it by then.
Its notes are too (``note`` with its ``owner``), and its writes elsewhere
(its usage rows, its ADK session's events) land while it holds the run
(``hold``, ``holding``): the run's row locked, so nobody takes it meanwhile.

The admin API owns the tables: its Alembic migration ``0005adk_run_store``
creates them as :data:`metadata` describes them. Times are UTC: stored naive,
read back aware.
"""

from __future__ import annotations

import contextlib
import copy
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
WAITING = "waiting"
SUCCEEDED = "succeeded"
FAILED = "failed"
ABANDONED = "abandoned"
STATUSES = (QUEUED, RUNNING, PAUSED, WAITING, SUCCEEDED, FAILED, ABANDONED)
#: Still going: queued, running, or waiting for a person or a time.
OPEN = frozenset({QUEUED, RUNNING, PAUSED, WAITING})
FINISHED = frozenset({SUCCEEDED, FAILED, ABANDONED})
#: What can be abandoned at once: anything not running and not finished, and a failed run.
ABANDONABLE = frozenset({QUEUED, PAUSED, WAITING, FAILED})
#: What can be abandoned at all: a running run too, which its worker stops.
CANCELABLE = ABANDONABLE | {RUNNING}

#: Why a run failed, or why it was queued again: ``failed`` (the run said so: a
#: failed step, input that doesn't fit, a declined question), ``error`` (a bug
#: in the worker or the task), ``transient`` (a hiccup it was retried after),
#: ``interrupted`` (its worker went while it ran).
FAILURE_CATEGORIES = ("failed", "error", "transient", "interrupted")

DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
EVENT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

metadata = sa.MetaData()

runs = sa.Table(
    "adk_runs",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("organization_id", sa.String(64), nullable=False),
    sa.Column("agent_id", sa.String(64), nullable=False),
    sa.Column("agent_name", sa.String(255), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    # Which version of the workflow ran: "draft", or a published version's
    # number; null for runs from before workflows had versions
    # (forge-admin's 0012adk_run_versions).
    sa.Column("version", sa.String(16), nullable=True),
    sa.Column("session_id", sa.String(128), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    # The run as the worker runs it (forge_task_adk_workflows.runs.RunPayload).
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("state", sa.JSON, nullable=True),
    sa.Column("decisions", sa.JSON, nullable=True),
    sa.Column("pause", sa.JSON, nullable=True),
    # Every approval and question it waits at, each decided on its own
    # (forge-admin's 0026adk_run_gates): pause is the first, while it's paused.
    sa.Column("gates", sa.JSON, nullable=True),
    sa.Column("waiting_until", DATETIME, nullable=True),
    sa.Column("waiting_reason", sa.String(500), nullable=True),
    sa.Column("result", sa.JSON, nullable=True),
    sa.Column("error", sa.JSON, nullable=True),
    sa.Column("requested_by", sa.String(64), nullable=False),
    sa.Column("requested_by_name", sa.String(255), nullable=False),
    sa.Column("resubmit_of", sa.String(32), nullable=True),
    # The run that started it (a workflow its LLM agent called as a tool;
    # forge-admin's 0021adk_run_children).
    sa.Column("parent_run_id", sa.String(32), nullable=True),
    sa.Column("lease_owner", sa.String(128), nullable=True),
    sa.Column("lease_until", DATETIME, nullable=True),
    # One more each time a worker takes the run: which attempt holds it, for
    # the keys that make an attempt's effects happen once (forge-admin's
    # 0020adk_run_control, as the column below).
    sa.Column("lease_epoch", sa.Integer, nullable=False, server_default="0"),
    # When someone abandoned the run while it ran: its worker stops it, and
    # abandons it.
    sa.Column("cancel_requested_at", DATETIME, nullable=True),
    # When a run queued again after a hiccup may be taken: not before (its
    # backoff), whatever job comes for it (forge-admin's 0022adk_run_backoff).
    sa.Column("next_attempt_at", DATETIME, nullable=True),
    sa.Column("created_at", DATETIME, nullable=False),
    sa.Column("updated_at", DATETIME, nullable=False),
    sa.Column("started_at", DATETIME, nullable=True),
    sa.Column("finished_at", DATETIME, nullable=True),
    sa.Index("ix_adk_runs_organization", "organization_id", "created_at"),
    sa.Index("ix_adk_runs_agent", "organization_id", "agent_id", "created_at"),
    sa.Index("ix_adk_runs_status", "status", "updated_at"),
    sa.Index("ix_adk_runs_parent", "parent_run_id", "created_at"),
)

events = sa.Table(
    "adk_run_events",
    metadata,
    sa.Column("id", EVENT_ID, primary_key=True, autoincrement=True),
    sa.Column("run_id", sa.String(32), sa.ForeignKey("adk_runs.id", ondelete="CASCADE"), nullable=False),
    sa.Column("at", DATETIME, nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("message", sa.String(1000), nullable=False),
    sa.Column("actor_id", sa.String(64), nullable=True),
    sa.Column("actor_name", sa.String(255), nullable=True),
    sa.Column("attributes", sa.JSON, nullable=True),
    sa.Index("ix_adk_run_events_run", "run_id", "id"),
)

#: What a list of runs reads: everything but what only the run's own page shows.
SUMMARY_COLUMNS = [c for c in runs.c if c.name not in ("payload", "state", "decisions", "result")]


class RunStoreError(Exception):
    """A run can't do what was asked of it."""


class NoSuchRun(RunStoreError):
    """There's no such run (or not in that organization)."""


class NotAllowed(RunStoreError):
    """The run's status doesn't allow it (it's running, finished, or not paused)."""


class NotOpen(NotAllowed):
    """The approval or question named isn't one the run waits at."""


class LostLease(RunStoreError):
    """The worker no longer has the run: its lease ran out and it moved on."""


class CancelRequested(RunStoreError):
    """Someone abandoned the running run: its worker stops it, and abandons
    it, instead of anything that would carry it on."""


@dataclass(frozen=True)
class Renewal:
    """What renewing a run's lease found: whether the worker still has it
    (until ``until``, on the run store's clock), and whether someone
    abandoned the run meanwhile. True when it's kept."""

    kept: bool
    stopping: bool = False
    until: datetime | None = None

    def __bool__(self) -> bool:
        return self.kept


@dataclass(frozen=True)
class Actor:
    """Who did something to a run: a member, or nobody (the worker, the timer)."""

    id: str | None = None
    name: str = ""


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def new_session_id() -> str:
    """:return: A new ADK session's ID, as the admin API makes them for runs."""
    return str(uuid.uuid4())


def _naive(when: datetime) -> datetime:
    """UTC, without a zone, as the columns keep it. A naive time is UTC already."""
    return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when


def _aware(when: datetime | None) -> datetime | None:
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


_TIMES = (
    "waiting_until",
    "lease_until",
    "cancel_requested_at",
    "next_attempt_at",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)


def _row(mapping: Any) -> dict[str, Any]:
    row = dict(mapping)
    for name in _TIMES:
        if name in row:
            row[name] = _aware(row[name])
    return row


def _json(value: Any) -> Any:
    """A deep copy that's plain JSON: what a JSON column takes."""
    return copy.deepcopy(value)


class RunStore:
    """ADK workflow runs and their activity, over the admin MySQL's engine."""

    def __init__(self, engine: AsyncEngine, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.clock = clock

    def _now(self) -> datetime:
        return _naive(self.clock())

    # ------------------------------------------------------------------ reading

    async def get(self, run_id: str, *, organization_id: str | None = None) -> dict[str, Any] | None:
        """:return: The run, all of it; None when there's none (in that organization)."""
        query = sa.select(runs).where(runs.c.id == run_id)
        if organization_id is not None:
            query = query.where(runs.c.organization_id == organization_id)
        async with self.engine.connect() as conn:
            found = (await conn.execute(query)).mappings().first()
        return _row(found) if found is not None else None

    async def page(
        self,
        organization_id: str,
        *,
        agent_id: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """:return: The organization's runs (of one ADK workflow, in some
        statuses), newest first, without their payloads; and how many match."""
        where = [runs.c.organization_id == organization_id]
        if agent_id is not None:
            where.append(runs.c.agent_id == agent_id)
        if statuses:
            where.append(runs.c.status.in_(list(statuses)))
        query = (
            sa.select(*SUMMARY_COLUMNS)
            .where(*where)
            .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        count = sa.select(sa.func.count()).select_from(runs).where(*where)
        async with self.engine.connect() as conn:
            items = [_row(r) for r in (await conn.execute(query)).mappings()]
            total = int((await conn.execute(count)).scalar_one())
        return items, total

    async def started_since(
        self, organization_id: str, since: datetime, *, limit: int = 20_000
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        :return: The organization's runs created since ``since``, newest
            first, as an overview counts them (``id``, ``agent_id``,
            ``agent_name``, ``status``, ``attempt``, ``requested_by``,
            ``requested_by_name``, ``error``, and their times); and whether
            there were more than ``limit`` (the oldest left out).
        """
        query = (
            sa.select(
                runs.c.id,
                runs.c.agent_id,
                runs.c.agent_name,
                runs.c.status,
                runs.c.attempt,
                runs.c.requested_by,
                runs.c.requested_by_name,
                runs.c.error,
                runs.c.created_at,
                runs.c.started_at,
                runs.c.finished_at,
            )
            .where(runs.c.organization_id == organization_id, runs.c.created_at >= _naive(since))
            .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            .limit(limit + 1)
        )
        async with self.engine.connect() as conn:
            found = [_row(r) for r in (await conn.execute(query)).mappings()]
        return found[:limit], len(found) > limit

    async def open_counts(self, organization_id: str) -> dict[str, int]:
        """:return: How many of the organization's runs are in each status that isn't finished."""
        query = (
            sa.select(runs.c.status, sa.func.count().label("count"))
            .where(runs.c.organization_id == organization_id, runs.c.status.in_(list(OPEN)))
            .group_by(runs.c.status)
        )
        async with self.engine.connect() as conn:
            found = {row["status"]: int(row["count"]) for row in (await conn.execute(query)).mappings()}
        return {status: found.get(status, 0) for status in (QUEUED, RUNNING, PAUSED, WAITING)}

    async def latest_by_agent(self, organization_id: str) -> list[dict[str, Any]]:
        """:return: For each ADK workflow the organization ever ran: ``agent_id``, its
        latest ``agent_name`` and when it was last run (``last_at``)."""
        query = (
            sa.select(runs.c.agent_id, runs.c.agent_name, sa.func.max(runs.c.created_at).label("last_at"))
            .where(runs.c.organization_id == organization_id)
            .group_by(runs.c.agent_id, runs.c.agent_name)
        )
        latest: dict[str, dict[str, Any]] = {}
        async with self.engine.connect() as conn:
            for row in (await conn.execute(query)).mappings():
                when = _aware(row["last_at"])
                seen = latest.get(row["agent_id"])
                if seen is None or (when is not None and when > seen["last_at"]):
                    latest[row["agent_id"]] = {
                        "agent_id": row["agent_id"],
                        "agent_name": row["agent_name"],
                        "last_at": when,
                    }
        return list(latest.values())

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        """:return: The run's activity, oldest first."""
        query = sa.select(events).where(events.c.run_id == run_id).order_by(events.c.id)
        async with self.engine.connect() as conn:
            found = [dict(r) for r in (await conn.execute(query)).mappings()]
        for event in found:
            event["at"] = _aware(event["at"])
        return found

    # ------------------------------------------------------------------ the admin API

    async def create(
        self,
        *,
        organization_id: str,
        agent_id: str,
        agent_name: str,
        revision: int,
        session_id: str,
        payload: dict[str, Any],
        requested_by: Actor,
        resubmit_of: str | None = None,
        run_id: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """
        :param version: Which version of the workflow it runs: "draft" or a
            number; None when unknown.
        :return: A new run, queued: the caller queues its job.
        """
        async with self.engine.begin() as conn:
            run_id = await self._insert(
                conn,
                organization_id=organization_id,
                agent_id=agent_id,
                agent_name=agent_name,
                revision=revision,
                session_id=session_id,
                payload=payload,
                requested_by=requested_by,
                resubmit_of=resubmit_of,
                run_id=run_id,
                version=version,
            )
        return await self._must(run_id)

    async def create_child(
        self,
        *,
        run_id: str,
        parent_run_id: str,
        organization_id: str,
        agent_id: str,
        agent_name: str,
        revision: int,
        session_id: str,
        payload: dict[str, Any],
        requested_by: Actor,
        version: str | None = None,
        owner: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """
        A run another starts (a workflow its LLM agent calls as a tool), under
        the ID its call makes for it: asked again for that ID, as when the call
        is answered after its worker stopped, it's the run that call started.

        :param owner: The parent's worker: a new run is started only while it
            has the parent (fenced on its lease, as its writes are: owned, and
            not run out).
        :return: The run, and whether it's new (queued: the caller queues its job).
        :raises NotAllowed: The parent run isn't running (the owner's), or
            someone abandoned it; or the ID is another run's.
        """
        async with self.engine.begin() as conn:
            parent = (
                await conn.execute(
                    sa.select(runs.c.status, runs.c.cancel_requested_at, runs.c.lease_owner, runs.c.lease_until)
                    .where(runs.c.id == parent_run_id)
                    .with_for_update()
                )
            ).first()
            existing = (
                await conn.execute(sa.select(runs.c.organization_id, runs.c.parent_run_id).where(runs.c.id == run_id))
            ).first()
            if existing is not None:
                if (existing.organization_id, existing.parent_run_id) != (organization_id, parent_run_id):
                    raise NotAllowed(f"Run {run_id[:8]} isn't one run {parent_run_id[:8]} started")
                created = False
            else:
                if (
                    parent is None
                    or parent.status != RUNNING
                    or parent.cancel_requested_at is not None
                    or (owner is not None and (parent.lease_owner != owner or not self._held(parent.lease_until)))
                ):
                    raise NotAllowed("A run that isn't running, or is being abandoned, can't start runs")
                await self._insert(
                    conn,
                    organization_id=organization_id,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    revision=revision,
                    session_id=session_id,
                    payload=payload,
                    requested_by=requested_by,
                    resubmit_of=None,
                    run_id=run_id,
                    version=version,
                    parent_run_id=parent_run_id,
                )
                created = True
        return await self._must(run_id), created

    async def children(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """:return: The runs the run started (a list's summary), oldest first."""
        query = (
            sa.select(*SUMMARY_COLUMNS)
            .where(runs.c.parent_run_id == run_id)
            .order_by(runs.c.created_at, runs.c.id)
            .limit(limit)
        )
        async with self.engine.connect() as conn:
            return [_row(r) for r in (await conn.execute(query)).mappings()]

    async def _insert(
        self,
        conn: AsyncConnection,
        *,
        organization_id: str,
        agent_id: str,
        agent_name: str,
        revision: int,
        session_id: str,
        payload: dict[str, Any],
        requested_by: Actor,
        resubmit_of: str | None,
        run_id: str | None,
        version: str | None,
        parent_run_id: str | None = None,
    ) -> str:
        """A new run, queued, with its first activity. :return: Its ID."""
        now = self._now()
        run_id = run_id or new_id()
        values: dict[str, Any] = {
            "id": run_id,
            "organization_id": organization_id,
            "agent_id": agent_id,
            "agent_name": agent_name[:255],
            "revision": revision,
            "session_id": session_id,
            "status": QUEUED,
            "attempt": 1,
            "payload": _json(payload),
            "decisions": {},
            "requested_by": requested_by.id or "",
            "requested_by_name": requested_by.name[:255],
            "resubmit_of": resubmit_of,
            "created_at": now,
            "updated_at": now,
        }
        # Only when known, so a store older than the column still takes runs.
        if version is not None:
            values["version"] = version[:16]
        if parent_run_id is not None:
            values["parent_run_id"] = parent_run_id
        if resubmit_of:
            message = f"Resubmitted from run {resubmit_of[:8]}"
        elif parent_run_id:
            message = f"Started by run {parent_run_id[:8]}"
        else:
            message = "Started"
        await conn.execute(sa.insert(runs).values(values))
        await self._event(conn, run_id, "created", message, actor=requested_by, at=now)
        return run_id

    async def decide(
        self,
        run_id: str,
        *,
        request_id: str,
        approved: bool,
        comment: str,
        actor: Actor,
        organization_id: str | None = None,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        """
        Decide (or answer) an approval or question the run waits at (one of
        its gates); the others stay open. Paused or waiting, the run is
        queued to carry on with it; queued or running, its next attempt reads
        it. ``timed_out``: nobody did by its deadline (the worker's rejection).

        :raises NoSuchRun: There's no such run.
        :raises NotOpen: It doesn't wait at ``request_id`` (decided already, or moved on).
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            gates = open_gates(run)
            pause = next((gate for gate in gates if gate.get("id") == request_id), None)
            if pause is None:
                raise NotOpen("The run doesn't wait at that approval or question any more")
            now = self._now()
            decisions = dict(run["decisions"] or {})
            decisions[pause["key"]] = {
                "approved": approved,
                "comment": comment,
                "actor_id": actor.id,
                "actor_name": actor.name,
                "request_id": request_id,
                "decided_at": now.replace(tzinfo=UTC).isoformat(),
            }
            values: dict[str, Any] = {
                "decisions": decisions,
                "gates": [gate for gate in gates if gate is not pause] or None,
                "updated_at": now,
            }
            if run["status"] in (PAUSED, WAITING):
                values.update(status=QUEUED, pause=None, waiting_until=None, waiting_reason=None)
            await self._update(conn, run_id, values, expect=(run["status"],))
            if timed_out:
                kind, message = "timed_out", "Nobody decided in time"
            elif pause.get("kind") == "human_input":
                kind, message = ("answered", "Answered") if approved else ("declined", "Declined to answer")
            else:
                kind, message = ("decided", "Approved") if approved else ("decided", "Rejected")
            what = (pause.get("details") or {}).get("step_name") or ""
            await self._event(
                conn,
                run_id,
                kind,
                f"{message}{f': {what}' if what else ''}",
                actor=actor,
                at=now,
                attributes={"request_id": request_id, "approved": approved, "comment": comment[:500]},
            )
        return await self._must(run_id)

    async def retry(self, run_id: str, *, actor: Actor, organization_id: str | None = None) -> dict[str, Any]:
        """
        A failed run, queued again as its next attempt: it carries on from
        what it kept.

        :raises NotAllowed: It isn't failed, or another run of its session is
            still going.
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            if run["status"] != FAILED:
                raise NotAllowed("Only a failed run can be retried")
            await self._alone_in_session(conn, run)
            now = self._now()
            attempt = int(run["attempt"]) + 1
            await self._update(
                conn,
                run_id,
                # An earlier attempt's abandonment, which it finished before, no longer stands.
                {
                    "status": QUEUED,
                    "attempt": attempt,
                    "finished_at": None,
                    "cancel_requested_at": None,
                    "updated_at": now,
                },
                expect=(FAILED,),
            )
            await self._event(conn, run_id, "retried", f"Retried: attempt {attempt}", actor=actor, at=now)
        return await self._must(run_id)

    async def resubmit(self, run_id: str, *, actor: Actor, organization_id: str | None = None) -> dict[str, Any]:
        """
        :return: A new run of the finished run's payload, queued, in an ADK
            session of its own; its files stay in the session they were
            saved in (``files_session_id``), and it copies them to its own.
        :raises NotAllowed: The run isn't finished.
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            if run["status"] not in FINISHED:
                raise NotAllowed("Only a finished run can be resubmitted")
            payload = dict(run["payload"])
            if payload.get("files") and not payload.get("files_session_id"):
                payload["files_session_id"] = payload.get("session_id") or run["session_id"]
            payload["session_id"] = session_id = new_session_id()
            again = await self._insert(
                conn,
                organization_id=run["organization_id"],
                agent_id=run["agent_id"],
                agent_name=run["agent_name"],
                revision=run["revision"],
                session_id=session_id,
                payload=payload,
                requested_by=actor,
                resubmit_of=run_id,
                run_id=None,
                version=run.get("version"),
            )
        return await self._must(again)

    async def abandon(self, run_id: str, *, actor: Actor, organization_id: str | None = None) -> dict[str, Any]:
        """
        Give a run up: it never carries on. Queued, paused, waiting or failed,
        it's abandoned at once. Running, it's marked (``cancel_requested_at``):
        its worker stops it and abandons it (asking again changes nothing).
        The runs it started that are still going, and theirs, are abandoned
        with it.

        :raises NotAllowed: It's finished, and didn't fail.
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            now = self._now()
            if run["status"] == RUNNING:
                if run["cancel_requested_at"] is None:
                    await self._update(conn, run_id, {"cancel_requested_at": now, "updated_at": now}, expect=(RUNNING,))
                    await self._event(
                        conn,
                        run_id,
                        "cancel_requested",
                        "Abandoned while it ran: its worker stops it",
                        actor=actor,
                        at=now,
                    )
            else:
                await self._abandon_now(conn, run, actor=actor, at=now)
            await self._abandon_descendants(conn, run_id, actor=actor, at=now)
        return await self._must(run_id)

    async def _abandon_now(self, conn: AsyncConnection, run: dict[str, Any], *, actor: Actor, at: datetime) -> None:
        """A run that isn't running, abandoned. :raises NotAllowed: It's finished, and didn't fail."""
        run_id, now = run["id"], at
        if run["status"] not in ABANDONABLE:
            raise NotAllowed("A run that succeeded or was abandoned can't be abandoned")
        await self._update(
            conn,
            run_id,
            {
                "status": ABANDONED,
                "pause": None,
                "gates": None,
                "waiting_until": None,
                "waiting_reason": None,
                "finished_at": now,
                "updated_at": now,
            },
            expect=tuple(ABANDONABLE),
        )
        await self._event(conn, run_id, "abandoned", "Abandoned", actor=actor, at=now)

    async def _abandon_descendants(
        self, conn: AsyncConnection, run_id: str, *, actor: Actor | None, at: datetime
    ) -> int:
        """
        The runs the run started, and theirs, that are still going, abandoned
        with it: at once, or (running) stopped by their worker first. A run
        that ended stays as it is, but what it started is abandoned too.

        :return: How many were.
        """
        abandoned, seen, parents = 0, {run_id}, [run_id]
        while parents:
            found = (
                await conn.execute(
                    sa.select(runs.c.id, runs.c.status, runs.c.cancel_requested_at)
                    .where(runs.c.parent_run_id.in_(parents))
                    .with_for_update()
                )
            ).all()
            parents = []
            for child in found:
                if child.id in seen:
                    continue
                seen.add(child.id)
                parents.append(child.id)
                if child.status == RUNNING and child.cancel_requested_at is None:
                    await self._update(conn, child.id, {"cancel_requested_at": at, "updated_at": at}, expect=(RUNNING,))
                    message = "The run that started it was abandoned: its worker stops it"
                    await self._event(conn, child.id, "cancel_requested", message, actor=actor, at=at)
                    abandoned += 1
                elif child.status in (QUEUED, PAUSED, WAITING):
                    values = {
                        "status": ABANDONED,
                        "pause": None,
                        "gates": None,
                        "waiting_until": None,
                        "waiting_reason": None,
                        "finished_at": at,
                        "updated_at": at,
                    }
                    await self._update(conn, child.id, values, expect=(QUEUED, PAUSED, WAITING))
                    message = "Abandoned: the run that started it was abandoned"
                    await self._event(conn, child.id, ABANDONED, message, actor=actor, at=at)
                    abandoned += 1
        if abandoned:
            message = f"The runs it started that were still going are abandoned too ({abandoned})"
            await self._event(conn, run_id, "note", message, actor=actor, at=at)
        return abandoned

    async def delete_organization(self, organization_id: str) -> int:
        """Forget an organization's runs (their activity goes with them). :return: How many."""
        async with self.engine.begin() as conn:
            ids = sa.select(runs.c.id).where(runs.c.organization_id == organization_id)
            await conn.execute(sa.delete(events).where(events.c.run_id.in_(ids)))
            result = await conn.execute(sa.delete(runs).where(runs.c.organization_id == organization_id))
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ the worker

    async def claim(self, run_id: str, *, owner: str, lease_seconds: float) -> dict[str, Any] | None:
        """
        Take a queued run (or a waiting one whose time has come) to run it.

        :return: The run, now ``running`` and the owner's until its lease runs
            out, its ``lease_epoch`` one more; None when it isn't to be run now
            (another worker has it, it's paused, finished or being abandoned,
            its wait isn't over, or it waits out a hiccup's backoff).
        """
        now = self._now()
        due = sa.or_(runs.c.next_attempt_at.is_(None), runs.c.next_attempt_at <= now)
        runnable = sa.and_(
            sa.or_(
                sa.and_(runs.c.status == QUEUED, due),
                sa.and_(runs.c.status == WAITING, runs.c.waiting_until <= now),
            ),
            runs.c.cancel_requested_at.is_(None),
        )
        async with self.engine.begin() as conn:
            before = (
                await conn.execute(
                    sa.select(runs.c.status, runs.c.started_at, runs.c.lease_epoch).where(runs.c.id == run_id)
                )
            ).first()
            result = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runnable)
                .values(
                    status=RUNNING,
                    lease_owner=owner,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    lease_epoch=runs.c.lease_epoch + 1,
                    started_at=sa.func.coalesce(runs.c.started_at, now),
                    waiting_until=None,
                    waiting_reason=None,
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1 or before is None:
                return None
            if before.started_at is None:
                kind, message = "started", "A worker started it"
            elif before.status == WAITING:
                kind, message = "resumed", "Its wait is over: a worker carries it on"
            else:
                kind, message = "resumed", "A worker carries it on"
            attributes = {"worker": owner, "epoch": int(before.lease_epoch or 0) + 1}
            await self._event(conn, run_id, kind, message, at=now, attributes=attributes)
        return await self._must(run_id)

    async def renew(self, run_id: str, *, owner: str, lease_seconds: float) -> Renewal:
        """
        Keep the run the owner's for longer.

        :return: Whether it still has it, until when; false when it lost it,
            its lease ran out (any worker may take it by then, so it isn't
            renewed), or the run moved on. And whether someone abandoned it
            meanwhile.
        """
        now = self._now()
        until = now + timedelta(seconds=lease_seconds)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(runs).where(runs.c.id == run_id, *self._lease(owner, now)).values(lease_until=until)
            )
            if result.rowcount != 1:
                return Renewal(kept=False)
            asked = (
                await conn.execute(sa.select(runs.c.cancel_requested_at).where(runs.c.id == run_id))
            ).scalar_one_or_none()
        return Renewal(kept=True, stopping=asked is not None, until=_aware(until))

    async def checkpoint(self, run_id: str, *, owner: str, state: dict[str, Any]) -> None:
        """
        Make what the run keeps durable.

        :raises LostLease: The owner lost the run.
        :raises CancelRequested: Someone abandoned the run: it stops here.
        """
        async with self.engine.begin() as conn:
            await self._fenced(conn, run_id, owner, {"state": _json(state), "updated_at": self._now()}, going=True)

    async def note(
        self,
        run_id: str,
        message: str,
        *,
        kind: str = "note",
        actor: Actor | None = None,
        attributes: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> None:
        """
        A line on the run's activity.

        :param owner: The worker noting it as it runs the run: it's noted only
            while the run is its (fenced on its lease, as its writes are).
        :raises LostLease: The owner lost the run, or its lease ran out.
        """
        async with self.engine.begin() as conn:
            if owner is not None:
                await self.hold(conn, run_id, owner=owner)
            await self._event(conn, run_id, kind, message, actor=actor, at=self._now(), attributes=attributes)

    async def hold(self, conn: AsyncConnection, run_id: str, *, owner: str) -> None:
        """
        The owner's lease on the run, held for the rest of ``conn``'s
        transaction: it's the owner's and hasn't run out, and the run's row is
        locked against writes until the transaction ends, so no worker takes
        the run (nor does the maintenance queue it again) meanwhile. A write
        of the owner's in that transaction lands while it has the run, or not
        at all; a write elsewhere made before it ends (``holding``) lands
        before anyone else can have the run.

        :raises LostLease: The owner lost the run, or its lease ran out.
        """
        held = await conn.execute(
            sa.select(runs.c.id).where(runs.c.id == run_id, *self._lease(owner, self._now())).with_for_update(read=True)
        )
        if held.first() is None:
            raise LostLease(f"run {run_id} is no longer {owner}'s, or its lease ran out")

    @contextlib.asynccontextmanager
    async def holding(self, run_id: str, *, owner: str) -> AsyncIterator[None]:
        """
        While inside, the owner has the run (``hold``, on a connection of its
        own): for a write elsewhere (the run's ADK session) that mustn't land
        once another worker may have the run. Writes to the run, the owner's
        own among them, wait until the block ends: keep it short, and never
        write to the run inside it.

        :raises LostLease: The owner lost the run, or its lease ran out.
        """
        async with self.engine.begin() as conn:
            await self.hold(conn, run_id, owner=owner)
            yield

    async def pause(
        self,
        run_id: str,
        *,
        owner: str,
        state: dict[str, Any],
        gates: Sequence[Mapping[str, Any]],
        until: datetime | None = None,
        reason: str = "",
    ) -> list[dict[str, Any]] | None:
        """
        The run waits for people, at each of ``gates`` (``{key, kind, reason,
        details, deadline}``), each decided on its own; with ``until``, for a
        time too (a delay: it's ``waiting``, ``reason`` saying why). Its
        worker lets it go. A gate still open keeps its ``id``, so a decision
        on it made before lands whatever the run did since.

        A gate someone decided while the worker had the run (its attempt read
        the run's decisions before) doesn't wait: the run is queued again
        instead, for its next attempt to carry on with the decision.

        :return: Its open gates (each ``id`` names it in a decision); None
            when it was queued again instead.
        :raises LostLease: The owner lost the run.
        :raises CancelRequested: Someone abandoned the run: it doesn't wait.
        """
        now = self._now()
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, None)
            decided = run["decisions"] or {}
            known = {gate.get("key"): gate for gate in open_gates(run)}
            made = [_gate(gate, known.get(gate["key"]), now) for gate in gates]
            fresh = [gate for gate in made if gate["key"] not in known]
            values: dict[str, Any] = {
                "state": _json(state),
                "lease_owner": None,
                "lease_until": None,
                "updated_at": now,
            }
            if any(gate["key"] in decided for gate in made):
                open_ = [gate for gate in made if gate["key"] not in decided]
                values.update(status=QUEUED, pause=None, gates=open_ or None, waiting_until=None, waiting_reason=None)
                await self._fenced(conn, run_id, owner, values, going=True)
                await self._event(conn, run_id, "note", "Decided while it ran: it carries on", at=now)
                return None
            if until is not None:
                values.update(status=WAITING, pause=None, waiting_until=_naive(until), waiting_reason=reason[:500])
            else:
                values.update(status=PAUSED, pause=made[0] if made else None)
            values["gates"] = made or None
            await self._fenced(conn, run_id, owner, values, going=True)
            if until is not None:
                await self._event(conn, run_id, "waiting", reason[:1000] or "Waits", at=now)
            for gate in fresh:
                what = "an answer" if gate["kind"] == "human_input" else "a decision"
                await self._event(conn, run_id, "paused", f"Waits for {what}: {gate['reason']}"[:1000], at=now)
            if not fresh and until is None:
                count = f"{len(made)} approvals or questions" if len(made) != 1 else "its approval or question"
                await self._event(conn, run_id, "paused", f"Waits at {count} still open", at=now)
        return made

    async def wait(self, run_id: str, *, owner: str, state: dict[str, Any], until: datetime, reason: str) -> None:
        """
        The run waits for a time: its worker lets it go.

        :raises LostLease: The owner lost the run.
        :raises CancelRequested: Someone abandoned the run: it doesn't wait.
        """
        now = self._now()
        async with self.engine.begin() as conn:
            await self._fenced(
                conn,
                run_id,
                owner,
                {
                    "status": WAITING,
                    "state": _json(state),
                    "gates": None,
                    "waiting_until": _naive(until),
                    "waiting_reason": reason[:500],
                    "lease_owner": None,
                    "lease_until": None,
                    "updated_at": now,
                },
                going=True,
            )
            await self._event(conn, run_id, "waiting", reason[:1000] or "Waits", at=now)

    async def finish(
        self,
        run_id: str,
        *,
        owner: str,
        state: dict[str, Any] | None,
        succeeded: bool,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """The run ended, succeeded or failed. :raises LostLease: The owner lost the run."""
        now = self._now()
        values: dict[str, Any] = {
            "status": SUCCEEDED if succeeded else FAILED,
            "pause": None,
            "gates": None,
            "result": _json(result),
            "error": _failure(error, now) if not succeeded else None,
            "lease_owner": None,
            "lease_until": None,
            # Abandoned as it ended: it ended first, and nothing stops any more.
            "cancel_requested_at": None,
            "finished_at": now,
            "updated_at": now,
        }
        if state is not None:
            values["state"] = _json(state)
        async with self.engine.begin() as conn:
            await self._fenced(conn, run_id, owner, values)
            message = "Succeeded" if succeeded else f"Failed: {(error or {}).get('message') or 'it failed'}"
            await self._event(conn, run_id, SUCCEEDED if succeeded else FAILED, message[:1000], at=now)

    async def requeue(
        self,
        run_id: str,
        *,
        owner: str | None,
        error: dict[str, Any],
        state: dict[str, Any] | None = None,
        not_before: datetime | None = None,
    ) -> dict[str, Any] | None:
        """
        A run that stopped on a hiccup (``owner``'s, which lets it go) or was
        interrupted (``owner`` None: its lease ran out), queued again as its
        next attempt.

        :param not_before: When it may be taken (a hiccup's backoff): no job
            takes it sooner. None: at once.

        :return: The run; None when it isn't the owner's, or its lease ran
            out (or, interrupted, when its lease hasn't run out after all, or
            someone abandoned it: the maintenance abandons it instead).
        :raises CancelRequested: The owner's, and someone abandoned it.
        """
        now = self._now()
        if owner is not None:
            fence = sa.and_(*self._lease(owner, now))
        else:
            fence = sa.and_(runs.c.status == RUNNING, runs.c.lease_until < now)
        fence = sa.and_(fence, runs.c.cancel_requested_at.is_(None))
        values: dict[str, Any] = {
            "status": QUEUED,
            "attempt": runs.c.attempt + 1,
            "error": _failure(error, now),
            "lease_owner": None,
            "lease_until": None,
            "next_attempt_at": _naive(not_before) if not_before is not None else None,
            "updated_at": now,
        }
        if state is not None:
            values["state"] = _json(state)
        async with self.engine.begin() as conn:
            result = await conn.execute(sa.update(runs).where(runs.c.id == run_id, fence).values(values))
            if result.rowcount != 1:
                if owner is not None and await self._stopping(conn, run_id, owner, now):
                    raise CancelRequested(f"run {run_id} was abandoned while it ran")
                return None
            interrupted = owner is None
            kind = "recovered" if interrupted else "retried"
            why = error.get("message") or ("Its worker went while it ran" if interrupted else "A hiccup")
            await self._event(conn, run_id, kind, f"{why}: queued again"[:1000], at=now)
        return await self._must(run_id)

    async def fail_interrupted(self, run_id: str, *, error: dict[str, Any], result: Any = None) -> bool:
        """
        A run whose lease ran out once too often, or whose worker failed it
        but couldn't say so (the failure it kept elsewhere): it fails.

        :return: Whether it did.
        """
        now = self._now()
        async with self.engine.begin() as conn:
            updated = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runs.c.status == RUNNING, runs.c.lease_until < now)
                .values(
                    status=FAILED,
                    pause=None,
                    gates=None,
                    error=_failure(error, now),
                    result=_json(result),
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                return False
            await self._event(conn, run_id, FAILED, f"Failed: {error.get('message') or 'interrupted'}"[:1000], at=now)
        return True

    async def abandon_running(self, run_id: str, *, owner: str, state: dict[str, Any] | None) -> None:
        """
        A run someone abandoned while it ran, stopped by its worker: abandoned.

        :raises LostLease: The owner lost the run.
        """
        now = self._now()
        values: dict[str, Any] = {
            "status": ABANDONED,
            "lease_owner": None,
            "lease_until": None,
            "finished_at": now,
            "updated_at": now,
        }
        if state is not None:
            values["state"] = _json(state)
        async with self.engine.begin() as conn:
            await self._fenced(conn, run_id, owner, values)
            await self._event(conn, run_id, ABANDONED, "Abandoned: its worker stopped it", at=now)
            # And what it started since someone abandoned it, before it stopped.
            await self._abandon_descendants(conn, run_id, actor=None, at=now)

    async def stopping_left(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Runs someone abandoned while they ran that no worker is
        stopping: their worker went (their lease ran out), oldest first."""
        return await self._find(sa.and_(runs.c.cancel_requested_at.is_not(None), self._left()), limit)

    async def abandon_stopping(self, run_id: str) -> bool:
        """A run someone abandoned while it ran, whose worker went before it
        stopped it: abandoned. :return: Whether it was."""
        now = self._now()
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runs.c.cancel_requested_at.is_not(None), self._left())
                .values(
                    status=ABANDONED,
                    pause=None,
                    gates=None,
                    waiting_until=None,
                    waiting_reason=None,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return False
            await self._event(conn, run_id, ABANDONED, "Abandoned: its worker went before it stopped it", at=now)
            await self._abandon_descendants(conn, run_id, actor=None, at=now)
        return True

    def _left(self) -> Any:
        """Runs no worker has: not finished, and not running, or running with a lease that ran out."""
        return sa.or_(
            runs.c.status.in_([QUEUED, PAUSED, WAITING]),
            sa.and_(runs.c.status == RUNNING, runs.c.lease_until < self._now()),
        )

    async def time_out(self, run_id: str, *, pause_id: str, comment: str) -> dict[str, Any] | None:
        """
        Nobody decided an approval the run waits at by its deadline: a
        rejection by nobody, and the run is queued to carry on with it.

        :return: The run; None when it doesn't wait at ``pause_id`` any more.
        """
        try:
            run = await self.decide(
                run_id, request_id=pause_id, approved=False, comment=comment, actor=Actor(), timed_out=True
            )
        except (NoSuchRun, NotOpen):
            return None
        return run

    async def interrupted(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Running runs whose lease ran out (their worker went), oldest
        first; not those someone abandoned meanwhile (``stopping_left``)."""
        return await self._find(
            sa.and_(
                runs.c.status == RUNNING,
                runs.c.lease_until < self._now(),
                runs.c.cancel_requested_at.is_(None),
            ),
            limit,
        )

    async def stalled(self, *, idle_seconds: float, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Queued runs no worker took for ``idle_seconds`` since they
        were due (their job was lost: queued, or the end of a hiccup's
        backoff), and waiting runs whose time has come."""
        now = self._now()
        idle = now - timedelta(seconds=idle_seconds)
        due = sa.func.coalesce(runs.c.next_attempt_at, runs.c.updated_at)
        return await self._find(
            sa.or_(
                sa.and_(runs.c.status == QUEUED, due < idle),
                sa.and_(runs.c.status == WAITING, runs.c.waiting_until <= idle),
            ),
            limit,
        )

    async def overdue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Paused (or waiting) runs with a gate past its deadline
        (``overdue_gates`` says which)."""
        found = await self._find(runs.c.status.in_([PAUSED, WAITING]), limit=None)
        now = self.clock()
        return [run for run in found if overdue_gates(run, now)][:limit]

    # ------------------------------------------------------------------ helpers

    async def _find(self, where: Any, limit: int | None) -> list[dict[str, Any]]:
        query = sa.select(*SUMMARY_COLUMNS).where(where).order_by(runs.c.updated_at)
        if limit is not None:
            query = query.limit(limit)
        async with self.engine.connect() as conn:
            return [_row(r) for r in (await conn.execute(query)).mappings()]

    async def _must(self, run_id: str) -> dict[str, Any]:
        run = await self.get(run_id)
        if run is None:
            raise NoSuchRun(run_id)
        return run

    async def _locked(self, conn: AsyncConnection, run_id: str, organization_id: str | None) -> dict[str, Any]:
        query = sa.select(runs).where(runs.c.id == run_id).with_for_update()
        if organization_id is not None:
            query = query.where(runs.c.organization_id == organization_id)
        found = (await conn.execute(query)).mappings().first()
        if found is None:
            raise NoSuchRun(run_id)
        return _row(found)

    async def _alone_in_session(self, conn: AsyncConnection, run: dict[str, Any]) -> None:
        """
        :raises NotAllowed: Another run of the run's ADK session is still going.
            Two runs waiting at the same step of one session ask under the same
            ID, and ADK hands an answer to the newer of them.
        """
        other = (
            await conn.execute(
                sa.select(runs.c.id)
                .where(
                    runs.c.organization_id == run["organization_id"],
                    runs.c.session_id == run["session_id"],
                    runs.c.id != run["id"],
                    runs.c.status.in_(list(OPEN)),
                )
                .order_by(runs.c.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if other is not None:
            raise NotAllowed(
                f"Run {other[:8]} shares this run's session and is still going: "
                "let it finish, or abandon it, then try again"
            )

    async def _update(
        self, conn: AsyncConnection, run_id: str, values: dict[str, Any], *, expect: Iterable[str]
    ) -> None:
        result = await conn.execute(
            sa.update(runs).where(runs.c.id == run_id, runs.c.status.in_(list(expect))).values(values)
        )
        if result.rowcount != 1:
            raise NotAllowed("The run moved on meanwhile")

    async def _fenced(
        self, conn: AsyncConnection, run_id: str, owner: str, values: dict[str, Any], *, going: bool = False
    ) -> None:
        """
        A write of the owner's to the run it has: its lease is the owner's,
        and hasn't run out (any worker may take the run once it has).

        :param going: It carries the run on (a checkpoint, a pause, a wait, a
            retry): it's refused once someone abandoned the run.
        :raises LostLease: The owner lost the run, or its lease ran out.
        :raises CancelRequested: ``going``, and someone abandoned the run.
        """
        now = self._now()
        fence = [runs.c.id == run_id, *self._lease(owner, now)]
        if going:
            fence.append(runs.c.cancel_requested_at.is_(None))
        result = await conn.execute(sa.update(runs).where(*fence).values(values))
        if result.rowcount == 1:
            return
        if going and await self._stopping(conn, run_id, owner, now):
            raise CancelRequested(f"run {run_id} was abandoned while it ran")
        raise LostLease(f"run {run_id} is no longer {owner}'s, or its lease ran out")

    def _lease(self, owner: str, now: datetime) -> list[Any]:
        """The fence of the owner's writes: the run is running, under the owner's lease, which hasn't run out."""
        return [runs.c.status == RUNNING, runs.c.lease_owner == owner, runs.c.lease_until > now]

    def _held(self, lease_until: datetime | None) -> bool:
        """:return: Whether a lease that runs out at ``lease_until`` (as the column keeps it) hasn't yet."""
        return lease_until is not None and _naive(lease_until) > self._now()

    async def _stopping(self, conn: AsyncConnection, run_id: str, owner: str, now: datetime) -> bool:
        """:return: Whether the owner still has the run, and someone abandoned it."""
        found = (
            await conn.execute(
                sa.select(runs.c.cancel_requested_at).where(runs.c.id == run_id, *self._lease(owner, now))
            )
        ).first()
        return found is not None and found.cancel_requested_at is not None

    async def _event(
        self,
        conn: AsyncConnection,
        run_id: str,
        kind: str,
        message: str,
        *,
        at: datetime,
        actor: Actor | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            sa.insert(events).values(
                run_id=run_id,
                at=at,
                kind=kind[:32],
                message=message[:1000],
                actor_id=actor.id if actor else None,
                actor_name=(actor.name[:255] or None) if actor else None,
                attributes=_json(attributes) if attributes else None,
            )
        )


def _failure(error: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """A failure as a run keeps it: ``{message, category, step, occurred_at}``."""
    error = dict(error or {})
    category = error.get("category")
    return {
        "message": str(error.get("message") or "It failed")[:2000],
        "category": category if category in FAILURE_CATEGORIES else "error",
        "step": error.get("step"),
        "occurred_at": now.replace(tzinfo=UTC).isoformat(),
    }


def open_gates(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """:return: The approvals and questions an unfinished run waits at, each
    decided on its own (``{id, key, kind, reason, details, requested_at,
    deadline}``): its gates, or (a run from before gates) its pause."""
    if run.get("status") in FINISHED:
        return []
    gates = run.get("gates")
    if isinstance(gates, list):
        return [gate for gate in gates if isinstance(gate, dict) and gate.get("id")]
    pause = run.get("pause")
    return [pause] if run.get("status") == PAUSED and isinstance(pause, dict) and pause.get("id") else []


def overdue_gates(run: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """:return: The run's open gates past their deadline at ``now``."""
    late = []
    for gate in open_gates(run):
        deadline = gate.get("deadline")
        if isinstance(deadline, str) and deadline and datetime.fromisoformat(deadline) <= now:
            late.append(gate)
    return late


def _gate(asked: Mapping[str, Any], known: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """A gate as the run keeps it: one still open keeps its ID and when it was asked."""
    deadline = asked.get("deadline")
    return {
        "id": str(known["id"]) if known else new_id(),
        "key": str(asked["key"]),
        "kind": str(asked.get("kind") or "approval"),
        "reason": str(asked.get("reason") or "")[:1000],
        "details": _json(dict(asked.get("details") or {})),
        "requested_at": str(known["requested_at"])
        if known and known.get("requested_at")
        else now.replace(tzinfo=UTC).isoformat(),
        "deadline": _naive(deadline).replace(tzinfo=UTC).isoformat() if isinstance(deadline, datetime) else None,
    }
