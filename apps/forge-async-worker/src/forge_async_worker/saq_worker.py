"""The SAQ worker: serves the ``adk_workflows`` queue with ``run_adk`` and
``expire_pause``, and runs ``maintain`` every minute (jobs.py says what each
does to a run). Jobs are coroutines, so a worker runs several at once on its
event loop.

    forge-async-worker worker                   # the adk_workflows queue
    forge-async-worker worker --concurrency 8
    forge-async-worker worker --check           # health: this host serves the queue

Other services enqueue by name, without this package: ``queue.enqueue("run_adk",
run_id=..., key=...)`` on the ``adk_workflows`` queue. The worker runs every
job under its own settings (queue.JOB_OPTIONS), whatever it was enqueued with.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from saq import CronJob, Queue, Worker
from sqlalchemy.ext.asyncio import create_async_engine

from forge_async_worker.config import WorkerSettings
from forge_async_worker.jobs import AdkRunJobs
from forge_async_worker.queue import JOB_OPTIONS, MAINTAIN, QUEUE, RunQueue
from forge_async_worker.runtime import Runtime, build_runtime, default_registry
from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.run_store import RunStore, runs
from forge_task_adk_workflows.task import TASK_NAME
from forge_tasks.settings import load_section
from forge_tasks.tasks import JobSpec, TaskRegistry

log = logging.getLogger(__name__)

CONTEXT_KEY = "forge"  # SAQ's context keeps its own worker under "worker"
MAINTENANCE_CRON = "* * * * *"
NO_STORE = (
    "The worker keeps ADK workflow runs in the admin MySQL: set HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL "
    "(mysql+aiomysql://...)"
)


@dataclass
class WorkerState:
    """What every job of the process shares: the built tasks, the run store,
    the queue, and the jobs over them."""

    settings: WorkerSettings
    runtime: Runtime
    store: RunStore
    queue: RunQueue
    jobs: AdkRunJobs


# ----------------------------------------------------------------------------- the jobs


async def run_adk(ctx: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """
    Executes an asynchronous ADK (Application Development Kit) job run based on the given
    context and run identifier. This function interacts with a stateful worker to perform the
    job execution and returns the job results after completion.

    :param ctx: A dictionary representing the context of the job execution. The context
        contains various key-value pairs required for job execution. For example, it includes
        application-specific state information and optionally a "job" key that specifies job
        configurations or details.
    :type ctx: dict[str, Any]
    :param run_id: A string representing the identifier for the specific job run. This is
        used to uniquely identify the job execution request.
    :return: A dictionary containing the result of the ADK job execution. The result may
        include information such as the status, output, performance metrics, and any additional
        data generated during the job execution.
    :rtype: dict[str, Any]
    """
    state: WorkerState = ctx[CONTEXT_KEY]
    return await state.jobs.run_adk(run_id, job=ctx.get("job"))


async def expire_pause(ctx: dict[str, Any], *, run_id: str, pause_id: str) -> dict[str, Any]:
    """
    Expire a pause associated with a specific job run. This function interacts with the
    worker state and updates the job's pause status, expiring the pause identified by
    the given pause ID. The context provided must include the necessary state to perform
    this operation.

    :param ctx: A dictionary representing the context, containing the worker state.
    :type ctx: dict[str, Any]
    :param run_id: The unique identifier of the job run associated with the pause.
    :param pause_id: The unique identifier of the pause to be expired.
    :return: A dictionary with the result of the pause expiration operation.
    :rtype: dict[str, Any]
    """
    state: WorkerState = ctx[CONTEXT_KEY]
    return await state.jobs.expire_pause(run_id, pause_id)


async def maintain(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Perform maintenance tasks associated with the given context.

    This function retrieves the worker state from the context and executes the
    maintain operation on the associated jobs. The purpose of this function is
    to handle periodic maintenance tasks efficiently, utilizing the underlying
    job state of the worker.

    :param ctx: The context dictionary containing necessary data, including
                the worker state.
    :type ctx: dict[str, Any]
    :return: The result of the maintain operation, representing the outcome of
             the tasks performed.
    :rtype: dict[str, Any]
    """
    state: WorkerState = ctx[CONTEXT_KEY]
    return await state.jobs.maintain()


async def _own_policy(ctx: dict[str, Any]) -> None:
    """
    Determine and apply the specified policy configuration differences for a job within
    the provided context. The function retrieves the intended options for the job's
    function and compares them with the current job's attributes. If there are any
    discrepancies, the job will be updated to match the desired policy.

    :param ctx: The context dictionary containing a "job" key which holds the job object.
    :type ctx: dict[str, Any]
    :return: None
    """
    job = ctx["job"]
    wanted = JOB_OPTIONS.get(job.function, {})
    differs = {name: value for name, value in wanted.items() if getattr(job, name) != value}
    if differs:
        await job.update(**differs)


class _NoFollowUps:
    """
    Handles job queuing as part of a system that processes ADK workflow runs.

    This class is designed to enforce restrictions on job queuing, specifically
    disallowing follow-up job submissions. It is intended for use in workflows
    focused on ADK processing and prohibits any kind of dependent task queuing.

    """

    async def enqueue(self, spec: JobSpec, *, countdown: float | None = None) -> None:
        raise RuntimeError(f"{spec.label()}: this worker runs ADK workflow runs only; it can't queue follow-ups")


# ----------------------------------------------------------------------------- queues and workers


def job_queue(settings: WorkerSettings) -> RunQueue:
    """The ``adk_workflows`` queue, on HYBRID_REDIS_URL."""
    return RunQueue(Queue.from_url(settings.redis_url, name=QUEUE))


