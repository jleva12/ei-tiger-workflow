"""
ADK workflow runs, kept in the admin MySQL beside their ADK sessions: what the
admin API starts, lists, decides and acts on, and what the worker runs.

A run's ``status``:

- ``queued``: waiting for a worker. New, decided, retried, recovered, or a
  wait that's over.
- ``running``: a worker has it, as ``lease_owner`` until ``lease_until``
  (renewed while it runs). A run whose lease ran out was interrupted: the
  worker's maintenance queues it again.
- ``paused``: waiting for a person, at the approval or question in ``pause``.
  A decision (or the pause's deadline) queues it again.
- ``waiting``: waiting for a time (a delay), until ``waiting_until``; then a
  worker takes it again.
- ``succeeded``, ``failed``, ``abandoned``: finished. A failed run can be
  retried (it carries on from where it was, as its next ``attempt``); a
  finished one resubmitted (a new run of the same payload).

A run's decisions (``decisions``, by the gate's key) are what the worker's
control answers ``approval`` with; ``state`` is what the run keeps
(``control.keep``), made durable at each checkpoint. Every write a worker makes
while it has a run is fenced on its lease: a worker that lost the run (its lease
ran out, and the run went to another) can't overwrite it.

The admin API owns the tables: its Alembic migration ``0005adk_run_store``
creates them as :data:`metadata` describes them. Times are UTC: stored naive,
read back aware.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterable, Sequence
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
#: What can be abandoned: anything not running and not finished, and a failed run.
ABANDONABLE = frozenset({QUEUED, PAUSED, WAITING, FAILED})

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
    sa.Column("session_id", sa.String(128), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    # The run as the worker runs it (forge_task_adk_workflows.runs.RunPayload).
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("state", sa.JSON, nullable=True),
    sa.Column("decisions", sa.JSON, nullable=True),
    sa.Column("pause", sa.JSON, nullable=True),
    sa.Column("waiting_until", DATETIME, nullable=True),
    sa.Column("waiting_reason", sa.String(500), nullable=True),
    sa.Column("result", sa.JSON, nullable=True),
    sa.Column("error", sa.JSON, nullable=True),
    sa.Column("requested_by", sa.String(64), nullable=False),
    sa.Column("requested_by_name", sa.String(255), nullable=False),
    sa.Column("resubmit_of", sa.String(32), nullable=True),
    sa.Column("lease_owner", sa.String(128), nullable=True),
    sa.Column("lease_until", DATETIME, nullable=True),
    sa.Column("created_at", DATETIME, nullable=False),
    sa.Column("updated_at", DATETIME, nullable=False),
    sa.Column("started_at", DATETIME, nullable=True),
    sa.Column("finished_at", DATETIME, nullable=True),
    sa.Index("ix_adk_runs_organization", "organization_id", "created_at"),
    sa.Index("ix_adk_runs_agent", "organization_id", "agent_id", "created_at"),
    sa.Index("ix_adk_runs_status", "status", "updated_at"),
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
    """The approval or question named isn't the one the run waits at."""


class LostLease(RunStoreError):
    """The worker no longer has the run: its lease ran out and it moved on."""


@dataclass(frozen=True)
class Actor:
    """Who did something to a run: a member, or nobody (the worker, the timer)."""

    id: str | None = None
    name: str = ""


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def _naive(when: datetime) -> datetime:
    """UTC, without a zone, as the columns keep it. A naive time is UTC already."""
    return when.astimezone(UTC).replace(tzinfo=None) if when.tzinfo else when


def _aware(when: datetime | None) -> datetime | None:
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


