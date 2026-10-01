"""The SAQ worker: one generic job function (``run_job``) serves every task
type, on one SAQ queue per task type, and runs each delivery through the
enhanced task framework (etf_jobs.py). Jobs are coroutines, so a worker runs
several at once on its event loop. ``run_job`` takes the job's Redis lock (if
any), keeps the delivery's heartbeat fresh while its run advances, and hands a
retryable failure back to SAQ, which retries it with backoff.

    forge-async-worker worker                           # every enabled task type's queue, one process
    forge-async-worker worker --queues commits --concurrency 4
    forge-async-worker worker --check                   # health: this host serves each queue

Each task type's schedules become SAQ cron jobs on the worker of its queue.
SAQ keys cron jobs by function name, so each schedule gets a function of its
own, and a tick runs once however many workers serve the queue. Every queue
also runs the task framework's maintenance (recover orphaned runs and re-drive
them, expire overdue approvals).

Other services submit jobs by name, without this package: on the queue named
after the task type, ``queue.enqueue("run_job", spec={"task_type": ...,
"kind": ..., "payload": {...}}, key=...)``. The worker runs every job under
its own timeout, heartbeat and retries, whatever the job was enqueued with.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from saq import CronJob, Queue, Worker

from etf import (
    Actor,
    BatchStatus,
    JobAlreadyRunningError,
    JobRun,
    LockAcquisitionError,
    RunNotRestartableError,
    UnknownJobError,
    ValidationDecision,
)
from forge_async_worker.config import WorkerSettings
from forge_async_worker.etf_jobs import TaskRuns, spec_of, start_task_runs
from forge_async_worker.job_control import waiting_of
from forge_async_worker.queue import SaqJobQueue
from forge_async_worker.runtime import Runtime, build_runtime, default_registry
from forge_tasks.errors import TransientError
from forge_tasks.tasks import JobResult, JobSpec, JobStatus, Schedule, TaskRegistry

log = logging.getLogger(__name__)

MAX_RETRIES = 10
BACKOFF_BASE = 30.0  # seconds
BACKOFF_MAX = 1800.0
LOCK_WAIT = 30  # seconds to wait for a held lock, then the job is re-queued this far out
LOCK_TTL = 3600
# Jobs have no timeout (backfills run for hours). A running job touches itself
# every TOUCH_EVERY seconds; the sweeper retries one untouched for HEARTBEAT
# seconds, i.e. whose worker died.
HEARTBEAT = 120
TOUCH_EVERY = 30
JOB_OPTIONS: dict[str, Any] = {"timeout": 0, "heartbeat": HEARTBEAT, "retries": MAX_RETRIES + 1}
# The task framework restarts a failed run for each of SAQ's retries of its delivery.
MAX_AUTO_REDRIVES = MAX_RETRIES + 1
CONTEXT_KEY = "forge"  # SAQ's context keeps its own worker under "worker"


def backoff(retries: int) -> float:
    """30s, 60s, 2m, 4m ... capped at 30m, with +-25% jitter: rides out
    provider overloads, rate limits and database failovers."""
    delay = min(BACKOFF_BASE * (2**retries), BACKOFF_MAX)
    return delay * random.uniform(0.75, 1.25)


class RunFailed(RuntimeError):
    """The delivery's run failed on a bug: the delivery fails, without retries."""


@dataclass
class WorkerState:
    """What every job of the process shares: the built tasks, the task
    framework, and the queue for follow-ups and re-drives."""

    settings: WorkerSettings
    runtime: Runtime
    runs: TaskRuns
    queue: SaqJobQueue


# ----------------------------------------------------------------------------- the job


class _NoLock:
    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None


def _lock(ctx: dict[str, Any], key: str | None) -> Any:
    """Redis lock per job lock_key. It only avoids duplicate work; correctness
    comes from each task's own idempotency/fencing."""
    if key is None:
        return _NoLock()
    return ctx["redis"].lock(f"forge-async-worker:lock:{key}", timeout=LOCK_TTL, blocking_timeout=LOCK_WAIT)


async def _touch(job: Any) -> None:
    while True:
        await asyncio.sleep(TOUCH_EVERY)
        try:
            await job.update()
        except Exception:
            log.warning("heartbeat failed for job %s", job.id, exc_info=True)


async def _own_policy(ctx: dict[str, Any]) -> None:
    """Before each job: a job another service enqueued may carry SAQ's
    defaults (a 10 second timeout, one attempt, no heartbeat); it runs under
    this worker's JOB_OPTIONS instead."""
    job = ctx["job"]
    differs = {name: value for name, value in JOB_OPTIONS.items() if getattr(job, name) != value}
    if differs:
        await job.update(**differs)


