"""What a long-running job may ask of the run it's part of: durable state it
keeps between attempts, checkpoints, a person's decision, a wait until later,
and notes on the run's activity.

Most jobs never need it: a job runs start to end, and a retry runs it again.
A job that runs for days (an ADK workflow waiting on an approval, a person's
answer, a delay) does: it saves where it is, lets the worker go while it waits, and
carries on from there when it's resumed.

The worker runs an ADK workflow run's job on its run in the run store
(``forge_task_adk_workflows.run_store``) and hands the job a control backed by
it (what the run keeps, the decisions people made at its approvals, its
activity, and the job queued again when a wait is over). Outside the worker
(tests, the CLI) :class:`LocalJobControl` keeps the same state in memory. A
job reads its control with :func:`current_control`.

The waits end the current attempt by raising: :meth:`JobControl.approval`
the first time it's asked for a decision, :meth:`JobControl.wait_for` and
:meth:`JobControl.wait_until` always. Don't swallow what they raise (a broad
``except Exception`` must re-raise :class:`ControlSignal`s): the run pauses on
it.

A job waiting on several people at once (an ADK workflow's ways, each at an
approval of its own) reads each decision with :meth:`JobControl.decided`,
carries on with those made, and waits on the rest together
(:meth:`JobControl.wait_for`): each is decided on its own, and any decision
resumes the run.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn, Protocol, runtime_checkable


@dataclass(frozen=True)
class Decision:
    """A person's decision at an approval."""

    approved: bool
    comment: str = ""
    actor_id: str | None = None
    actor_name: str = ""
    request_id: str | None = None
    #: When it was made (UTC), as it was kept: a deadline is held to it, not
    #: to when the run reads it. None when unknown: the run takes the time it
    #: reads it.
    decided_at: datetime | None = None


@dataclass(frozen=True)
class Gate:
    """A decision a job waits for: what :meth:`JobControl.approval` asks, as
    :meth:`JobControl.wait_for` takes it."""

    key: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    #: With a timeout, no decision in time is a rejection.
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class RunState:
    """Another tracked run, as :meth:`JobControl.find_run` finds it.

    ``status`` is ``running`` (queued or running), ``waiting`` (for a person
    or a time), ``succeeded``, ``failed`` or ``stopped``; ``result`` is what
    its job returned, when it succeeded.
    """

    id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


