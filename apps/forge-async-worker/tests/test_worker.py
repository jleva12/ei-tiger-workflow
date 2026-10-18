"""The SAQ side of the worker (saq_worker.py, queue.py): the job functions,
the settings every job runs under, the queues, the workers and the upkeep's
cron. tests/test_saq_redis.py runs the same code on a real Redis."""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import timedelta
from typing import Any

import pytest

from forge_async_worker import saq_worker
from forge_async_worker.queue import HEARTBEAT, JOB_OPTIONS, RunQueue
from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.run_store import RunStore
from forge_tasks.tasks import JobSpec

from .conftest import START, FakeJob, FakeSaqQueue, Worker, worker_settings
from .toy_tasks import toy_registry


def context(worker: Worker, job: FakeJob | None = None) -> dict[str, Any]:
    """What SAQ hands a job function."""
    return {"job": job or FakeJob(), saq_worker.CONTEXT_KEY: worker.state}


async def test_the_job_functions_run_the_workers_jobs(worker: Worker) -> None:
    run = await worker.start({"do": "approve", "timeout": 60})

    out = await saq_worker.run_adk(context(worker), run_id=run["id"])
    assert out == {"run_id": run["id"], "outcome": "paused"}

    pause = (await worker.get(run["id"]))["pause"]
    worker.clock.advance(61)
    out = await saq_worker.expire_pause(
        context(worker, FakeJob("expire_pause")), run_id=run["id"], pause_id=pause["id"]
    )
    assert out == {"run_id": run["id"], "outcome": "timed_out"}

    assert await saq_worker.maintain(context(worker, FakeJob("maintain"))) == {
        "recovered": [],
        "failed": [],
        "deferred": [],
        "abandoned": [],
        "queued": [],
        "timed_out": [],
    }


async def test_jobs_run_under_the_workers_settings() -> None:
    job = FakeJob()  # as another service enqueues it: SAQ's defaults
    await saq_worker._own_policy({"job": job})
    assert (job.timeout, job.heartbeat, job.retries, job.touched) == (0, HEARTBEAT, 1, 1)
    await saq_worker._own_policy({"job": job})
    assert job.touched == 1  # already right: nothing written

    expiry = FakeJob("expire_pause")
    await saq_worker._own_policy({"job": expiry})
    assert (expiry.timeout, expiry.heartbeat, expiry.retries) == (60, 0, 1)
    other = FakeJob("something_else")
    await saq_worker._own_policy({"job": other})
    assert other.touched == 0


async def test_the_queue_sends_each_job_with_its_settings_and_key() -> None:
    saq, upkeep = FakeSaqQueue(), FakeSaqQueue("adk_workflows_upkeep")
    queue = RunQueue(saq, upkeep=upkeep)

    await queue.run_adk("r1")
    await queue.run_adk("r1", at=START + timedelta(seconds=30.2), key="adk-run:r1:sweep")
    await queue.expire_pause("r1", "p1", at=START + timedelta(hours=1))
    await queue.close()

    (f1, now), (f2, later) = saq.sent
    assert (f1, now.pop("run_id"), now.pop("key")[:11]) == ("run_adk", "r1", "adk-run:r1:")
    assert now == JOB_OPTIONS["run_adk"] and "scheduled" not in now
    assert (later["key"], later["scheduled"]) == ("adk-run:r1:sweep", int(START.timestamp()) + 31)  # never early
    # The upkeep's job goes to its own queue.
    [(f3, expiry)] = upkeep.sent
    assert (f3, expiry["run_id"], expiry["pause_id"]) == ("expire_pause", "r1", "p1")
    assert expiry["scheduled"] == int(START.timestamp()) + 3600 and expiry["timeout"] == 60
    assert queue.name == "adk_workflows" and queue.queues == (saq, upkeep)
    assert (saq.disconnected, upkeep.disconnected) == (1, 1)

    alone = RunQueue(saq)  # without an upkeep queue: everything goes to the one
    await alone.expire_pause("r1", "p1", at=START)
    assert saq.sent[-1][0] == "expire_pause" and alone.queues == (saq,)


async def test_the_queue_keeps_a_runs_failure_a_week() -> None:
    saq = FakeSaqQueue()
    queue = RunQueue(saq)

    assert await queue.kept_failure("r1", 2) is None
    await queue.keep_failure("r1", 2, {"error": {"message": "no"}, "final": True, "epoch": 2})
    assert await queue.kept_failure("r1", 2) == {"error": {"message": "no"}, "final": True, "epoch": 2}
    assert saq.redis.expiries["forge:adk-run-failure:r1:2"] == 7 * 24 * 3600
    # Each taking's own: another's isn't it, nor forgets it.
    assert await queue.kept_failure("r1", 3) is None
    await queue.forget_failure("r1", 3)
    assert await queue.kept_failure("r1", 2) is not None
    await queue.forget_failure("r1", 2)
    assert await queue.kept_failure("r1", 2) is None
    saq.redis.values["forge:adk-run-failure:r2:1"] = "not JSON"
    assert await queue.kept_failure("r2", 1) is None
    # One a worker from before kept for the run is the taking's it names.
    saq.redis.values["forge:adk-run-failure:r3"] = '{"error": {"message": "no"}, "final": true, "epoch": 4}'
    assert (await queue.kept_failure("r3", 4) or {}).get("epoch") == 4
    assert await queue.kept_failure("r3", 5) is None