def _fail_now(job: Any) -> None:
    """SAQ retries any exception while attempts remain; only a retryable
    failure should be retried, so spend the rest."""
    job.retries = job.attempts


def _retry_after(job: Any, delay: float, label: str) -> None:
    """SAQ schedules the retry with the job's own delay: make it ours."""
    job.retry_delay = delay
    job.retry_backoff = False
    if job.retryable:
        log.warning("job %s retries in %.0fs (attempt %d)", label, delay, job.attempts)
    else:
        log.error("job %s gave up after %d attempts", label, job.attempts)


async def _settle(state: WorkerState, job: Any, run: JobRun, label: str) -> JobResult:
    """The delivery's outcome from its run: acknowledge it, or hand it back to SAQ."""
    if run.status is BatchStatus.COMPLETED:
        return await state.runs.result(run)
    if run.status is BatchStatus.FAILED:
        failure = run.failures[-1] if run.failures else None
        reason = f"{failure.exception_type}: {failure.message}" if failure else f"run {run.id} failed"
        if failure is not None and failure.retryable:
            _retry_after(job, backoff(job.attempts - 1), label)
            raise TransientError(reason)
        if failure is not None and failure.attributes.get("permanent"):
            log.error("job %s failed permanently: %s", label, failure.message)
            return JobResult.failed(failure.message, run_id=run.id)
        _fail_now(job)
        raise RunFailed(reason)
    # Waiting for a person, paused or stopped: acknowledged; the run moves on
    # through the operator (a decision, resume or restart).
    return JobResult(
        status=JobStatus.SKIPPED,
        detail={"reason": f"run {run.status.value.lower()}", "run_id": run.id},
    )


async def run_job(ctx: dict[str, Any], *, spec: dict[str, Any]) -> dict[str, Any]:
    state: WorkerState = ctx[CONTEXT_KEY]
    job = ctx["job"]
    try:
        job_spec = JobSpec.model_validate(spec)
    except Exception:
        _fail_now(job)
        raise
    lock = _lock(ctx, state.runtime.runner.lock_key(job_spec))
    if not await lock.acquire():
        # Someone else is working on the same target. Re-send it as a new job so
        # waiting never consumes the retry budget meant for real failures.
        await state.queue.enqueue(job_spec, countdown=LOCK_WAIT)
        return JobResult(status=JobStatus.SKIPPED, detail={"reason": "locked; re-queued"}).model_dump(mode="json")
    heartbeat = asyncio.create_task(_touch(job))
    try:
        run = await state.runs.launch(
            job_spec,
            queue=ctx["worker"].queue.name,
            key=job.key,
            enqueued=job.queued,
            description=await state.runtime.runner.describe(job_spec),
        )
        result = await _settle(state, job, run, job_spec.label())
    except (JobAlreadyRunningError, LockAcquisitionError) as exc:
        # A duplicate delivery of a run another worker is advancing: look again later.
        _retry_after(job, LOCK_WAIT, job_spec.label())
        raise TransientError(str(exc)) from exc
    except RunNotRestartableError as exc:
        # Its run failed on its own merits before, or was interrupted too often:
        # no retry of the delivery can fix that; an operator restart can.
        log.error("job %s not re-run: %s", job_spec.label(), exc)
        result = JobResult.failed(str(exc))
    except UnknownJobError as exc:
        log.error("job %s: no such job on this worker", job_spec.label())
        result = JobResult.failed(f"UnknownJobError: {exc}")
    except (TransientError, RunFailed):
        raise
    except Exception:
        # The task framework's store or leases are unreachable: SAQ retries.
        _retry_after(job, backoff(job.attempts - 1), job_spec.label())
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        try:
            await lock.release()
        except Exception:
            log.warning("lock release failed for %s (expired?)", job_spec.label())
    return result.model_dump(mode="json")


async def redrive_run(ctx: dict[str, Any], *, instance_id: str) -> dict[str, Any]:
    """Restart a run the maintenance sweep recovered (its worker died)."""
    state: WorkerState = ctx[CONTEXT_KEY]
    return await _advance(ctx, instance_id, lambda: state.runs.operator.redrive(instance_id))


