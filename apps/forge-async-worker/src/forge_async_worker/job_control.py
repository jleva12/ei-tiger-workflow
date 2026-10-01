"""The control a job gets when the worker runs it (forge_tasks.control): the
task framework's run, as a long-running job needs it.

- What the job keeps lives in the run's step context, made durable by a
  checkpoint, and carried into every later attempt (a restart, a resume).
- An approval is one of the task framework's human-in-the-loop gates: the run
  pauses (AWAITING_VALIDATION) and the worker goes; a decision (the
  background tasks API queues it) resumes the run on a worker. Approving is
  the gate's APPROVE; rejecting is an OVERRIDE that changes nothing, so the
  run carries on (down its rejected way) rather than stopping. What was
  decided is kept with the job, so each approval keeps its decision however
  many gates the run passes.
- A wait stops the run (STOPPED, checkpoint kept) and schedules a SAQ job that
  resumes it at the time; the step context says it's waiting, and until when,
  which is how a waiting run is told from one someone stopped.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

from etf import BatchStatus, InstanceQuery, StateStore, StepContext, StopExecution
from etf.audit import AuditQuery
from etf.model import ValidationOutcome
from etf.status import AuditEventType, ValidationDecisionType
from forge_tasks.control import Decision, RunState

if TYPE_CHECKING:
    from etf import StepRun

log = logging.getLogger(__name__)

STATE_KEY = "job_state"  # what the job keeps
GATE_KEY = "job_gate"  # the approval the run is paused at
DECISIONS_KEY = "job_decisions"  # decisions already read, by approval
WAITING_KEY = "job_waiting"  # {"until", "reason"} while the run waits for a time


def waiting_of(step: StepRun, serializer: Any) -> dict[str, Any] | None:
    """What a stopped run's step says it waits for (``{"until", "reason"}``), if it waits."""
    try:
        raw = (
            serializer.deserialize(step.execution_context)
            if not isinstance(step.execution_context, dict)
            else step.execution_context
        )
    except Exception:
        return None
    waiting = raw.get(WAITING_KEY) if isinstance(raw, dict) else None
    return waiting if isinstance(waiting, dict) and waiting.get("until") else None


