"""
The control an ADK workflow run's job gets on the worker
(:class:`forge_tasks.control.JobControl`), backed by its run in the run store
(``forge_task_adk_workflows.run_store``).

- **State.** ``keep`` and ``load`` work on a copy of what the run keeps
  (``run["state"]``); a checkpoint makes it durable, fenced on the worker's
  lease. Every attempt of the run starts from what the last one kept.
- **Approvals and questions.** ``approval`` answers at once with the
  decision the run has for that gate (``run["decisions"]``); without one it
  raises :class:`PauseRun`, which the worker makes the run's pause (and, with
  a timeout, queues its expiry for the deadline). A decision queues the run
  again, and its next attempt reads the decision from the same call.
- **Waits.** ``wait_until`` raises :class:`WaitRun`, which the worker makes
  the run's wait, queueing ``run_adk`` for the time.
- **Notes** go on the run's activity. There are no other runs to find, and
  nobody asks a run to stop: it's abandoned only while it isn't running.
"""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import exc as db

from forge_task_adk_workflows.run_store import RunStore
from forge_tasks.control import ControlSignal, Decision, RunState
from forge_tasks.errors import TransientError

APPROVAL = "approval"
HUMAN_INPUT = "human_input"


class PauseRun(ControlSignal):
    """The run waits for a person: the approval or question at ``key``."""

    def __init__(self, *, key: str, kind: str, reason: str, details: dict[str, Any], deadline: datetime | None) -> None:
        super().__init__(f"waits for {'an answer' if kind == HUMAN_INPUT else 'a decision'}: {reason}")
        self.key = key
        self.kind = kind
        self.reason = reason
        self.details = details
        self.deadline = deadline


class WaitRun(ControlSignal):
    """The run waits until ``when`` (UTC)."""

    def __init__(self, when: datetime, reason: str) -> None:
        super().__init__(f"waits until {when.isoformat()}: {reason}")
        self.when = when
        self.reason = reason


class RunControl:
    """forge_tasks.control.JobControl over one attempt of a run, which ``owner`` holds."""

    def __init__(self, run: dict[str, Any], *, store: RunStore, owner: str) -> None:
        self.run = run
        self.store = store
        self.owner = owner
        self.state: dict[str, Any] = copy.deepcopy(run.get("state") or {})
        self.decisions: dict[str, Any] = dict(run.get("decisions") or {})

    @property
    def run_id(self) -> str:
        return str(self.run["id"])

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
        """:raises LostLease: The worker lost the run. :raises TransientError: The store can't be reached."""
        with _hiccups("keep what it did"):
            await self.store.checkpoint(self.run_id, owner=self.owner, state=self.state)

    async def note(self, message: str, **attributes: Any) -> None:
        await self.store.note(self.run_id, message, attributes=attributes or None)

    def should_stop(self) -> bool:
        return False

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
        decided = self.decisions.get(key)
        if isinstance(decided, dict):
            return Decision(
                approved=bool(decided.get("approved")),
                comment=str(decided.get("comment") or ""),
                actor_id=decided.get("actor_id"),
                actor_name=str(decided.get("actor_name") or ""),
                request_id=decided.get("request_id"),
            )
        details = dict(details or {})
        kind = HUMAN_INPUT if details.get("kind") == HUMAN_INPUT else APPROVAL
        deadline = None
        if timeout_seconds is not None:
            deadline = _utc(self.store.clock()) + timedelta(seconds=timeout_seconds)
        raise PauseRun(key=key, kind=kind, reason=reason, details=details, deadline=deadline)

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        # A naive time is UTC, like everything the run store keeps; never the host's zone.
        raise WaitRun(_utc(when), reason)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


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