async def restart_run(ctx: dict[str, Any], *, instance_id: str, actor: dict[str, str] | None = None) -> dict[str, Any]:
    """Retry a failed or stopped task as its next attempt, for the person who
    asked (the background tasks API queues this; the job runs here, on a worker)."""
    state: WorkerState = ctx[CONTEXT_KEY]
    who = Actor.human(actor["id"], actor.get("display_name", "")) if actor and actor.get("id") else None
    return await _advance(ctx, instance_id, lambda: state.runs.operator.restart(instance_id, actor=who))


def _skipped(reason: str) -> dict[str, Any]:
    return JobResult(status=JobStatus.SKIPPED, detail={"reason": reason}).model_dump(mode="json")


async def resume_run(ctx: dict[str, Any], *, instance_id: str) -> dict[str, Any]:
    """A waiting run's time came (a delay): it carries on,
    here. A run someone stopped, restarted or abandoned meanwhile is left alone."""
    state: WorkerState = ctx[CONTEXT_KEY]
    latest = await state.runs.store.find_latest_run(instance_id)
    if latest is None or latest.status is not BatchStatus.STOPPED:
        return _skipped(f"run is {latest.status.value.lower() if latest else 'gone'}")
    steps = await state.runs.store.find_step_runs(latest.id)
    if not any(waiting_of(step, state.runs.config.serializer) for step in steps):
        return _skipped("run was stopped, not waiting")
    return await _advance(ctx, instance_id, lambda: state.runs.operator.resume(latest.id, actor=Actor.system()))


async def decide_run(
    ctx: dict[str, Any],
    *,
    instance_id: str,
    approved: bool,
    comment: str = "",
    actor: dict[str, str] | None = None,
    request_id: str | None = None,
    gate: str | None = None,
) -> dict[str, Any]:
    """Decide a run's open approval (a person's decision, or a timeout's
    rejection) and carry the run on, here. Approving is the gate's APPROVE;
    rejecting an OVERRIDE that changes nothing, so the run carries on down its
    rejected way (a REJECT would stop it). A decision for an approval that's
    no longer open (decided already, a timeout after the decision) does nothing."""
    state: WorkerState = ctx[CONTEXT_KEY]
    latest = await state.runs.store.find_latest_run(instance_id)
    if latest is None or latest.status is not BatchStatus.AWAITING_VALIDATION or not latest.open_validation_id:
        return _skipped("no approval is open")
    request = await state.runs.store.get_validation_request(latest.open_validation_id)
    if (request_id and request.id != request_id) or (gate and request.payload.get("key") != gate):
        return _skipped("that approval is no longer open")
    who = Actor.human(actor["id"], actor.get("display_name", "")) if actor and actor.get("id") else Actor.system()
    decision = ValidationDecision.approve(who, comment) if approved else ValidationDecision.override(who, {}, comment)
    return await _advance(
        ctx, instance_id, lambda: state.runs.operator.submit_validation_decision(request.id, decision)
    )


async def _advance(ctx: dict[str, Any], instance_id: str, act: Callable[[], Awaitable[JobRun]]) -> dict[str, Any]:
    """Run an existing task's next attempt under its job's lock, and settle the SAQ job from it."""
    state: WorkerState = ctx[CONTEXT_KEY]
    job = ctx["job"]
    latest = await state.runs.store.find_latest_run(instance_id)
    spec = spec_of(latest) if latest is not None else None
    label = spec.label() if spec else instance_id
    lock = _lock(ctx, state.runtime.runner.lock_key(spec) if spec else None)
    if not await lock.acquire():
        _retry_after(job, LOCK_WAIT, label)
        raise TransientError(f"{label} is locked")
    heartbeat = asyncio.create_task(_touch(job))
    try:
        run = await act()
        result = await _settle(state, job, run, label)
    except (JobAlreadyRunningError, LockAcquisitionError) as exc:
        _retry_after(job, LOCK_WAIT, label)
        raise TransientError(str(exc)) from exc
    except RunNotRestartableError as exc:
        result = JobResult(status=JobStatus.SKIPPED, detail={"reason": str(exc)})
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        with contextlib.suppress(Exception):
            await lock.release()
    return result.model_dump(mode="json")


async def maintain(ctx: dict[str, Any]) -> dict[str, Any]:
    """The task framework's upkeep: recover runs whose worker died and queue
    their re-drive, and expire approvals past their deadline."""
    state: WorkerState = ctx[CONTEXT_KEY]
    heartbeat = asyncio.create_task(_touch(ctx["job"]))
    try:
        operator = state.runs.operator
        stale = timedelta(seconds=state.settings.etf.stale_after_seconds)
        recovered = await operator.recover_stale_runs(older_than=stale)
        for run in recovered:
            await state.queue.enqueue_redrive(run.job_name.split(".", 1)[0], run.instance_id)
        expired = await operator.expire_validations()
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    if recovered or expired:
        log.warning("maintenance: recovered %d orphaned runs, expired %d approvals", len(recovered), len(expired))
    return {"recovered": [run.id for run in recovered], "expired": [request.id for request in expired]}


