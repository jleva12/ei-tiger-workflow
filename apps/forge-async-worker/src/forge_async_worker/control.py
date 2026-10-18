"""
The control an ADK workflow run's job gets on the worker
(:class:`forge_tasks.control.JobControl`), backed by its run in the run store
(``forge_task_adk_workflows.run_store``).

- **State.** ``keep`` and ``load`` work on a copy of what the run keeps
  (``run["state"]``); a checkpoint makes it durable, fenced on the worker's
  lease. Every attempt of the run starts from what the last one kept.
- **Approvals and questions.** ``approval`` (and ``decided``) answers at
  once with the decision the run has for that gate (``run["decisions"]``);
  without one it raises :class:`PauseRun`, which the worker makes the run's
  pause (and, with a timeout, queues its expiry for the deadline).
  ``wait_for`` raises it for every gate the run waits at together, each
  decided on its own, with the time it waits for too, if any. A decision
  queues the run again, and its next attempt reads the decision from the
  same call.
- **Waits.** ``wait_until`` raises :class:`WaitRun`, which the worker makes
  the run's wait, queueing ``run_adk`` for the time.
- **Notes** go on the run's activity, while the worker has the run (fenced
  on its lease, as its checkpoints are). There are no other runs to find.
- **Holding.** The attempt's writes elsewhere (its ADK session's events)
  land inside ``holding``, which holds the worker's lease on the run as they
  do: an attempt whose lease ran out writes nothing more.
- **Stopping.** Abandoned while it runs, a run stops at its next checkpoint,
  which raises :class:`StopRun` (and ``should_stop`` says so once the worker
  learns it renewing the lease, which stops the job too); the worker
  abandons it.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import exc as db

from forge_task_adk_workflows.run_store import Actor, CancelRequested, RunStore, new_id
from forge_tasks.control import ControlSignal, Decision, Gate, RunState
from forge_tasks.errors import TransientError

log = logging.getLogger(__name__)

APPROVAL = "approval"
HUMAN_INPUT = "human_input"


class PauseRun(ControlSignal):
    """The run waits for people: at each of ``gates`` (``{key, kind, reason,
    details, deadline}``), each decided on its own; and with ``until``, for a
    time too (``reason`` why)."""

    def __init__(self, gates: Sequence[dict[str, Any]], *, until: datetime | None = None, reason: str = "") -> None:
        first = gates[0]
        what = "an answer" if first["kind"] == HUMAN_INPUT else "a decision"
        more = f" (and {len(gates) - 1} more)" if len(gates) > 1 else ""
        super().__init__(f"waits for {what}: {first['reason']}{more}")
        self.gates = list(gates)
        self.until = until
        self.reason = reason

    @property
    def key(self) -> str:
        """The first gate's key."""
        return str(self.gates[0]["key"])


class StopRun(ControlSignal):
    """Someone abandoned the run while it ran: it stops, and is abandoned."""

    def __init__(self) -> None:
        super().__init__("abandoned while it ran")


class WaitRun(ControlSignal):
    """The run waits until ``when`` (UTC)."""

    def __init__(self, when: datetime, reason: str) -> None:
        super().__init__(f"waits until {when.isoformat()}: {reason}")
        self.when = when
        self.reason = reason