class EtfJobControl:
    """forge_tasks.control.JobControl over one step of a task framework run."""

    def __init__(self, ctx: StepContext, *, task_type: str, queue: Any, store: StateStore | None) -> None:
        self.ctx = ctx
        self.task_type = task_type
        self.queue = queue
        self.store = store

    @property
    def run_id(self) -> str | None:
        return self.ctx.run_id

    @property
    def instance_id(self) -> str | None:
        return self.ctx.instance_id

    # ------------------------------------------------------------------ state

    def load(self, key: str, default: Any = None) -> Any:
        state = self.ctx.get(STATE_KEY) or {}
        return copy.deepcopy(state.get(key, default))

    def keep(self, key: str, value: Any) -> None:
        state = dict(self.ctx.get(STATE_KEY) or {})
        state[key] = json.loads(json.dumps(value, default=str))
        self.ctx.put(STATE_KEY, state)

    async def checkpoint(self, message: str = "") -> None:
        await self.ctx.checkpoint(message)

    async def note(self, message: str, **attributes: Any) -> None:
        await self.ctx.emit_audit(message, **attributes)

    def should_stop(self) -> bool:
        return self.ctx.should_stop()

    # ------------------------------------------------------------------ approvals

    async def approval(
        self,
        *,
        key: str,
        reason: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Decision:
        decisions: dict[str, Any] = dict(self.ctx.get(DECISIONS_KEY) or {})
        if key in decisions:
            return Decision(**decisions[key])
        outcomes = self.ctx.step_context.get(ValidationOutcome.OUTCOMES_KEY) or {}
        if self.ctx.get(GATE_KEY) == key and outcomes:
            # The decision the run was resumed with: the one gate it was paused at.
            blob = outcomes[max(outcomes, key=lambda ordinal: int(ordinal))]
            return await self._decided(key, ValidationOutcome.from_context_blob(blob), decisions)
        self.ctx.put(GATE_KEY, key)
        if timeout_seconds:
            await self._schedule_timeout(key, timeout_seconds)
        # Pauses the run (raises); returns only if the task framework has a decision here already.
        outcome = await self.ctx.request_validation(reason=reason, payload={"key": key, **(details or {})})
        return await self._decided(key, outcome, decisions)

    async def _decided(self, key: str, outcome: ValidationOutcome, decisions: dict[str, Any]) -> Decision:
        decision = Decision(
            approved=outcome.decision is ValidationDecisionType.APPROVE,
            comment=outcome.comment,
            actor_id=outcome.actor_id,
            actor_name=await self._name_of(outcome),
            request_id=outcome.request_id,
        )
        decisions[key] = asdict(decision)
        self.ctx.put(DECISIONS_KEY, decisions)
        self.ctx.put(GATE_KEY, None)
        # Read: the next gate starts from nothing, so it can't read this one's decision.
        self.ctx.step_context.remove(ValidationOutcome.OUTCOMES_KEY)
        await self.ctx.checkpoint()
        return decision

    async def _name_of(self, outcome: ValidationOutcome) -> str:
        """The decider's name, from the decision's audit event (the gate keeps only their ID)."""
        if self.store is None:
            return ""
        try:
            events = await self.store.query_audit(
                AuditQuery(
                    instance_id=self.ctx.instance_id, event_types=[AuditEventType.VALIDATION_DECISION], limit=200
                )
            )
        except Exception:
            log.warning("couldn't read who decided %s", outcome.request_id, exc_info=True)
            return ""
        for event in reversed(events):
            if event.actor is None:
                continue
            same = (
                event.attributes.get("validation_request_id") == outcome.request_id
                if outcome.request_id
                else event.actor.id == outcome.actor_id
            )
            if same:
                return event.actor.display_name or ""
        return ""

    async def _schedule_timeout(self, key: str, seconds: int) -> None:
        schedule = getattr(self.queue, "enqueue_decision", None)
        if schedule is None or self.instance_id is None:
            log.warning("approval %s has a timeout, but this queue can't schedule it", key)
            return
        at = datetime.now(UTC).timestamp() + seconds
        await schedule(
            self.task_type,
            self.instance_id,
            gate=key,
            approved=False,
            comment=f"Nobody decided within {seconds // 3600 or 1} hour(s)",
            actor=None,
            at=at,
            key=f"timeout:{self.instance_id}:{key}",
        )

    # ------------------------------------------------------------------ waiting

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        # A naive time is UTC, like everything the worker stores; never the host's zone.
        when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
        self.ctx.put(WAITING_KEY, {"until": when.isoformat(), "reason": reason})
        await self.ctx.checkpoint(reason)
        schedule = getattr(self.queue, "enqueue_resume", None)
        if schedule is None or self.instance_id is None:
            raise RuntimeError("this worker's queue can't schedule a resume")
        await schedule(
            self.task_type,
            self.instance_id,
            at=when.timestamp(),
            key=f"resume:{self.instance_id}:{int(when.timestamp())}",
        )
        raise StopExecution(f"waiting until {when.isoformat(timespec='seconds')}: {reason}")

    # ------------------------------------------------------------------ other runs

    async def find_run(self, labels: dict[str, str]) -> RunState | None:
        if self.store is None:
            return None
        page = await self.store.query_instances(InstanceQuery(labels=dict(labels), limit=1))
        if not page.items:
            return None
        instance = page.items[0]
        latest = await self.store.find_latest_run(instance.id)
        if latest is None:
            return RunState(id=instance.id, status="running")
        status = latest.status
        failure = latest.failures[-1].message if latest.failures else None
        if status is BatchStatus.COMPLETED:
            result = None
            for step in await self.store.find_step_runs(latest.id):
                stored = step.attributes.get("result")
                if isinstance(stored, dict):
                    result = stored
            return RunState(id=instance.id, status="succeeded", result=result)
        if status in (BatchStatus.FAILED, BatchStatus.ABANDONED):
            return RunState(id=instance.id, status="failed", error=failure or status.value.lower())
        if status in (BatchStatus.AWAITING_VALIDATION, BatchStatus.PAUSED):
            return RunState(id=instance.id, status="waiting")
        if status is BatchStatus.STOPPED:
            steps = await self.store.find_step_runs(latest.id)
            waits = any(waiting_of(step, _JSON) for step in steps)
            return RunState(id=instance.id, status="waiting" if waits else "stopped", error=None if waits else failure)
        return RunState(id=instance.id, status="running")


class _Json:
    @staticmethod
    def deserialize(value: Any) -> Any:
        if isinstance(value, bytes | bytearray):
            value = value.decode()
        return json.loads(value) if isinstance(value, str) else value


_JSON = _Json()
