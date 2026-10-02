"""The SAQ side of the worker (saq_worker.py, queue.py): the job functions,
the settings every job runs under, the queue, the worker and its cron.
tests/test_saq_redis.py runs the same code on a real Redis."""

from __future__ import annotations

import socket
from datetime import timedelta
from typing import Any

import pytest

from forge_async_worker import saq_worker
from forge_async_worker.queue import HEARTBEAT, JOB_OPTIONS, RunQueue
from forge_task_adk_workflows.config import AdkWorkflowsSettings
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
    saq = FakeSaqQueue()
    queue = RunQueue(saq)

    await queue.run_adk("r1")
    await queue.run_adk("r1", at=START + timedelta(seconds=30.2), key="adk-run:r1:sweep")
    await queue.expire_pause("r1", "p1", at=START + timedelta(hours=1))
    await queue.close()

    (f1, now), (f2, later), (f3, expiry) = saq.sent
    assert (f1, now.pop("run_id"), now.pop("key")[:11]) == ("run_adk", "r1", "adk-run:r1:")
    assert now == JOB_OPTIONS["run_adk"] and "scheduled" not in now
    assert (later["key"], later["scheduled"]) == ("adk-run:r1:sweep", int(START.timestamp()) + 31)  # never early
    assert (f3, expiry["run_id"], expiry["pause_id"]) == ("expire_pause", "r1", "p1")
    assert expiry["scheduled"] == int(START.timestamp()) + 3600 and expiry["timeout"] == 60
    assert queue.name == "adk_workflows"


def test_it_serves_the_adk_workflows_queue_only() -> None:
    settings = worker_settings()
    assert saq_worker.serving(settings) == saq_worker.serving(settings, ["adk_workflows"]) == ["adk_workflows"]
    with pytest.raises(ValueError, match="nope"):
        saq_worker.serving(settings, ["nope"])
    with pytest.raises(ValueError, match="isn't enabled"):
        saq_worker.serving(worker_settings(enabled_tasks=[]))


def test_the_job_queue_is_saqs_adk_workflows_queue() -> None:
    from saq import Queue

    queue = saq_worker.job_queue(worker_settings())
    assert isinstance(queue.queue, Queue) and queue.name == "adk_workflows"
    assert queue.job_options == JOB_OPTIONS


async def test_the_worker_runs_the_jobs_and_maintains_every_minute(worker: Worker) -> None:
    from saq import Queue

    worker.state.queue = RunQueue(Queue.from_url("redis://127.0.0.1:1/0", name="adk_workflows"))
    [built] = saq_worker.build_workers(worker.state, concurrency=3)

    assert set(built.functions) == {"run_adk", "expire_pause", "maintain"}
    [cron] = built.cron_jobs
    assert cron.function is saq_worker.maintain and cron.cron == "* * * * *" and cron.unique
    assert (cron.timeout, cron.retries) == (300, 1)
    assert built.concurrency == 3 and built.context[saq_worker.CONTEXT_KEY] is worker.state
    assert str(built.id).startswith(f"{socket.gethostname()}.") and ":" not in str(built.id)
    await worker.state.queue.close()


async def test_a_worker_without_a_run_store_says_what_to_set() -> None:
    with pytest.raises(ValueError, match="HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL"):
        await saq_worker.start_state(
            worker_settings(),
            registry=toy_registry(),
            queue=RunQueue(FakeSaqQueue()),
            options={"adk_workflows": AdkWorkflowsSettings()},
        )


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