def serving(settings: WorkerSettings, requested: Sequence[str] | None = None) -> list[str]:
    """The queues to serve: the ``adk_workflows`` queue, the only one there is."""
    if TASK_NAME not in settings.enabled_tasks:
        raise ValueError(f"the {TASK_NAME} task isn't enabled (HYBRID_ENABLED_TASKS): there's nothing to serve")
    unknown = sorted(set(requested or ()) - {QUEUE})
    if unknown:
        raise ValueError(f"no queue(s) {unknown}: this worker serves the {QUEUE} queue")
    return [QUEUE]


def open_store(settings: AdkWorkflowsSettings) -> RunStore:
    """The run store, on the ADK workflows task's session database (the admin MySQL)."""
    if settings.session_database_url is None:
        raise ValueError(NO_STORE)
    engine = create_async_engine(settings.session_database_url.get_secret_value(), pool_pre_ping=True)
    return RunStore(engine)


def worker_id() -> str:
    # The health check finds this host's workers by the prefix; SAQ splits ids on ":".
    return f"{socket.gethostname()}.{os.getpid()}.{QUEUE}".replace(":", "-")


def build_workers(state: WorkerState, *, concurrency: int, **worker_options: Any) -> list[Worker]:
    """The SAQ worker of the queue, sharing the process's tasks, run store and queue."""
    # The jobs take SAQ's context as a plain dict, with the worker's own entry.
    upkeep: CronJob[Any] = CronJob(maintain, cron=MAINTENANCE_CRON, unique=True, **JOB_OPTIONS[MAINTAIN])
    worker: Worker[Any] = Worker(
        state.queue.queue,
        functions=[run_adk, expire_pause],
        cron_jobs=[upkeep],
        concurrency=concurrency,
        id=worker_id(),
        before_process=_own_policy,
        **worker_options,
    )
    worker.context[CONTEXT_KEY] = state
    return [worker]


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
            log.warning("runs still running after %ss go back to their queue", grace_period)
            return
        await asyncio.sleep(0.2)


async def run_workers(
    workers: Sequence[Worker], *, grace_period: float = 30, stop: asyncio.Event | None = None
) -> None:
    """Runs the workers until SIGINT, SIGTERM or ``stop``. SAQ workers install
    their own signal handlers; the process owns them instead.

    Stopping waits for running jobs, up to grace_period, then stops SAQ at
    once: its own grace period also waits out idle dequeues and upkeep sleeps,
    so every stop would take all of it. A run still running is queued again
    (jobs.py), and SAQ re-queues its job."""
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


# ----------------------------------------------------------------------------- the process


async def start_state(
    settings: WorkerSettings,
    *,
    registry: TaskRegistry | None = None,
    store: RunStore | None = None,
    queue: RunQueue | None = None,
    **runtime_options: Any,
) -> WorkerState:
    """Build the enabled tasks, and open the run store (on the ADK workflows
    task's session database) and the queue."""
    runtime = build_runtime(
        settings,
        queue=_NoFollowUps(),
        registry=registry or default_registry(),
        redis_url=runtime_options.pop("redis_url", settings.redis_url),
        **runtime_options,
    )
    try:
        if TASK_NAME not in runtime.tasks:
            raise ValueError(f"the {TASK_NAME} task isn't enabled (HYBRID_ENABLED_TASKS)")
        if store is None:
            options = getattr(runtime.tasks[TASK_NAME], "settings", None)
            if not isinstance(options, AdkWorkflowsSettings):
                options = load_section(TASK_NAME, AdkWorkflowsSettings)
            store = open_store(options)
        queue = queue or job_queue(settings)
    except BaseException:
        await runtime.close()
        raise
    jobs = AdkRunJobs(store=store, queue=queue, runner=runtime.runner)
    return WorkerState(settings=settings, runtime=runtime, store=store, queue=queue, jobs=jobs)


async def close_state(state: WorkerState) -> None:
    try:
        await state.runtime.close()  # the tasks and their shared clients
    finally:
        try:
            await state.queue.close()
        finally:
            await state.store.engine.dispose()


async def check_store(store: RunStore) -> bool:
    """Whether the run store's tables are there (the admin API's migrations
    create them). Says why not, but never raises."""
    try:
        async with store.engine.connect() as conn:
            await conn.execute(sa.select(runs.c.id).limit(1))
    except Exception as error:
        # The driver's own words (the table is missing, the server is down), without SQLAlchemy's SQL.
        log.warning(
            "the run store can't be used yet (make admin-migrate creates it): %s", getattr(error, "orig", error)
        )
        return False
    return True


async def serve(
    settings: WorkerSettings,
    queue_names: Sequence[str] | None = None,
    *,
    concurrency: int = 4,
    grace_period: int = 30,
    ensure_schema: bool = False,
    registry: TaskRegistry | None = None,
) -> None:
    serving(settings, queue_names)
    state = await start_state(settings, registry=registry)
    try:
        if ensure_schema:
            await state.runtime.ensure_schema()
            await check_store(state.store)
            log.info("schema ensured for %s", list(state.runtime.tasks))
        workers = build_workers(state, concurrency=concurrency)
        log.info("serving queue %s, %d runs at once", QUEUE, concurrency)
        await run_workers(workers, grace_period=grace_period)
    finally:
        await close_state(state)


async def check(settings: WorkerSettings, queue_names: Sequence[str] | None = None) -> bool:
    """True when this host has a live worker on the queue (workers refresh
    their info every 10 seconds)."""
    serving(settings, queue_names)
    queue = job_queue(settings)
    prefix = f"{socket.gethostname()}.".replace(":", "-")
    try:
        info = await queue.queue.info()
        if not any(wid.startswith(prefix) for wid in info.get("workers", {})):
            log.warning("no live worker from this host on queue %s", QUEUE)
            return False
        return True
    finally:
        await queue.close()
