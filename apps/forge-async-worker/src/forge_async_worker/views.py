"""What the background tasks API shows of a task: the enhanced task framework's
records (instance, runs, steps, audit trail) in the shape the admin API and the
web console read. A task is one ETF job instance, a SAQ delivery; its attempts
are the instance's runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from etf import AuditEvent, BatchStatus, FailureRecord, JobInstance, JobRun, StepRun
from forge_async_worker.etf_jobs import JOB_STEP, thaw

INTERRUPTED = frozenset({"etf.WorkerLost", "asyncio.exceptions.CancelledError"})
# Still moving: nothing to resubmit, restart or abandon yet.
ACTIVE = frozenset({BatchStatus.PENDING, BatchStatus.RUNNING, BatchStatus.STOPPING, BatchStatus.PAUSING})
RESTARTABLE = frozenset({BatchStatus.FAILED, BatchStatus.STOPPED})
RESUBMITTABLE = frozenset({BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.STOPPED, BatchStatus.ABANDONED})


class ActorView(BaseModel):
    kind: str
    id: str
    display_name: str = ""


class FailureView(BaseModel):
    type: str
    message: str
    category: str  # transient | permanent | interrupted | error
    occurred_at: datetime | None = None


class FailureDetail(FailureView):
    retryable: bool
    permanent: bool
    step: str | None = None
    stack_trace: str = ""
    cause_chain: list[str] = []


class TaskSummary(BaseModel):
    id: str
    job_name: str
    task_type: str
    kind: str
    description: str | None = None
    status: str
    outcome: str | None = None
    attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    failure: FailureView | None = None
    # A stopped run that only waits (a delay, a look at another system): until when, and why.
    waiting_until: datetime | None = None
    waiting_reason: str | None = None
    # A run paused for a person's decision.
    awaiting_approval: bool = False


class TaskPage(BaseModel):
    items: list[TaskSummary]
    total: int


class Delivery(BaseModel):
    queue: str
    key: str
    enqueued_at: datetime | None = None


class StepView(BaseModel):
    name: str
    status: str
    attempt: int
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None


class RunView(BaseModel):
    id: str
    attempt: int
    status: str
    exit_code: str | None = None
    exit_description: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    restart_of: str | None = None
    requested_by: ActorView | None = None
    failures: list[FailureDetail] = []
    steps: list[StepView] = []


class EventView(BaseModel):
    sequence: int
    run_id: str
    at: datetime
    type: str
    actor: ActorView | None = None
    from_status: str | None = None
    to_status: str | None = None
    step: str | None = None
    message: str | None = None


class Actions(BaseModel):
    resubmit: bool
    restart: bool
    abandon: bool
    decide: bool = False


class ApprovalView(BaseModel):
    """The decision a paused run waits for."""

    id: str
    reason: str
    details: dict[str, Any] = {}
    requested_at: datetime | None = None
    deadline: datetime | None = None


class TaskDetail(TaskSummary):
    payload: dict[str, Any] = {}
    labels: dict[str, str] = {}
    delivery: Delivery | None = None
    requested_by: ActorView | None = None
    correlation_id: str | None = None
    result: dict[str, Any] | None = None
    runs: list[RunView] = []
    events: list[EventView] = []
    approval: ApprovalView | None = None
    actions: Actions


# ----------------------------------------------------------------------------- building blocks


def category(failure: FailureRecord) -> str:
    """Why it failed, as the worker treats it: retried automatically
    (transient, interrupted), not retried (permanent: the input can't succeed;
    error: a bug)."""
    if failure.exception_type in INTERRUPTED:
        return "interrupted"
    if failure.retryable:
        return "transient"
    if failure.attributes.get("permanent"):
        return "permanent"
    return "error"


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _actor(actor: Any) -> ActorView | None:
    if actor is None:
        return None
    kind = getattr(actor.kind, "value", actor.kind)
    return ActorView(kind=str(kind), id=actor.id, display_name=actor.display_name or "")


def _failure(failure: FailureRecord) -> FailureView:
    return FailureView(
        type=failure.exception_type,
        message=failure.message,
        category=category(failure),
        occurred_at=failure.occurred_at,
    )


def _failure_detail(failure: FailureRecord) -> FailureDetail:
    return FailureDetail(
        **_failure(failure).model_dump(),
        retryable=failure.retryable,
        permanent=bool(failure.attributes.get("permanent")),
        step=failure.step_name,
        stack_trace=failure.stack_trace,
        cause_chain=list(failure.cause_chain),
    )


def _status(instance: JobInstance, latest: JobRun | None) -> BatchStatus:
    return latest.status if latest is not None else instance.status


def job_result(steps: list[StepRun]) -> dict[str, Any] | None:
    """What the task's job returned (a JobResult), when its step completed."""
    for step in steps:
        stored = step.attributes.get("result") if step.step_name == JOB_STEP else None
        if isinstance(stored, dict):
            return stored
    return None