@runtime_checkable
class JobControl(Protocol):
    @property
    def run_id(self) -> str | None:
        """The run's ID (None outside the worker)."""
        ...

    @property
    def instance_id(self) -> str | None:
        """What stays the same across the run's attempts (None outside the worker)."""
        ...

    def load(self, key: str, default: Any = None) -> Any:
        """A value this job kept, from any earlier attempt of the run."""
        ...

    def keep(self, key: str, value: Any) -> None:
        """Keep a JSON value for the rest of the run; durable at the next checkpoint."""
        ...

    async def checkpoint(self, message: str = "") -> None:
        """Make what's kept durable now: a restart carries on from here.
        ``message`` says where the job is, on the run's activity."""
        ...

    async def approval(
        self,
        *,
        key: str,
        reason: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Decision:
        """A person's decision, asked for once per ``key``. Undecided, it pauses
        the run (raises) until someone decides; decided, it returns at once,
        every time it's asked. With a timeout, no decision in time is a
        rejection."""
        ...

    def decided(self, key: str) -> Decision | None:
        """The decision made at ``key``, if anyone made it; never pauses."""
        ...

    async def wait_for(self, gates: Sequence[Gate], *, until: datetime | None = None, reason: str = "") -> NoReturn:
        """End this attempt until someone decides any of ``gates`` (each on its
        own: the rest stay open), or ``until`` comes (a time it waits for too,
        ``reason`` saying why). Without gates, it's :meth:`wait_until`."""
        ...

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        """End this attempt and carry on at ``when``: checkpoints, schedules the
        resume, and raises. The resumed run starts the job again, from what it kept."""
        ...

    async def note(self, message: str, **attributes: Any) -> None:
        """A line on the run's activity."""
        ...

    async def find_run(self, labels: dict[str, str]) -> RunState | None:
        """The newest tracked run carrying these labels (another job's, e.g.
        one this job started), or None when there's none yet."""
        ...

    def should_stop(self) -> bool:
        """Whether someone asked the run to stop: wind down at a safe point."""
        ...


class ControlSignal(Exception):
    """Ends an attempt so the run can wait: never handle it as a failure."""


class AwaitingDecision(ControlSignal):
    """LocalJobControl: the run waits for a decision at ``key`` (the first of
    ``keys``, every one it waits at)."""

    def __init__(self, key: str, reason: str, keys: Sequence[str] = ()) -> None:
        super().__init__(f"waiting for a decision: {reason}")
        self.key = key
        self.reason = reason
        self.keys = list(keys) or [key]


class WaitingUntil(ControlSignal):
    """LocalJobControl: the run waits until ``when``."""

    def __init__(self, when: datetime, reason: str) -> None:
        super().__init__(f"waiting until {when.isoformat()}: {reason}")
        self.when = when
        self.reason = reason


@dataclass
class LocalJobControl:
    """A control kept in memory, for tests and the CLI: decisions are given
    with :meth:`decide`, and a wait raises :class:`WaitingUntil` for the
    caller to run the job again when it likes."""

    state: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    notes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    checkpoints: int = 0
    # What each checkpoint said, in order.
    checkpoint_messages: list[str] = field(default_factory=list)
    stop: bool = False
    asked: list[str] = field(default_factory=list)
    # Other runs, by their labels (``frozenset(labels.items())``), for find_run.
    runs: dict[frozenset[tuple[str, str]], RunState] = field(default_factory=dict)
    instance: str = "local"

    @property
    def run_id(self) -> str | None:
        return None

    @property
    def instance_id(self) -> str | None:
        return self.instance

    def load(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.state.get(key, default))

    def keep(self, key: str, value: Any) -> None:
        self.state[key] = copy.deepcopy(value)

    async def checkpoint(self, message: str = "") -> None:
        self.checkpoints += 1
        self.checkpoint_messages.append(message)

    def decide(self, key: str, decision: Decision) -> None:
        self.decisions[key] = decision

    async def approval(
        self,
        *,
        key: str,
        reason: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Decision:
        if key in self.decisions:
            return self.decisions[key]
        await self.wait_for([Gate(key, reason, dict(details or {}), timeout_seconds)])

    def decided(self, key: str) -> Decision | None:
        return self.decisions.get(key)

    async def wait_for(self, gates: Sequence[Gate], *, until: datetime | None = None, reason: str = "") -> NoReturn:
        if not gates:
            if until is None:
                raise ValueError("A wait needs a gate or a time")
            raise WaitingUntil(until, reason)
        self.asked.extend(gate.key for gate in gates)
        raise AwaitingDecision(gates[0].key, gates[0].reason, [gate.key for gate in gates])

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        raise WaitingUntil(when, reason)

    async def note(self, message: str, **attributes: Any) -> None:
        self.notes.append((message, attributes))

    async def find_run(self, labels: dict[str, str]) -> RunState | None:
        return self.runs.get(frozenset(labels.items()))

    def should_stop(self) -> bool:
        return self.stop


_current: ContextVar[JobControl | None] = ContextVar("forge_tasks_job_control", default=None)


def current_control() -> JobControl | None:
    """The control of the job running now, if its runner gave it one."""
    return _current.get()


@contextlib.contextmanager
def controlling(control: JobControl) -> Iterator[JobControl]:
    """Give the job run in this block ``control`` (the worker does, per run)."""
    token = _current.set(control)
    try:
        yield control
    finally:
        _current.reset(token)
