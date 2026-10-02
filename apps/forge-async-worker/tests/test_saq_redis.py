"""The SAQ worker on a real Redis: runs go concurrently up to the limit, a
hiccup's retry is scheduled with backoff, the maintenance has its cron key,
the health check sees this host, a stop waits for running runs but not for
idle workers, and a bare enqueue (as the admin API's) runs under the worker's
settings. The run store is a SQLite file, on the real clock.

Needs HYBRID_TEST_REDIS_URL, a Redis database these tests clear (SAQ keys):
make async-worker-test-redis uses database 15 of shared Redis."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from forge_async_worker import saq_worker
from forge_async_worker.queue import HEARTBEAT, RunQueue
from forge_async_worker.saq_worker import WorkerState
from forge_task_adk_workflows.run_store import QUEUED, SUCCEEDED, RunStore, metadata
from forge_tasks.control import JobControl
from forge_tasks.errors import TransientError

from .conftest import ALICE, payload, worker_settings
from .toy_tasks import ScriptedTask, toy_registry

URL = os.environ.get("HYBRID_TEST_REDIS_URL")
pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(not URL, reason="needs HYBRID_TEST_REDIS_URL (make async-worker-test-redis)"),
]


async def _clear(queue: RunQueue) -> None:
    redis = queue.queue.redis
    async for key in redis.scan_iter(match="saq:*"):
        await redis.delete(key)


@dataclass
class Stack:
    state: WorkerState
    stop: asyncio.Event
    running: asyncio.Task[None]

    @property
    def queue(self) -> RunQueue:
        return self.state.queue

    @property
    def store(self) -> RunStore:
        return self.state.store

    @property
    def task(self) -> ScriptedTask:
        return self.state.runtime.tasks["adk_workflows"]  # type: ignore[return-value]

    async def start(self, *steps: dict[str, Any]) -> str:
        run = await self.store.create(
            organization_id="org-1",
            agent_id="ag-1",
            agent_name="Support desk",
            revision=3,
            session_id="session-1",
            payload=payload(*steps),
            requested_by=ALICE,
        )
        return str(run["id"])

    async def until(self, run_id: str, status: str, *, attempt: int = 1, seconds: float = 10) -> dict[str, Any]:
        """The run, once it's in ``status`` at ``attempt``."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            run = await self.store.get(run_id)
            if run is not None and (run["status"], run["attempt"]) == (status, attempt):
                return run
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} isn't {status} at attempt {attempt}: {await self.store.get(run_id)}")


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    assert URL is not None
    settings = worker_settings(redis_url=URL)
    # A file, not memory: runs go at once, each on a connection of its own.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    queue = saq_worker.job_queue(settings)
    await _clear(queue)
    state = await saq_worker.start_state(
        settings, registry=toy_registry(), store=RunStore(engine), queue=queue, redis_url=None
    )
    workers = saq_worker.build_workers(state, concurrency=2)
    stop = asyncio.Event()  # what SIGINT and SIGTERM set
    running = asyncio.create_task(saq_worker.run_workers(workers, grace_period=10, stop=stop))
    yield Stack(state, stop, running)
    stop.set()
    await running
    await _clear(queue)
    await saq_worker.close_state(state)


async def test_runs_go_concurrently_up_to_the_limit(stack: Stack) -> None:
    in_flight, peak = [0], [0]

    async def tracked(control: JobControl, index: int) -> None:
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        try:
            await asyncio.sleep(0.3)
        finally:
            in_flight[0] -= 1

    stack.task.hook = tracked
    ids = [await stack.start({"do": "hook"}) for _ in range(4)]
    for run_id in ids:
        await stack.queue.run_adk(run_id)

    for run_id in ids:
        await stack.until(run_id, SUCCEEDED)
    assert peak[0] == 2  # concurrency=2


async def test_a_hiccups_retry_is_scheduled_with_backoff(stack: Stack) -> None:
    async def overloaded(control: JobControl, index: int) -> None:
        raise TransientError("Model overloaded")

    stack.task.hook = overloaded
    run_id = await stack.start({"do": "hook"})
    started = time.time()
    await stack.queue.run_adk(run_id)

    queued = await stack.until(run_id, QUEUED, attempt=2)
    assert queued["error"]["category"] == "transient"
    retry = await stack.queue.queue.job(f"adk-run:{run_id}:retry-2")
    assert retry is not None and 22.5 - 1 <= retry.scheduled - started <= 37.5 + 5  # backoff(0): 30s +-25%


async def test_the_maintenance_has_its_own_cron_key(stack: Stack) -> None:
    job = None
    for _ in range(50):
        job = await stack.queue.queue.job("cron:maintain")
        if job is not None:
            break
        await asyncio.sleep(0.1)
    assert job is not None and job.scheduled > time.time() - 1 and job.scheduled % 60 == 0


async def test_the_health_check_sees_this_hosts_worker(stack: Stack) -> None:
    settings = worker_settings(redis_url=URL)
    healthy = False
    for _ in range(50):
        healthy = await saq_worker.check(settings)
        if healthy:
            break
        await asyncio.sleep(0.1)
    assert healthy and await saq_worker.check(settings, ["adk_workflows"])


async def test_stopping_waits_for_running_runs(stack: Stack) -> None:
    busy = asyncio.Event()

    async def slow(control: JobControl, index: int) -> None:
        busy.set()
        await asyncio.sleep(1.5)

    stack.task.hook = slow
    run_id = await stack.start({"do": "hook"})
    await stack.queue.run_adk(run_id)
    await asyncio.wait_for(busy.wait(), 10)

    stack.stop.set()
    await asyncio.wait_for(stack.running, timeout=8)

    assert (await stack.store.get(run_id))["status"] == SUCCEEDED  # type: ignore[index]


async def test_idle_workers_stop_at_once(stack: Stack) -> None:
    await asyncio.sleep(0.5)  # started, nothing to do
    started = time.monotonic()
    stack.stop.set()
    await asyncio.wait_for(stack.running, timeout=8)
    assert time.monotonic() - started < 3  # not SAQ's full grace period


async def test_a_bare_enqueue_runs_under_the_workers_settings(stack: Stack) -> None:
    """The admin API enqueues by name, with none of the worker's settings."""
    from saq.job import Status

    run_id = await stack.start({"do": "step"})
    job = await stack.queue.queue.enqueue("run_adk", run_id=run_id, key=f"adk-run:{run_id}:bare")
    assert job is not None and (job.timeout, job.heartbeat) == (10, 0)  # SAQ's defaults

    await job.refresh(until_complete=10)

    assert job.status == Status.COMPLETE and job.result == {"run_id": run_id, "outcome": "succeeded"}
    assert (job.timeout, job.heartbeat, job.retries) == (0, HEARTBEAT, 1)
    assert (await stack.store.get(run_id))["status"] == SUCCEEDED  # type: ignore[index]