def test_it_serves_the_enabled_tasks_queues() -> None:
    settings = worker_settings()
    assert saq_worker.serving(settings) == saq_worker.serving(settings, ["adk_workflows"]) == ["adk_workflows"]
    with pytest.raises(ValueError, match="nope"):
        saq_worker.serving(settings, ["nope"])
    with pytest.raises(ValueError, match="documents"):
        saq_worker.serving(settings, ["documents"])  # the documents task isn't enabled
    with pytest.raises(ValueError, match="is enabled"):
        saq_worker.serving(worker_settings(enabled_tasks=[]))

    both = worker_settings(enabled_tasks=["adk_workflows", "documents"])
    assert saq_worker.serving(both) == ["adk_workflows", "documents"]
    assert saq_worker.serving(both, ["documents"]) == ["documents"]


def test_the_job_queue_is_saqs_adk_workflows_queue_with_its_upkeeps() -> None:
    from saq import Queue

    queue = saq_worker.job_queue(worker_settings())
    assert isinstance(queue.queue, Queue) and queue.name == "adk_workflows"
    assert isinstance(queue.upkeep, Queue) and queue.upkeep.name == "adk_workflows_upkeep"
    assert queue.job_options == JOB_OPTIONS
    documents = saq_worker.job_queue(worker_settings(), "documents")
    assert documents.queues == (documents.queue,) and documents.name == "documents"
    assert all(isinstance(each, saq_worker.PatientQueue) for each in (*queue.queues, *documents.queues))


async def test_a_worker_whose_redis_is_away_tries_it_again_a_moment_apart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from saq import Worker as SaqWorker

    monkeypatch.setattr(saq_worker, "DEQUEUE_PAUSE", 0.1)
    monkeypatch.setattr(saq_worker, "DEQUEUE_PAUSE_MAX", 0.2)
    tried: list[float] = []

    class Counted(saq_worker.PatientQueue):
        async def dequeue(self, timeout: float = 0.0, poll_interval: float = 0.0) -> Any:
            tried.append(asyncio.get_running_loop().time())
            return await super().dequeue(timeout, poll_interval)

    queue = Counted.from_url("redis://127.0.0.1:1/0", name="away")  # nothing answers there
    worker = SaqWorker(queue, functions=[saq_worker.run_job], concurrency=4)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)
    with caplog.at_level(logging.WARNING, logger=saq_worker.log.name):
        await saq_worker.run_workers([worker], grace_period=0, stop=stop)
    await queue.redis.aclose()  # SAQ's disconnect asks Redis too

    # Each of its four dequeues at once tries again 0.1 to 0.2 s after it failed:
    # SAQ alone tries again at once, thousands of times a second.
    assert 4 < len(tried) <= 4 * (1.0 / 0.1 + 1)
    said = [record.getMessage() for record in caplog.records if record.name == saq_worker.log.name]
    assert len(said) == 1 and said[0].startswith("queue away can't be read")


async def test_a_queue_whose_redis_answers_again_dequeues_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from saq.queue.redis import RedisQueue

    monkeypatch.setattr(saq_worker, "DEQUEUE_PAUSE", 0.01)
    away = True

    async def dequeue(self: RedisQueue, timeout: float = 0.0, poll_interval: float = 0.0) -> None:
        if away:
            raise ConnectionError("Connection refused")

    async def ping() -> bool:
        await dequeue(queue)
        return True

    monkeypatch.setattr(RedisQueue, "dequeue", dequeue)
    queue = saq_worker.PatientQueue.from_url("redis://127.0.0.1:1/0", name="back")
    monkeypatch.setattr(queue.redis, "ping", ping)

    with caplog.at_level(logging.INFO, logger=saq_worker.log.name):
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await queue.dequeue()
        assert queue.failing_since is not None
        away = False
        assert await queue.dequeue() is None
        assert queue.failing_since is None
        away = True  # away again, later: a new outage
        with pytest.raises(ConnectionError):
            await queue.dequeue()
    await queue.redis.aclose()  # SAQ's disconnect asks Redis too

    said = [record.getMessage().split(" (")[0] for record in caplog.records if record.name == saq_worker.log.name]
    assert said == ["queue back can't be read", "queue back can be read again", "queue back can't be read"]