# ----------------------------------------------------------------------------- schedules


def cron_expression(schedule: Schedule) -> str:
    """A schedule's cron; every_seconds becomes the clock-aligned cron with
    the same period (SAQ's cron takes seconds as an optional sixth field)."""
    if schedule.cron:
        return schedule.cron
    seconds = int(schedule.every_seconds or 3600)
    if 0 < seconds < 60 and 60 % seconds == 0:
        return f"* * * * * */{seconds}"
    minutes, rest = divmod(seconds, 60)
    if not rest and 0 < minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    hours, rest = divmod(seconds, 3600)
    if not rest and 0 < hours < 24 and 24 % hours == 0:
        return f"0 */{hours} * * *"
    if seconds == 86400:
        return "0 0 * * *"
    raise ValueError(f"schedule {schedule.name}: every_seconds={schedule.every_seconds} has no cron equivalent")


def scheduled_function(schedule: Schedule) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    spec = schedule.job.model_dump(mode="json")

    async def fire(ctx: dict[str, Any]) -> dict[str, Any]:
        return await run_job(ctx, spec=spec)

    # SAQ enqueues a cron job under its function's name and keys it "cron:<name>".
    fire.__name__ = fire.__qualname__ = f"schedule:{schedule.name}"
    return fire


def cron_jobs(
    registry: TaskRegistry, enabled: Sequence[str], queue_name: str, *, maintenance_cron: str | None = None
) -> list[CronJob]:
    queues = registry.queues(enabled)
    jobs: list[CronJob] = [
        CronJob(scheduled_function(s), cron=cron_expression(s), unique=True, **JOB_OPTIONS)
        for s in registry.schedules(enabled)
        if queues.get(s.job.task_type) == queue_name
    ]
    if maintenance_cron:
        jobs.append(CronJob(maintain, cron=maintenance_cron, unique=True, **JOB_OPTIONS))
    return jobs


# ----------------------------------------------------------------------------- queues and workers


def job_queue(settings: WorkerSettings, registry: TaskRegistry | None = None) -> SaqJobQueue:
    """The JobQueue that sends jobs to the workers: for the worker's own
    follow-ups, and for any process that enqueues (an API, a webhook)."""
    registry = registry or default_registry()
    names = registry.queues(settings.enabled_tasks)
    by_name = {name: Queue.from_url(settings.redis_url, name=name) for name in sorted(set(names.values()))}
    return SaqJobQueue({task: by_name[name] for task, name in names.items()}, job_options=JOB_OPTIONS)


def serving(settings: WorkerSettings, registry: TaskRegistry, requested: Sequence[str] | None) -> list[str]:
    """Queue names to serve: the requested ones, or every enabled task type's."""
    available = sorted(set(registry.queues(settings.enabled_tasks).values()))
    if not requested:
        return available
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"no enabled task type uses queue(s) {unknown}; enabled queues: {available}")
    return list(dict.fromkeys(requested))


def worker_id(queue_name: str) -> str:
    # The health check finds this host's workers by the prefix; SAQ splits ids on ":".
    return f"{socket.gethostname()}.{os.getpid()}.{queue_name}".replace(":", "-")


def build_workers(
    state: WorkerState,
    locks: Any,
    queue_names: Sequence[str],
    *,
    concurrency: int,
    **worker_options: Any,
) -> list[Worker]:
    """One SAQ worker per queue, sharing the process's tasks, task framework and lock client."""
    by_name = {q.name: q for q in state.queue.queues.values()}
    workers = []
    for name in queue_names:
        worker = Worker(
            by_name[name],
            functions=[run_job, redrive_run, restart_run, resume_run, decide_run],
            cron_jobs=cron_jobs(
                state.runtime.registry,
                state.settings.enabled_tasks,
                name,
                maintenance_cron=state.settings.etf.maintenance_cron,
            ),
            concurrency=concurrency,
            id=worker_id(name),
            before_process=_own_policy,
            **worker_options,
        )
        worker.context.update({CONTEXT_KEY: state, "redis": locks})
        workers.append(worker)
    return workers