def delivery(instance: JobInstance) -> Delivery | None:
    """The SAQ delivery the task came from: its queue, key and enqueue time."""
    raw = instance.identifying_parameters.get("delivery")
    if not isinstance(raw, str) or ":" not in raw:
        return None
    queue, key = raw.split(":", 1)
    enqueued = instance.identifying_parameters.get("enqueued")
    at = None
    if isinstance(enqueued, int | float) and enqueued > 0:
        seconds = enqueued / 1000 if enqueued > 1e11 else enqueued  # SAQ stamps milliseconds
        at = datetime.fromtimestamp(seconds, UTC)
    return Delivery(queue=queue, key=key, enqueued_at=at)


def _utc(at: datetime) -> datetime:
    """An aware UTC time, so the API always sends an offset; naive stored times are UTC."""
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def actions(status: BatchStatus, task_type_queued: bool) -> Actions:
    return Actions(
        resubmit=task_type_queued and status in RESUBMITTABLE,
        restart=task_type_queued and status in RESTARTABLE,
        abandon=status in RESTARTABLE,
    )


# ----------------------------------------------------------------------------- views


def summary(
    instance: JobInstance,
    latest: JobRun | None,
    steps: list[StepRun] | None = None,
    waiting: dict[str, Any] | None = None,
) -> TaskSummary:
    task_type, _, kind = instance.job_name.partition(".")
    status = _status(instance, latest)
    outcome = None
    if status is BatchStatus.COMPLETED and steps is not None:
        result = job_result(steps)
        outcome = result.get("status") if result else None
    description = latest.parameters.get("description") if latest is not None else None
    return TaskSummary(
        id=instance.id,
        job_name=instance.job_name,
        task_type=instance.labels.get("task_type", task_type),
        kind=instance.labels.get("kind", kind),
        description=description if isinstance(description, str) else None,
        status=status.value,
        outcome=outcome,
        attempts=latest.attempt if latest is not None else 0,
        created_at=instance.created_at,
        updated_at=max(instance.updated_at, latest.last_updated) if latest is not None else instance.updated_at,
        started_at=latest.start_time if latest is not None else None,
        ended_at=latest.end_time if latest is not None else None,
        duration_ms=_duration_ms(latest.start_time, latest.end_time) if latest is not None else None,
        failure=_failure(latest.failures[-1]) if latest is not None and latest.failures else None,
        waiting_until=_utc(datetime.fromisoformat(waiting["until"]))
        if waiting and status is BatchStatus.STOPPED
        else None,
        waiting_reason=str(waiting.get("reason") or "") if waiting and status is BatchStatus.STOPPED else None,
        awaiting_approval=status is BatchStatus.AWAITING_VALIDATION,
    )


def run_view(run: JobRun, steps: list[StepRun]) -> RunView:
    return RunView(
        id=run.id,
        attempt=run.attempt,
        status=run.status.value,
        exit_code=run.exit_status.code if run.exit_status is not None else None,
        exit_description=(run.exit_status.description if run.exit_status is not None else "") or "",
        created_at=run.create_time,
        started_at=run.start_time,
        ended_at=run.end_time,
        duration_ms=_duration_ms(run.start_time, run.end_time),
        restart_of=run.restart_of,
        requested_by=_actor(run.requested_by),
        failures=[_failure_detail(f) for f in run.failures],
        steps=[
            StepView(
                name=step.step_name,
                status=step.status.value,
                attempt=step.attempt,
                started_at=step.start_time,
                ended_at=step.end_time,
                duration_ms=_duration_ms(step.start_time, step.end_time),
            )
            for step in steps
        ],
    )


def event_view(event: AuditEvent) -> EventView:
    return EventView(
        sequence=event.sequence,
        run_id=event.run_id,
        at=event.at,
        type=getattr(event.event_type, "value", str(event.event_type)),
        actor=_actor(event.actor),
        from_status=event.from_status.value if event.from_status is not None else None,
        to_status=event.to_status.value if event.to_status is not None else None,
        step=event.step_name,
        message=event.message,
    )


def detail(
    instance: JobInstance,
    runs: list[JobRun],
    steps: dict[str, list[StepRun]],
    events: list[AuditEvent],
    *,
    task_type_queued: bool,
    waiting: dict[str, Any] | None = None,
    approval: ApprovalView | None = None,
) -> TaskDetail:
    """The whole task: ``runs`` oldest first, ``steps`` by run id."""
    latest = runs[-1] if runs else None
    latest_steps = steps.get(latest.id, []) if latest is not None else []
    base = summary(instance, latest, latest_steps, waiting)
    completed = next((r for r in reversed(runs) if r.status is BatchStatus.COMPLETED), None)
    payload = latest.parameters.get("payload") if latest is not None else None
    return TaskDetail(
        **base.model_dump(),
        payload=thaw(payload) if isinstance(payload, dict) else {},
        labels=dict(instance.labels),
        delivery=delivery(instance),
        requested_by=_actor(latest.requested_by) if latest is not None else None,
        correlation_id=latest.correlation_id if latest is not None else None,
        result=job_result(steps.get(completed.id, [])) if completed is not None else None,
        runs=[run_view(run, steps.get(run.id, [])) for run in reversed(runs)],
        events=[event_view(e) for e in sorted(events, key=lambda e: (e.at, e.sequence))],
        approval=approval,
        actions=actions(_status(instance, latest), task_type_queued).model_copy(
            update={"decide": approval is not None and task_type_queued}
        ),
    )