async def test_runs_and_their_upkeep_have_a_worker_each(worker: Worker) -> None:
    worker.state.queue = saq_worker.job_queue(worker_settings(redis_url="redis://127.0.0.1:1/0"))
    [runs, upkeep] = saq_worker.build_workers(worker.state, concurrency=3)

    # Runs: as many at once as asked, and the upkeep queued here before it had a queue of its own.
    assert runs.queue is worker.state.queue.queue and runs.concurrency == 3
    assert set(runs.functions) == {"run_adk", "expire_pause", "maintain"} and not runs.cron_jobs
    # The upkeep: the maintenance every minute, never behind the runs.
    assert upkeep.queue is worker.state.queue.upkeep and upkeep.concurrency == saq_worker.UPKEEP_CONCURRENCY
    assert set(upkeep.functions) == {"maintain", "expire_pause"}
    [cron] = upkeep.cron_jobs
    assert cron.function is saq_worker.maintain and cron.cron == "* * * * *" and cron.unique
    assert (cron.timeout, cron.retries) == (300, 1)
    for built in (runs, upkeep):
        assert built.context[saq_worker.CONTEXT_KEY] is worker.state
        assert str(built.id).startswith(f"{socket.gethostname()}.") and ":" not in str(built.id)
    assert str(upkeep.id).endswith(".adk_workflows_upkeep")
    await worker.state.queue.close()


async def test_the_documents_queue_runs_task_jobs(worker: Worker) -> None:
    worker.state.settings = worker_settings(enabled_tasks=["adk_workflows", "documents"])
    [built] = saq_worker.build_workers(worker.state, concurrency=2, queues=["documents"])

    assert set(built.functions) == {"run_job"} and not built.cron_jobs
    assert built.queue.name == "documents" and built.concurrency == 2
    assert str(built.id).endswith(".documents") and built.context[saq_worker.CONTEXT_KEY] is worker.state


async def test_run_job_runs_the_spec_and_answers_its_result() -> None:
    from types import SimpleNamespace

    from forge_tasks.runner import ok

    ran: list[JobSpec] = []

    async def run(spec: JobSpec) -> Any:
        ran.append(spec)
        return ok(doc_id="d1", chunk_count=3)

    state = SimpleNamespace(runtime=SimpleNamespace(runner=SimpleNamespace(run=run)))
    ctx = {"job": FakeJob("run_job"), saq_worker.CONTEXT_KEY: state}
    spec = {"task_type": "documents", "kind": "ingest", "payload": {"tenant_id": "kb", "doc_id": "d1"}}

    out = await saq_worker.run_job(ctx, spec=spec)
    assert out["status"] == "ok" and out["detail"] == {"doc_id": "d1", "chunk_count": 3}
    assert [(s.task_type, s.kind) for s in ran] == [("documents", "ingest")]

    bad = await saq_worker.run_job(ctx, spec={"kind": "ingest"})
    assert bad["status"] == "failed" and "invalid job spec" in bad["error"]


async def test_a_worker_without_a_run_store_says_what_to_set() -> None:
    with pytest.raises(ValueError, match="HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL"):
        await saq_worker.start_state(
            worker_settings(),
            registry=toy_registry(),
            queue=RunQueue(FakeSaqQueue()),
            options={"adk_workflows": AdkWorkflowsSettings()},
        )


async def test_an_attempt_takes_as_long_as_the_tasks_settings_allow(store: RunStore) -> None:
    state = await saq_worker.start_state(
        worker_settings(),
        registry=toy_registry(),
        store=store,
        queue=RunQueue(FakeSaqQueue()),
        redis_url=None,
        options={"adk_workflows": AdkWorkflowsSettings(run_timeout=600)},
    )
    try:
        assert state.jobs is not None and state.jobs.attempt_seconds == 1200
    finally:
        await saq_worker.close_state(state)


async def test_a_job_can_hand_no_follow_ups_on(worker: Worker) -> None:
    with pytest.raises(RuntimeError, match="can't queue follow-ups"):
        await worker.state.runtime.queue.enqueue(JobSpec(task_type="adk_workflows", kind="run"))


def test_prepare_runs_every_installed_tasks_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forge_async_worker import cli

    prepared = []

    class Prepared:
        name = queue = "prepared"
        schedules: list[Any] = []

        def prepare(self) -> None:
            prepared.append(self.name)

        def build(self, ctx: Any) -> Any:
            raise AssertionError("prepare builds nothing")

    registry = toy_registry()
    registry.register(Prepared())
    monkeypatch.setattr(cli, "default_registry", lambda: registry)
    cli.main(["prepare"])
    assert prepared == ["prepared"] and "prepared prepared" in capsys.readouterr().out