class _IdleCancellation(logging.Filter):
    """After the drain, SAQ's stop cancels only idle dequeues and upkeep, yet
    warns each time that tasks missed its grace period. _drain reports jobs
    that really did."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "did not finish within the shutdown grace period" not in record.getMessage()


async def _drain(workers: Sequence[Worker], grace_period: float) -> None:
    """Waits up to grace_period for the jobs the workers are running."""
    deadline = asyncio.get_running_loop().time() + grace_period
    while any(worker.job_task_contexts for worker in workers):
        if asyncio.get_running_loop().time() >= deadline:
            log.warning("jobs still running after %ss go back to their queue", grace_period)
            return
        await asyncio.sleep(0.2)


async def run_workers(
    workers: Sequence[Worker], *, grace_period: float = 30, stop: asyncio.Event | None = None
) -> None:
    """Runs the workers until SIGINT, SIGTERM or ``stop``. Each SAQ worker
    installs its own signal handlers, and in one process only the last would
    stop, so the process owns them instead.

    Stopping waits for running jobs, up to grace_period, then stops SAQ at
    once: its own grace period also waits out idle dequeues and upkeep sleeps,
    so every stop would take all of it. SAQ re-queues jobs still running."""
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    for worker in workers:
        worker.SIGNALS = []
        await worker.queue.connect()
    running = [asyncio.create_task(worker.start()) for worker in workers]
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([stopping, *running], return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            log.info("stopping: waiting up to %ss for running jobs", grace_period)
            await _drain(workers, grace_period)
    finally:
        stopping.cancel()
        saq_log, quiet = logging.getLogger("saq"), _IdleCancellation()
        saq_log.addFilter(quiet)
        try:
            await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
            await asyncio.gather(*running, return_exceptions=True)
        finally:
            saq_log.removeFilter(quiet)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    for task in running:
        error = None if task.cancelled() else task.exception()
        if error is not None:
            raise error


async def start_state(
    settings: WorkerSettings,
    *,
    registry: TaskRegistry | None = None,
    queue: SaqJobQueue | None = None,
    **runtime_options: Any,
) -> WorkerState:
    """Build the enabled tasks on the queue's Redis and connect the task framework."""
    registry = registry or default_registry()
    queue = queue or job_queue(settings, registry)
    # Tasks may publish state other services read (per-repo stats, say) to the queues' Redis.
    runtime = build_runtime(
        settings,
        queue=queue,
        registry=registry,
        redis_url=runtime_options.pop("redis_url", settings.redis_url),
        **runtime_options,
    )
    try:
        runs = await start_task_runs(settings, runtime.tasks, runtime.runner, max_auto_redrives=MAX_AUTO_REDRIVES)
    except BaseException:
        await runtime.close()
        raise
    return WorkerState(settings=settings, runtime=runtime, runs=runs, queue=queue)


async def close_state(state: WorkerState) -> None:
    try:
        await state.runtime.close()  # tasks, shared clients and the queues' connections
    finally:
        await state.runs.close()


async def serve(
    settings: WorkerSettings,
    queue_names: Sequence[str] | None = None,
    *,
    concurrency: int = 4,
    grace_period: int = 30,
    ensure_schema: bool = False,
    registry: TaskRegistry | None = None,
) -> None:
    registry = registry or default_registry()
    names = serving(settings, registry, queue_names)
    state = await start_state(settings, registry=registry)
    locks = aioredis.Redis.from_url(settings.redis_url)
    try:
        if ensure_schema:
            # Only the served task types': each queue's deployment sets up its own.
            served = [name for name, task in state.runtime.tasks.items() if task.queue in names]
            for name in served:
                await state.runtime.tasks[name].ensure_schema()
            log.info("schema ensured for %s", served)
        workers = build_workers(state, locks, names, concurrency=concurrency)
        log.info("serving queues %s, %d jobs at once each", names, concurrency)
        await run_workers(workers, grace_period=grace_period)
    finally:
        await close_state(state)
        await locks.aclose()


async def check(
    settings: WorkerSettings, queue_names: Sequence[str] | None = None, *, registry: TaskRegistry | None = None
) -> bool:
    """True when this host has a live worker on each queue (workers refresh
    their info every 10 seconds)."""
    registry = registry or default_registry()
    names = serving(settings, registry, queue_names)
    queue = job_queue(settings, registry)
    by_name = {q.name: q for q in queue.queues.values()}
    prefix = f"{socket.gethostname()}.".replace(":", "-")
    try:
        for name in names:
            info = await by_name[name].info()
            if not any(wid.startswith(prefix) for wid in info.get("workers", {})):
                log.warning("no live worker from this host on queue %s", name)
                return False
        return True
    finally:
        await queue.close()