class RunControl:
    """forge_tasks.control.JobControl over one attempt of a run, which ``owner`` holds."""

    def __init__(
        self,
        run: dict[str, Any],
        *,
        store: RunStore,
        owner: str,
        queue: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.run = run
        self.store = store
        self.owner = owner
        #: Queues a job that takes a run (``RunQueue.run_adk``); None: the maintenance finds it.
        self.queue = queue
        self.state: dict[str, Any] = copy.deepcopy(run.get("state") or {})
        self.decisions: dict[str, Any] = dict(run.get("decisions") or {})
        #: Someone abandoned the run while it runs (the lease's renewal says so).
        self.stopping = False
        # Checkpoints one at a time, each of all there is to keep: steps and
        # tool calls going at once keep things together, and a write of less
        # must never land after one of more.
        self._writing = asyncio.Lock()

    @property
    def run_id(self) -> str:
        return str(self.run["id"])

    @property
    def lease_epoch(self) -> int:
        """Which taking of the run this attempt is: one more each time a worker
        takes it, for the keys that make the attempt's effects happen once."""
        return int(self.run.get("lease_epoch") or 0)

    @property
    def instance_id(self) -> str:
        """The run's ID: the same for every attempt (a resubmitted run is a new one)."""
        return str(self.run["id"])

    # ------------------------------------------------------------------ state

    def load(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.state.get(key, default))

    def keep(self, key: str, value: Any) -> None:
        self.state[key] = json.loads(json.dumps(value, default=str))

    async def checkpoint(self, message: str = "") -> None:
        """
        :raises LostLease: The worker lost the run.
        :raises StopRun: Someone abandoned the run: it stops here.
        :raises TransientError: The store can't be reached.
        """
        with _hiccups("keep what it did"):
            try:
                async with self._writing:
                    await self.store.checkpoint(self.run_id, owner=self.owner, state=self.state)
            except CancelRequested:
                self.stopping = True
                raise StopRun() from None

    async def note(self, message: str, **attributes: Any) -> None:
        """:raises LostLease: The worker lost the run: nothing is noted."""
        await self.store.note(self.run_id, message, attributes=attributes or None, owner=self.owner)

    def holding(self) -> contextlib.AbstractAsyncContextManager[None]:
        """
        While inside, the worker has the run (``RunStore.holding``): for the
        attempt's writes elsewhere (its ADK session's events) that mustn't
        land once another worker may have it.

        :raises LostLease: The worker lost the run.
        """
        return self.store.holding(self.run_id, owner=self.owner)

    def should_stop(self) -> bool:
        return self.stopping

    @property
    def children(self) -> ChildRuns:
        """Runs the run starts: the workflows its LLM agents call as tools."""
        return ChildRuns(self.store, self.queue, parent=self.run_id, owner=self.owner)

    async def find_run(self, labels: dict[str, str]) -> RunState | None:
        return None

    # ------------------------------------------------------------------ waits

    async def approval(
        self,
        *,
        key: str,
        reason: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Decision:
        decided = self.decided(key)
        if decided is not None:
            return decided
        await self.wait_for([Gate(key, reason, dict(details or {}), timeout_seconds)])

    def decided(self, key: str) -> Decision | None:
        decided = self.decisions.get(key)
        if not isinstance(decided, dict):
            return None
        return Decision(
            approved=bool(decided.get("approved")),
            comment=str(decided.get("comment") or ""),
            actor_id=decided.get("actor_id"),
            actor_name=str(decided.get("actor_name") or ""),
            request_id=decided.get("request_id"),
            # When it was made, not when the run reads it: its job may have waited past the deadline.
            decided_at=_decided_at(decided.get("decided_at")),
        )

    async def wait_for(self, gates: Sequence[Gate], *, until: datetime | None = None, reason: str = "") -> NoReturn:
        if not gates:
            if until is None:
                raise ValueError("A wait needs a gate or a time")
            await self.wait_until(until, reason=reason)
        now = _utc(self.store.clock())
        asked = [
            {
                "key": gate.key,
                "kind": HUMAN_INPUT if gate.details.get("kind") == HUMAN_INPUT else APPROVAL,
                "reason": gate.reason,
                "details": dict(gate.details),
                "deadline": now + timedelta(seconds=gate.timeout_seconds) if gate.timeout_seconds is not None else None,
            }
            for gate in gates
        ]
        raise PauseRun(asked, until=_utc(until) if until is not None else None, reason=reason)

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        # A naive time is UTC, like everything the run store keeps; never the host's zone.
        raise WaitRun(_utc(when), reason)


class ChildRuns:
    """
    Starts runs in the run store, and reads them
    (``forge_task_adk_workflows.graph.agent_tools.ChildRuns``): a workflow an
    LLM agent calls as a tool runs as a run of its own, queued like any other,
    linked to the run that started it (``parent``) while ``owner`` has that.

    :param parent: The run that starts them; None for runs of their own.
    :param owner: Its worker: runs are started only while it has the parent.
    """

    def __init__(
        self,
        store: RunStore,
        queue: Callable[[str], Awaitable[None]] | None,
        *,
        parent: str | None = None,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self.parent = parent
        self.owner = owner

    async def start(self, payload: dict[str, Any], *, run_id: str | None = None) -> str:
        """
        :param run_id: The ID its call makes for it: started already (the
            call answered again), it's that run, not another.
        :return: The run's ID.
        :raises NotAllowed: The parent isn't running (this worker's), or is
            being abandoned.
        """
        known = {
            "organization_id": str(payload["tenant_id"]),
            "agent_id": str(payload["agent_id"]),
            "agent_name": str(payload.get("name") or payload["agent_id"]),
            "revision": int(payload.get("revision") or 0),
            "session_id": str(payload["session_id"]),
            "payload": payload,
            "requested_by": Actor(str(payload["run_as"]), str(payload.get("run_as_name") or "")),
            "version": None if payload.get("version") is None else str(payload["version"]),
        }
        with _hiccups("start a run"):
            if self.parent is not None:
                run, created = await self.store.create_child(
                    run_id=run_id or new_id(), parent_run_id=self.parent, owner=self.owner, **known
                )
            else:
                run, created = await self.store.create(run_id=run_id, **known), True
        if not created:
            return str(run["id"])
        if self.queue is not None:
            try:
                await self.queue(run["id"])
            except Exception:
                log.warning("couldn't queue run %s; the maintenance will", run["id"], exc_info=True)
        return str(run["id"])

    async def get(self, run_id: str) -> dict[str, Any] | None:
        with _hiccups("read a run"):
            return await self.store.get(run_id)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _decided_at(value: Any) -> datetime | None:
    """:return: When a decision was made, as the run store keeps it (ISO 8601); None when it doesn't say."""
    try:
        return _utc(datetime.fromisoformat(value)) if isinstance(value, str) and value else None
    except ValueError:
        return None


def is_hiccup(error: BaseException) -> bool:
    """Whether a database error is one trying again may get past (a lost
    connection, a timeout, a lock wait)."""
    if isinstance(error, db.DBAPIError) and error.connection_invalidated:
        return True
    return isinstance(error, db.OperationalError | db.InterfaceError | db.DisconnectionError | db.TimeoutError)


@contextlib.contextmanager
def _hiccups(what: str) -> Iterator[None]:
    try:
        yield
    except db.SQLAlchemyError as error:
        if is_hiccup(error):
            raise TransientError(f"The run can't {what} for now: {type(error).__name__}") from error
        raise
