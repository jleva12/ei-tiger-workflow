"""The SAQ worker on a real Redis: jobs run concurrently, transient errors are
rescheduled with our backoff, other errors fail at once, every schedule (and
the task framework's upkeep) gets its own cron key, and the health check sees
this host. The task framework runs in memory.

Needs HYBRID_TEST_REDIS_URL, a Redis database these tests clear (SAQ keys and
job locks): make async-worker-test-redis uses database 15 of shared Redis."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from forge_async_worker import saq_worker
from forge_async_worker.saq_worker import JOB_OPTIONS
from forge_tasks.errors import StorageError

from .conftest import worker_settings
from .toy_tasks import toy_registry

URL = os.environ.get("HYBRID_TEST_REDIS_URL")
pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(not URL, reason="needs HYBRID_TEST_REDIS_URL (make async-worker-test-redis)"),
]
STORE = {"task_type": "notes", "kind": "store", "payload": {"tenant_id": "t1", "note_id": "n1", "text": "hello"}}


async def _clear(client) -> None:
    for pattern in ("saq:*", "forge-async-worker:lock:*"):
        async for key in client.scan_iter(match=pattern):
            await client.delete(key)


@pytest.fixture
async def stack():
    import redis.asyncio as aioredis

    settings = worker_settings(redis_url=URL)
    registry = toy_registry()
    locks = aioredis.Redis.from_url(URL)
    await _clear(locks)
    queue = saq_worker.job_queue(settings, registry)
    state = await saq_worker.start_state(settings, registry=registry, queue=queue, redis_url=None)
    workers = saq_worker.build_workers(state, locks, ["notes", "alerts"], concurrency=2)
    stop = asyncio.Event()  # what SIGINT and SIGTERM set
    running = asyncio.create_task(saq_worker.run_workers(workers, grace_period=10, stop=stop))
    yield settings, state.runtime, queue, stop, running
    stop.set()
    await running
    await saq_worker.close_state(state)
    await _clear(locks)
    await locks.aclose()


async def enqueue(queue, spec):
    return await queue.queues[spec["task_type"]].enqueue("run_job", spec=spec, **JOB_OPTIONS)


async def test_jobs_run_concurrently_up_to_the_limit(stack, monkeypatch):
    from saq.job import Status

    _, runtime, queue, *_ = stack
    real_run, in_flight, peak = runtime.runner.run, [0], [0]

    async def tracked(spec):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        try:
            await asyncio.sleep(0.3)
            return await real_run(spec)
        finally:
            in_flight[0] -= 1

    monkeypatch.setattr(runtime.runner, "run", tracked)
    specs = [{**STORE, "payload": {**STORE["payload"], "note_id": f"n{i}", "text": f"note {i}"}} for i in range(4)]
    jobs = [await enqueue(queue, s) for s in specs]
    for job in jobs:
        await job.refresh(until_complete=30)
        assert job.status == Status.COMPLETE and job.result["status"] == "ok" and job.result["detail"]["run_id"]
    assert peak[0] == 2  # concurrency=2 on the notes queue
    assert sorted(runtime.notes.notes) == [f"note {i}" for i in range(4)]


async def test_transient_error_is_rescheduled_with_backoff(stack, monkeypatch):
    from saq.job import Status

    _, runtime, queue, *_ = stack

    async def boom(spec):
        raise StorageError("primary stepped down")

    monkeypatch.setattr(runtime.runner, "run", boom)
    started = time.time()
    job = await enqueue(queue, STORE)
    for _ in range(100):
        await job.refresh()
        if job.status == Status.QUEUED and job.attempts == 1:
            break
        await asyncio.sleep(0.1)
    assert job.status == Status.QUEUED and job.attempts == 1 and "StorageError" in (job.error or "")
    assert "TransientError" in (job.error or "")  # the run failed retryably; its retry re-drives it
    notes = queue.queues["notes"]
    due = await notes.redis.zscore(notes.namespace("incomplete"), job.id)
    assert 22.5 - 1 <= due - started <= 37.5 + 5  # backoff(0): 30s +-25%


async def test_other_errors_fail_without_retry(stack, monkeypatch):
    from saq.job import Status

    _, runtime, queue, *_ = stack

    async def bug(spec):
        raise RuntimeError("a bug, not an outage")

    monkeypatch.setattr(runtime.runner, "run", bug)
    job = await enqueue(queue, STORE)
    await job.refresh(until_complete=30)
    assert job.status == Status.FAILED and job.attempts == 1 and "a bug" in (job.error or "")


async def test_each_schedule_has_its_own_cron_key(stack):
    _, runtime, queue, *_ = stack
    notes = queue.queues["notes"]
    names = [s.name for s in runtime.registry.schedules(["notes"])]
    jobs = []
    for _ in range(50):
        jobs = [await notes.job(f"cron:schedule:{name}") for name in names]
        if all(jobs):
            break
        await asyncio.sleep(0.1)
    assert all(job is not None and job.scheduled > time.time() for job in jobs)
    sweep = jobs[names.index("notes.sweep")]
    assert sweep is not None and sweep.scheduled % 900 == 0  # */15, on the clock
    for name in ("notes", "alerts"):  # the task framework's upkeep, once per queue
        upkeep = await queue.queues[name].job("cron:maintain")
        assert upkeep is not None and upkeep.scheduled % 300 == 0


async def test_health_check_sees_this_hosts_workers(stack):
    settings, *_ = stack
    healthy = False
    for _ in range(50):
        healthy = await saq_worker.check(settings, registry=toy_registry())
        if healthy:
            break
        await asyncio.sleep(0.1)
    assert healthy and await saq_worker.check(settings, ["notes"], registry=toy_registry())


async def test_stopping_waits_for_running_jobs(stack, monkeypatch):
    from saq.job import Status

    _, runtime, queue, stop, running = stack
    real_run = runtime.runner.run

    async def slow(spec):
        await asyncio.sleep(1.5)
        return await real_run(spec)

    monkeypatch.setattr(runtime.runner, "run", slow)
    job = await enqueue(queue, STORE)
    for _ in range(50):
        await job.refresh()
        if job.status == Status.ACTIVE:
            break
        await asyncio.sleep(0.05)
    assert job.status == Status.ACTIVE
    stop.set()
    await asyncio.wait_for(running, timeout=8)
    await job.refresh()
    assert job.status == Status.COMPLETE and job.result["status"] == "ok"


async def test_idle_workers_stop_at_once(stack):
    *_, stop, running = stack
    await asyncio.sleep(0.5)  # started, nothing to do
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(running, timeout=8)
    assert time.monotonic() - started < 3  # not SAQ's full grace period


async def test_bare_enqueues_run_under_the_workers_policy(stack):
    """Another service enqueues by name with none of the worker's options."""
    from saq.job import Status

    _, _, queue, *_ = stack
    job = await queue.queues["notes"].enqueue("run_job", spec={"task_type": "notes", "kind": "explode"})
    assert (job.timeout, job.retries) == (10, 1)  # SAQ's defaults
    await job.refresh(until_complete=30)
    assert job.status == Status.COMPLETE and job.result["status"] == "failed"  # unknown job: no retry
    assert (job.timeout, job.heartbeat, job.retries) == (0, saq_worker.HEARTBEAT, saq_worker.MAX_RETRIES + 1)
