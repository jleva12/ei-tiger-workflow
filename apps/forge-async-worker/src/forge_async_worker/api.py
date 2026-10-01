"""The background tasks API: what the enhanced task framework recorded of every
task the workers ran, and the operator's actions on it. The admin API calls it
to show an organization its tasks (filtered by the organization, the ``tenant`` label) and checks
who may act; this service only checks the admin's bearer token.

    forge-async-worker api [--host 127.0.0.1] [--port 8104]

    GET  /v1/tasks?tenant=&task_type=&status=&limit=&offset=   newest first, with the total
    GET  /v1/tasks/{id}                                        runs, steps, failures, audit trail
    POST /v1/tasks/{id}/resubmit   {"actor": {...}}            the same job again, as a new task
    POST /v1/tasks/{id}/restart    {"actor": {...}}            the task's next attempt, on a worker
    POST /v1/tasks/{id}/abandon    {"actor": {...}}            no more attempts
    POST /v1/tasks/{id}/decisions  {"request_id", "approved", "comment", "actor"}   decide its open approval, on a worker

``GET /v1/tasks`` also takes ``label=<name>:<value>`` (repeatable): the runs of
one workflow, the run another started; and ``exclude_task_type`` (repeatable):
every type but those, such as an organization's tasks without its workflow runs.

It runs no job itself: resubmit and restart queue SAQ jobs that workers run
(``run_job``, ``restart_run``), since the task framework runs a restarted job
in whichever process restarts it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, status
from forge_common.middleware import RequestContextMiddleware
from pydantic import BaseModel, ConfigDict, Field

from etf import Actor, BatchStatus, EtfError, InstanceQuery, JobInstance, RunNotRestartableError
from etf.audit import AuditQuery
from forge_async_worker import views
from forge_async_worker.config import WorkerSettings
from forge_async_worker.etf_jobs import TaskRuns, spec_of, start_task_runs
from forge_async_worker.job_control import waiting_of
from forge_async_worker.queue import SaqJobQueue
from forge_tasks.tasks import TaskRegistry

log = logging.getLogger(__name__)

AUDIT_LIMIT = 1000
TaskId = Annotated[str, Path(min_length=1, max_length=64)]


class ActorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=255)


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: ActorBody | None = None


class Queued(BaseModel):
    queue: str
    key: str


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    comment: str = Field(default="", max_length=4000)
    actor: ActorBody


def _state(request: Request) -> tuple[TaskRuns, SaqJobQueue]:
    return request.app.state.runs, request.app.state.queue


State = Annotated[tuple[TaskRuns, SaqJobQueue], Depends(_state)]


def _check_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    expected: str = request.app.state.token
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid bearer token is required")


def _human(body: ActionBody | None) -> Actor | None:
    if body is None or body.actor is None:
        return None
    return Actor.human(body.actor.id, body.actor.display_name)


async def _instance(runs: TaskRuns, task_id: str) -> JobInstance:
    try:
        return await runs.store.get_job_instance(task_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such task") from None


def _waiting(runs: TaskRuns, steps: list[Any] | None) -> dict[str, Any] | None:
    for step in steps or []:
        waiting = waiting_of(step, runs.config.serializer)
        if waiting:
            return waiting
    return None


async def _summary(runs: TaskRuns, instance: JobInstance) -> views.TaskSummary:
    latest = await runs.store.find_latest_run(instance.id)
    read_steps = latest is not None and latest.status in (BatchStatus.COMPLETED, BatchStatus.STOPPED)
    steps = await runs.store.find_step_runs(latest.id) if read_steps and latest is not None else None
    return views.summary(instance, latest, steps, _waiting(runs, steps))


async def _approval(runs: TaskRuns, latest: Any) -> views.ApprovalView | None:
    if latest is None or latest.status is not BatchStatus.AWAITING_VALIDATION or not latest.open_validation_id:
        return None
    try:
        request = await runs.store.get_validation_request(latest.open_validation_id)
    except KeyError:
        return None
    return views.ApprovalView(
        id=request.id,
        reason=request.reason,
        details=dict(request.payload),
        requested_at=request.created_at,
        deadline=request.sla_deadline,
    )


async def _detail(runs: TaskRuns, queue: SaqJobQueue, instance: JobInstance) -> views.TaskDetail:
    attempts = await runs.store.find_runs_for_instance(instance.id)
    steps = dict(
        zip(
            [r.id for r in attempts],
            await asyncio.gather(*(runs.store.find_step_runs(r.id) for r in attempts)),
            strict=True,
        )
    )
    events = await runs.store.query_audit(AuditQuery(instance_id=instance.id, limit=AUDIT_LIMIT))
    task_type = instance.job_name.partition(".")[0]
    latest = attempts[-1] if attempts else None
    return views.detail(
        instance,
        attempts,
        steps,
        events,
        task_type_queued=task_type in queue.queues,
        waiting=_waiting(runs, steps.get(latest.id)) if latest is not None else None,
        approval=await _approval(runs, latest),
    )


def create_app(
    settings: WorkerSettings,
    *,
    registry: TaskRegistry | None = None,
    runs: TaskRuns | None = None,
    queue: SaqJobQueue | None = None,
) -> FastAPI:
    """The API. ``runs`` and ``queue`` default to the task framework's store
    and the SAQ queues of ``settings`` (tests pass their own)."""
    if settings.api.token is None or not settings.api.token.get_secret_value():
        raise ValueError("set HYBRID_API__TOKEN: the background tasks API only answers callers with it")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_runs = own_queue = None
        if runs is None:
            from forge_async_worker.saq_worker import MAX_AUTO_REDRIVES

            own_runs = await start_task_runs(settings, None, None, max_auto_redrives=MAX_AUTO_REDRIVES)
        if queue is None:
            from forge_async_worker.saq_worker import job_queue

            own_queue = job_queue(settings, registry)
        app.state.runs = runs or own_runs
        app.state.queue = queue or own_queue
        try:
            yield
        finally:
            if own_queue is not None:
                await own_queue.close()
            if own_runs is not None:
                await own_runs.close()

    app = FastAPI(title="forge-async-worker background tasks", lifespan=lifespan)
    # Request ids on every log line of a request, and one access line each.
    app.add_middleware(
        RequestContextMiddleware,
        access_log=settings.logging.access_log,
        exclude_paths=settings.logging.access_log_exclude_paths,
    )
    app.state.token = settings.api.token.get_secret_value()
    app.state.runs, app.state.queue = runs, queue  # given ones serve at once, lifespan or not
    authorized = [Depends(_check_token)]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tasks", dependencies=authorized)
    async def list_tasks(
        state: State,
        tenant: Annotated[str | None, Query(max_length=255)] = None,
        label: Annotated[list[str] | None, Query(max_length=20)] = None,
        task_type: Annotated[list[str] | None, Query(max_length=100)] = None,
        exclude_task_type: Annotated[list[str] | None, Query(max_length=100)] = None,
        status_: Annotated[list[BatchStatus] | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> views.TaskPage:
        runs, _ = state
        labels: dict[str, str] = {}
        for pair in label or []:
            name, sep, value = pair.partition(":")
            if not sep or not name:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"label must be name:value, not {pair!r}")
            labels[name] = value
        if tenant:
            labels["tenant"] = tenant
        page = await runs.store.query_instances(
            InstanceQuery(
                job_name_prefixes=[f"{t}." for t in task_type] if task_type else None,
                exclude_job_name_prefixes=[f"{t}." for t in exclude_task_type] if exclude_task_type else None,
                statuses=status_ or None,
                labels=labels,
                limit=limit,
                offset=offset,
            )
        )
        items = await asyncio.gather(*(_summary(runs, instance) for instance in page.items))
        return views.TaskPage(items=list(items), total=page.total)

    @app.get("/v1/tasks/{task_id}", dependencies=authorized)
    async def get_task(task_id: TaskId, state: State) -> views.TaskDetail:
        runs, queue = state
        return await _detail(runs, queue, await _instance(runs, task_id))

    @app.post("/v1/tasks/{task_id}/resubmit", status_code=status.HTTP_202_ACCEPTED, dependencies=authorized)
    async def resubmit(task_id: TaskId, state: State, body: ActionBody | None = None) -> Queued:
        runs, queue = state
        task = await _detail(runs, queue, await _instance(runs, task_id))
        latest = await runs.store.find_latest_run(task_id)
        if latest is None or not task.actions.resubmit:
            raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status.lower()} task can't be resubmitted")
        name, key = await queue.resubmit(spec_of(latest), of=task_id)
        log.info("%s resubmitted task %s (%s) as %s", _who(body), task_id, task.job_name, key)
        return Queued(queue=name, key=key)

    @app.post("/v1/tasks/{task_id}/restart", status_code=status.HTTP_202_ACCEPTED, dependencies=authorized)
    async def restart(task_id: TaskId, state: State, body: ActionBody | None = None) -> Queued:
        runs, queue = state
        task = await _detail(runs, queue, await _instance(runs, task_id))
        if not task.actions.restart:
            raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status.lower()} task can't be restarted")
        actor = body.actor.model_dump() if body is not None and body.actor is not None else {}
        name, key = await queue.enqueue_restart(task.task_type, task_id, task.attempts, actor)
        log.info("%s restarted task %s (%s) as %s", _who(body), task_id, task.job_name, key)
        return Queued(queue=name, key=key)

    @app.post("/v1/tasks/{task_id}/decisions", status_code=status.HTTP_202_ACCEPTED, dependencies=authorized)
    async def decide(task_id: TaskId, body: DecisionBody, state: State) -> Queued:
        runs, queue = state
        task = await _detail(runs, queue, await _instance(runs, task_id))
        if task.approval is None or task.approval.id != body.request_id or not task.actions.decide:
            raise HTTPException(status.HTTP_409_CONFLICT, "That approval isn't open")
        name, key = await queue.enqueue_decision(
            task.task_type,
            task_id,
            request_id=body.request_id,
            approved=body.approved,
            comment=body.comment,
            actor=body.actor.model_dump(),
        )
        log.info(
            "%s %s task %s (%s)", body.actor.id, "approved" if body.approved else "rejected", task_id, task.job_name
        )
        return Queued(queue=name, key=key)

    @app.post("/v1/tasks/{task_id}/abandon", dependencies=authorized)
    async def abandon(task_id: TaskId, state: State, body: ActionBody | None = None) -> views.TaskDetail:
        runs, queue = state
        instance = await _instance(runs, task_id)
        latest = await runs.store.find_latest_run(task_id)
        if latest is None or latest.status not in views.RESTARTABLE:
            current = (latest.status if latest else instance.status).value.lower()
            raise HTTPException(status.HTTP_409_CONFLICT, f"A {current} task can't be abandoned")
        try:
            await runs.operator.abandon(latest.id, actor=_human(body))
        except (EtfError, RunNotRestartableError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        log.info("%s abandoned task %s (%s)", _who(body), task_id, instance.job_name)
        return await _detail(runs, queue, await _instance(runs, task_id))

    return app


def _who(body: ActionBody | None) -> Any:
    return body.actor.id if body is not None and body.actor is not None else "someone"