_TIMES = ("waiting_until", "lease_until", "created_at", "updated_at", "started_at", "finished_at")


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
    ) -> dict[str, Any]:
        """:return: A new run, queued: the caller queues its job."""
        now = self._now()
        run_id = run_id or new_id()
        values = {
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
        message = f"Resubmitted from run {resubmit_of[:8]}" if resubmit_of else "Started"
        async with self.engine.begin() as conn:
            await conn.execute(sa.insert(runs).values(values))
            await self._event(conn, run_id, "created", message, actor=requested_by, at=now)
        return await self._must(run_id)

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
        Decide (or answer) what the paused run waits at; the run is queued to
        carry on with it. ``timed_out``: nobody did by its deadline (the
        worker's rejection).

        :raises NoSuchRun: There's no such run.
        :raises NotOpen: It doesn't wait at ``request_id`` (decided already, or moved on).
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            pause = run["pause"] if run["status"] == PAUSED else None
            if not isinstance(pause, dict) or pause.get("id") != request_id:
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
            await self._update(
                conn,
                run_id,
                {"status": QUEUED, "pause": None, "decisions": decisions, "updated_at": now},
                expect=(PAUSED,),
            )
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

        :raises NotAllowed: It isn't failed.
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            if run["status"] != FAILED:
                raise NotAllowed("Only a failed run can be retried")
            now = self._now()
            attempt = int(run["attempt"]) + 1
            await self._update(
                conn,
                run_id,
                {"status": QUEUED, "attempt": attempt, "finished_at": None, "updated_at": now},
                expect=(FAILED,),
            )
            await self._event(conn, run_id, "retried", f"Retried: attempt {attempt}", actor=actor, at=now)
        return await self._must(run_id)

    async def resubmit(self, run_id: str, *, actor: Actor, organization_id: str | None = None) -> dict[str, Any]:
        """
        :return: A new run of the finished run's payload, queued (another
            invocation of the same ADK session).
        :raises NotAllowed: The run isn't finished.
        """
        run = await self.get(run_id, organization_id=organization_id)
        if run is None:
            raise NoSuchRun(run_id)
        if run["status"] not in FINISHED:
            raise NotAllowed("Only a finished run can be resubmitted")
        return await self.create(
            organization_id=run["organization_id"],
            agent_id=run["agent_id"],
            agent_name=run["agent_name"],
            revision=run["revision"],
            session_id=run["session_id"],
            payload=run["payload"],
            requested_by=actor,
            resubmit_of=run_id,
        )

    async def abandon(self, run_id: str, *, actor: Actor, organization_id: str | None = None) -> dict[str, Any]:
        """
        Give a run up: it never carries on.

        :raises NotAllowed: It's running, or finished (but failed).
        """
        async with self.engine.begin() as conn:
            run = await self._locked(conn, run_id, organization_id)
            if run["status"] not in ABANDONABLE:
                raise NotAllowed("A running or finished run can't be abandoned")
            now = self._now()
            await self._update(
                conn,
                run_id,
                {
                    "status": ABANDONED,
                    "pause": None,
                    "waiting_until": None,
                    "waiting_reason": None,
                    "finished_at": now,
                    "updated_at": now,
                },
                expect=tuple(ABANDONABLE),
            )
            await self._event(conn, run_id, "abandoned", "Abandoned", actor=actor, at=now)
        return await self._must(run_id)

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
            out; None when it isn't to be run now (another worker has it, it's
            paused or finished, or its wait isn't over).
        """
        now = self._now()
        runnable = sa.or_(
            runs.c.status == QUEUED,
            sa.and_(runs.c.status == WAITING, runs.c.waiting_until <= now),
        )
        async with self.engine.begin() as conn:
            before = (
                await conn.execute(sa.select(runs.c.status, runs.c.started_at).where(runs.c.id == run_id))
            ).first()
            result = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runnable)
                .values(
                    status=RUNNING,
                    lease_owner=owner,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    started_at=sa.func.coalesce(runs.c.started_at, now),
                    waiting_until=None,
                    waiting_reason=None,
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
            await self._event(conn, run_id, kind, message, at=now, attributes={"worker": owner})
        return await self._must(run_id)

    async def renew(self, run_id: str, *, owner: str, lease_seconds: float) -> bool:
        """Keep the run the owner's for longer. :return: False when it lost it."""
        now = self._now()
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runs.c.status == RUNNING, runs.c.lease_owner == owner)
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            )
        return result.rowcount == 1

    async def checkpoint(self, run_id: str, *, owner: str, state: dict[str, Any]) -> None:
        """Make what the run keeps durable. :raises LostLease: The owner lost the run."""
        async with self.engine.begin() as conn:
            await self._fenced(conn, run_id, owner, {"state": _json(state), "updated_at": self._now()})

    async def note(
        self,
        run_id: str,
        message: str,
        *,
        kind: str = "note",
        actor: Actor | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """A line on the run's activity."""
        async with self.engine.begin() as conn:
            await self._event(conn, run_id, kind, message, actor=actor, at=self._now(), attributes=attributes)

    async def pause(
        self,
        run_id: str,
        *,
        owner: str,
        state: dict[str, Any],
        key: str,
        kind: str,
        reason: str,
        details: dict[str, Any],
        deadline: datetime | None,
    ) -> dict[str, Any]:
        """
        The run waits for a person: its worker lets it go.

        :return: The pause (its ``id`` names it in a decision).
        :raises LostLease: The owner lost the run.
        """
        now = self._now()
        pause = {
            "id": new_id(),
            "key": key,
            "kind": kind,
            "reason": reason[:1000],
            "details": _json(details),
            "requested_at": now.replace(tzinfo=UTC).isoformat(),
            "deadline": _naive(deadline).replace(tzinfo=UTC).isoformat() if deadline else None,
        }
        async with self.engine.begin() as conn:
            await self._fenced(
                conn,
                run_id,
                owner,
                {
                    "status": PAUSED,
                    "state": _json(state),
                    "pause": pause,
                    "lease_owner": None,
                    "lease_until": None,
                    "updated_at": now,
                },
            )
            what = "an answer" if kind == "human_input" else "a decision"
            await self._event(conn, run_id, "paused", f"Waits for {what}: {reason}"[:1000], at=now)
        return pause

    async def wait(self, run_id: str, *, owner: str, state: dict[str, Any], until: datetime, reason: str) -> None:
        """The run waits for a time: its worker lets it go. :raises LostLease: The owner lost the run."""
        now = self._now()
        async with self.engine.begin() as conn:
            await self._fenced(
                conn,
                run_id,
                owner,
                {
                    "status": WAITING,
                    "state": _json(state),
                    "waiting_until": _naive(until),
                    "waiting_reason": reason[:500],
                    "lease_owner": None,
                    "lease_until": None,
                    "updated_at": now,
                },
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
            "result": _json(result),
            "error": _failure(error, now) if not succeeded else None,
            "lease_owner": None,
            "lease_until": None,
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
    ) -> dict[str, Any] | None:
        """
        A run that stopped on a hiccup (``owner``'s, which lets it go) or was
        interrupted (``owner`` None: its lease ran out), queued again as its
        next attempt.

        :return: The run; None when it isn't the owner's (or, interrupted,
            when its lease hasn't run out after all).
        """
        now = self._now()
        if owner is not None:
            fence = sa.and_(runs.c.status == RUNNING, runs.c.lease_owner == owner)
        else:
            fence = sa.and_(runs.c.status == RUNNING, runs.c.lease_until < now)
        values: dict[str, Any] = {
            "status": QUEUED,
            "attempt": runs.c.attempt + 1,
            "error": _failure(error, now),
            "lease_owner": None,
            "lease_until": None,
            "updated_at": now,
        }
        if state is not None:
            values["state"] = _json(state)
        async with self.engine.begin() as conn:
            result = await conn.execute(sa.update(runs).where(runs.c.id == run_id, fence).values(values))
            if result.rowcount != 1:
                return None
            interrupted = owner is None
            kind = "recovered" if interrupted else "retried"
            why = "Its worker went while it ran" if interrupted else (error.get("message") or "A hiccup")
            await self._event(conn, run_id, kind, f"{why}: queued again"[:1000], at=now)
        return await self._must(run_id)

    async def fail_interrupted(self, run_id: str, *, error: dict[str, Any]) -> bool:
        """A run whose lease ran out once too often: it fails. :return: Whether it did."""
        now = self._now()
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(runs)
                .where(runs.c.id == run_id, runs.c.status == RUNNING, runs.c.lease_until < now)
                .values(
                    status=FAILED,
                    error=_failure(error, now),
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return False
            await self._event(conn, run_id, FAILED, f"Failed: {error.get('message') or 'interrupted'}"[:1000], at=now)
        return True

    async def time_out(self, run_id: str, *, pause_id: str, comment: str) -> dict[str, Any] | None:
        """
        Nobody decided the approval the run waits at by its deadline: a
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
        """:return: Running runs whose lease ran out (their worker went), oldest first."""
        return await self._find(sa.and_(runs.c.status == RUNNING, runs.c.lease_until < self._now()), limit)

    async def stalled(self, *, idle_seconds: float, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Queued runs no worker took for ``idle_seconds`` (their job
        was lost), and waiting runs whose time has come."""
        now = self._now()
        idle = now - timedelta(seconds=idle_seconds)
        return await self._find(
            sa.or_(
                sa.and_(runs.c.status == QUEUED, runs.c.updated_at < idle),
                sa.and_(runs.c.status == WAITING, runs.c.waiting_until <= idle),
            ),
            limit,
        )

    async def overdue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """:return: Paused runs past their pause's deadline."""
        found = await self._find(runs.c.status == PAUSED, limit=None)
        now = self.clock()
        late = []
        for run in found:
            deadline = (run.get("pause") or {}).get("deadline")
            if deadline and datetime.fromisoformat(deadline) <= now:
                late.append(run)
        return late[:limit]

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

    async def _update(
        self, conn: AsyncConnection, run_id: str, values: dict[str, Any], *, expect: Iterable[str]
    ) -> None:
        result = await conn.execute(
            sa.update(runs).where(runs.c.id == run_id, runs.c.status.in_(list(expect))).values(values)
        )
        if result.rowcount != 1:
            raise NotAllowed("The run moved on meanwhile")

    async def _fenced(self, conn: AsyncConnection, run_id: str, owner: str, values: dict[str, Any]) -> None:
        result = await conn.execute(
            sa.update(runs)
            .where(runs.c.id == run_id, runs.c.status == RUNNING, runs.c.lease_owner == owner)
            .values(values)
        )
        if result.rowcount != 1:
            raise LostLease(f"run {run_id} is no longer {owner}'s")

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
