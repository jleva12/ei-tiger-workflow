"""The generic SAQ job runs any task type's jobs, each one a tracked run of the
enhanced task framework (toy tasks, in-memory backends, fake SAQ job, queues
and lock). tests/test_saq_redis.py runs the same code on a real Redis. The task
packages test their own jobs."""

from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import AsyncMock

import pytest

from etf import BatchStatus, JobInstance, JobParameters, JobRun
from forge_async_worker import saq_worker
from forge_async_worker.etf_jobs import launch_request
from forge_async_worker.queue import SaqJobQueue
from forge_async_worker.saq_worker import JOB_OPTIONS, MAX_RETRIES, redrive_run, run_job
from forge_tasks.errors import StorageError, TransientError
from forge_tasks.protocols import JobQueue
from forge_tasks.tasks import JobSpec, Schedule

from .conftest import FakeJob, FakeLocks, worker_settings
from .toy_tasks import toy_registry

STORE = {"task_type": "notes", "kind": "store", "payload": {"tenant_id": "t1", "note_id": "n1", "text": "hello"}}


def store(**payload: object) -> dict:
    return {**STORE, "payload": {**STORE["payload"], **payload}}


# ----------------------------------------------------------------------------- run_job


async def test_run_job_runs_a_tracked_job(worker):
    out = await run_job(worker.context(), spec=STORE)
    assert out["status"] == "ok" and out["detail"]["stored"] == 1
    assert worker.runtime.notes.notes == ["hello"]
    assert worker.locks.taken == ["forge-async-worker:lock:notes:t1:n1"] and worker.locks.locks[0].released
    # Tracked: one completed run of notes.store, with the delivery's payload.
    [run] = await worker.runs()
    assert (run.job_name, run.status, run.id) == ("notes.store", BatchStatus.COMPLETED, out["detail"]["run_id"])
    assert run.parameters.get("payload") == STORE["payload"]


async def test_a_permanent_failure_is_acknowledged_and_recorded(worker):
    job = FakeJob()
    out = await run_job(worker.context(job), spec=store(note_id="n2", fail="permanent"))
    assert out["status"] == "failed" and "bad note" in out["error"] and job.retryable  # acknowledged: no retry
    [run] = await worker.runs()
    assert run.id == out["detail"]["run_id"]
    assert run.status is BatchStatus.FAILED and run.failures[-1].attributes["permanent"] is True


async def test_follow_ups_go_to_their_task_types_queue(worker):
    out = await run_job(
        worker.context(), spec={"task_type": "notes", "kind": "split", "payload": {"tenant_id": "t", "text": "a|b"}}
    )
    assert out["status"] == "ok" and out["detail"]["parts"] == 2
    assert worker.queues["alerts"].sent == []
    sent = worker.queues["notes"].sent
    assert [(function, kwargs["spec"]["kind"]) for function, kwargs in sent] == [("run_job", "store")] * 2
    assert all({k: kwargs[k] for k in JOB_OPTIONS} == JOB_OPTIONS and "scheduled" not in kwargs for _, kwargs in sent)


async def test_unknown_job_fails_without_retry(worker):
    job = FakeJob()
    out = await run_job(worker.context(job), spec={"task_type": "notes", "kind": "explode"})
    assert out["status"] == "failed" and "notes.explode" in out["error"] and job.retryable
    out = await run_job(worker.context(), spec={"task_type": "nope", "kind": "x"})
    assert out["status"] == "failed"
    assert await worker.runs() == []


async def test_locked_job_is_requeued_without_spending_retries(worker):
    busy = FakeLocks(free=False)
    job = FakeJob()
    before = time.time()
    out = await run_job(worker.context(job, locks=busy), spec=STORE)
    assert out["status"] == "skipped" and "locked" in out["detail"]["reason"]
    assert not busy.locks[0].released and job.retries == MAX_RETRIES + 1
    ((function, kwargs),) = worker.queues["notes"].sent
    assert function == "run_job" and kwargs["spec"] == JobSpec.model_validate(STORE).wire()
    assert before + saq_worker.LOCK_WAIT - 1 <= kwargs["scheduled"] <= time.time() + saq_worker.LOCK_WAIT
    assert await worker.runs() == []  # nothing ran


async def test_transient_error_retries_with_our_backoff_and_the_retry_continues_the_run(worker, monkeypatch):
    runner = worker.runtime.runner
    real_run = runner.run

    async def boom(spec):
        raise StorageError("primary stepped down")

    monkeypatch.setattr(runner, "run", boom)
    job = FakeJob(attempts=1)
    with pytest.raises(TransientError, match="primary stepped down"):
        await run_job(worker.context(job), spec=STORE)
    assert 22.5 <= job.retry_delay <= 37.5 and job.retry_backoff is False and job.retryable
    assert worker.locks.locks[-1].released
    [first] = await worker.runs()
    assert first.status is BatchStatus.FAILED and first.failures[-1].retryable

    # SAQ's retry of the same delivery restarts its run, as its second attempt.
    monkeypatch.setattr(runner, "run", real_run)
    out = await run_job(worker.context(job.redelivered()), spec=STORE)
    assert out["status"] == "ok"
    runs = await worker.runs()
    assert len(runs) == 2 and {r.instance_id for r in runs} == {first.instance_id}
    second = next(r for r in runs if r.id != first.id)
    assert (second.attempt, second.status, second.id) == (2, BatchStatus.COMPLETED, out["detail"]["run_id"])

    monkeypatch.setattr(runner, "run", boom)
    last = FakeJob(attempts=MAX_RETRIES + 1)  # SAQ fails it: no attempts left
    with pytest.raises(TransientError):
        await run_job(worker.context(last), spec=STORE)
    assert not last.retryable


async def test_a_transient_error_raised_by_the_job_is_retried(worker):
    job = FakeJob()
    with pytest.raises(TransientError, match="try later"):
        await run_job(worker.context(job), spec=store(fail="transient"))
    assert job.retryable and job.retry_backoff is False


async def test_other_errors_fail_without_retry(worker):
    job = FakeJob()
    with pytest.raises(saq_worker.RunFailed, match="oops"):
        await run_job(worker.context(job), spec=store(fail="bug"))
    assert not job.retryable
    [run] = await worker.runs()
    assert run.status is BatchStatus.FAILED and not run.failures[-1].retryable

    # Should SAQ deliver it again anyway, its run is not re-run: an operator restarts it.
    out = await run_job(worker.context(job.redelivered()), spec=store(fail="bug"))
    assert out["status"] == "failed" and "restart" in out["error"]
    assert len(await worker.runs()) == 1

    job = FakeJob()
    with pytest.raises(ValueError):  # not a JobSpec
        await run_job(worker.context(job), spec={"task": "notes"})
    assert not job.retryable


async def test_a_duplicate_delivery_returns_the_first_result_without_running_again(worker):
    job = FakeJob()
    first = await run_job(worker.context(job), spec=STORE)
    again = await run_job(worker.context(job.redelivered()), spec=STORE)
    assert again == first and worker.runtime.notes.notes == ["hello"]
    # Another delivery (another key, or the same key enqueued again) is a run of its own.
    await run_job(worker.context(FakeJob(key=job.key, queued=job.queued + 60)), spec=STORE)
    assert worker.runtime.notes.notes == ["hello", "hello"] and len(await worker.runs()) == 2


async def test_a_run_cut_off_by_a_shutdown_is_restarted_by_its_redelivery(worker, monkeypatch):
    runner = worker.runtime.runner
    real_run = runner.run
    started = asyncio.Event()

    async def hangs(spec):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "run", hangs)
    job = FakeJob()
    running = asyncio.create_task(run_job(worker.context(job), spec=STORE))
    await started.wait()
    running.cancel()  # the worker stops; SAQ re-queues the job
    with pytest.raises(asyncio.CancelledError):
        await running
    [cut] = await worker.runs()
    assert cut.status is BatchStatus.FAILED

    monkeypatch.setattr(runner, "run", real_run)
    out = await run_job(worker.context(job.redelivered()), spec=STORE)
    assert out["status"] == "ok"
    assert {r.attempt for r in await worker.runs()} == {1, 2}


async def test_running_job_keeps_its_heartbeat(worker, monkeypatch):
    monkeypatch.setattr(saq_worker, "TOUCH_EVERY", 0.01)
    real_run = worker.runtime.runner.run

    async def slow(spec):
        await asyncio.sleep(0.1)
        return await real_run(spec)

    monkeypatch.setattr(worker.runtime.runner, "run", slow)
    job = FakeJob()
    await run_job(worker.context(job), spec=STORE)
    touched = job.touched
    assert touched >= 3
    await asyncio.sleep(0.05)
    assert job.touched == touched  # stopped with the job


def test_backoff_grows_and_caps():
    from forge_async_worker.saq_worker import BACKOFF_MAX, backoff

    assert 20 <= backoff(0) <= 40
    assert backoff(3) > backoff(0) * 4
    assert backoff(20) <= BACKOFF_MAX * 1.25


# ----------------------------------------------------------------------------- the task framework's upkeep


async def _orphan(worker, spec: JobSpec) -> JobRun:
    """A run whose worker died mid-job: RUNNING, and nobody holds its lease."""
    config = worker.state.runs.config
    request = launch_request(spec, queue=spec.task_type, key="lost", enqueued=1)
    identity = config.instance_resolver.resolve_identity(request.job_name, request.parameters)
    instance = await config.store.create_job_instance(
        JobInstance(
            id="inst-orphan",
            job_name=request.job_name,
            identity_hash=identity,
            identifying_parameters=dict(request.parameters.identifying),
            status=BatchStatus.RUNNING,
        )
    )
    return await config.store.create_job_run(
        JobRun(
            id="run-orphan",
            instance_id=instance.id,
            job_name=request.job_name,
            parameters=JobParameters(
                identifying=dict(request.parameters.identifying),
                non_identifying=dict(request.parameters.non_identifying),
            ),
            status=BatchStatus.RUNNING,
            current_step="run",
            last_updated=config.clock.now(),
        )
    )


async def test_maintenance_recovers_orphaned_runs_and_queues_their_redrive(worker):
    worker.state.settings.etf.stale_after_seconds = 0
    orphan = await _orphan(worker, JobSpec.model_validate(STORE))
    await asyncio.sleep(0.01)

    out = await saq_worker.maintain(worker.context())
    assert out == {"recovered": [orphan.id], "expired": []}
    ((function, kwargs),) = worker.queues["notes"].sent
    assert (function, kwargs["instance_id"], kwargs["key"]) == (
        "redrive_run",
        orphan.instance_id,
        "redrive:inst-orphan",
    )

    redriven = await redrive_run(worker.context(), instance_id=orphan.instance_id)
    assert redriven["status"] == "ok" and worker.runtime.notes.notes == ["hello"]
    assert worker.locks.taken == ["forge-async-worker:lock:notes:t1:n1"]  # the job's own lock
    latest = await worker.state.runs.store.find_latest_run(orphan.instance_id)
    assert (latest.attempt, latest.status) == (2, BatchStatus.COMPLETED)
    # A second re-drive (a duplicate job) finds nothing to do.
    again = await redrive_run(worker.context(), instance_id=orphan.instance_id)
    assert again["status"] == "skipped"
    assert (await saq_worker.maintain(worker.context())) == {"recovered": [], "expired": []}


# ----------------------------------------------------------------------------- schedules


def test_every_seconds_becomes_clock_aligned_cron():
    def every(seconds: float) -> str:
        return saq_worker.cron_expression(
            Schedule(name="s", job=JobSpec(task_type="t", kind="k"), every_seconds=seconds)
        )

    assert every(5) == "* * * * * */5"
    assert every(900) == "*/15 * * * *"
    assert every(3600) == "0 */1 * * *"
    assert every(7200) == "0 */2 * * *"
    assert every(86400) == "0 0 * * *"
    with pytest.raises(ValueError, match="no cron equivalent"):
        every(420 * 60)
    cron = Schedule(name="c", job=JobSpec(task_type="t", kind="k"), cron="17 3 * * *")
    assert saq_worker.cron_expression(cron) == "17 3 * * *"


def test_schedules_become_cron_jobs_on_their_queue():
    registry = toy_registry()
    enabled = ["notes", "alerts"]
    jobs = saq_worker.cron_jobs(registry, enabled, "notes")
    names = [j.function.__qualname__ for j in jobs]
    assert names == ["schedule:notes.nightly", "schedule:notes.sweep"]  # SAQ keys cron jobs by function name
    by_name = {j.function.__qualname__: j for j in jobs}
    assert by_name["schedule:notes.sweep"].cron == "*/15 * * * *"
    assert by_name["schedule:notes.nightly"].cron == "17 3 * * *"
    assert all(j.unique and j.timeout == 0 and j.retries == MAX_RETRIES + 1 for j in jobs)
    assert saq_worker.cron_jobs(registry, enabled, "alerts") == []
    [upkeep] = saq_worker.cron_jobs(registry, enabled, "alerts", maintenance_cron="*/5 * * * *")
    assert upkeep.function is saq_worker.maintain and upkeep.cron == "*/5 * * * *" and upkeep.unique


async def test_scheduled_function_runs_its_job(worker):
    [nightly, _] = toy_registry().schedules(["notes"])
    fire = saq_worker.scheduled_function(nightly)
    tick = FakeJob(key="cron:schedule:notes.nightly")
    out = await fire(worker.context(tick))
    assert out["status"] == "ok" and out["detail"]["parts"] == 2
    # The next tick is enqueued under the same key, at another time: a run of its own.
    await fire(worker.context(FakeJob(key=tick.key, queued=tick.queued + 86400)))
    assert [r.job_name for r in await worker.runs()] == ["notes.split"] * 2


# ----------------------------------------------------------------------------- queues and workers


async def test_saq_queue_routes_by_task_type():
    notes, alerts = AsyncMock(), AsyncMock()
    q = SaqJobQueue({"notes": notes, "alerts": alerts, "also-notes": notes}, job_options={"retries": 3})
    assert isinstance(q, JobQueue)
    before = time.time()
    await q.enqueue(JobSpec(task_type="notes", kind="split", payload={"a": 1}), countdown=5)
    args, kwargs = notes.enqueue.call_args
    assert args == ("run_job",)
    assert kwargs.pop("spec") == {"task_type": "notes", "kind": "split", "payload": {"a": 1}}
    assert kwargs.pop("retries") == 3 and before + 4 <= kwargs.pop("scheduled") <= time.time() + 5 and not kwargs
    alerts.enqueue.assert_not_awaited()
    with pytest.raises(KeyError, match="missing"):
        await q.enqueue(JobSpec(task_type="missing", kind="x"))
    await q.enqueue_redrive("alerts", "inst-1")
    alerts.enqueue.assert_awaited_once_with("redrive_run", instance_id="inst-1", key="redrive:inst-1", retries=3)
    await q.close()
    notes.disconnect.assert_awaited_once()
    alerts.disconnect.assert_awaited_once()


def test_serving_defaults_to_every_enabled_queue():
    registry = toy_registry()
    settings = worker_settings()
    assert saq_worker.serving(settings, registry, None) == ["alerts", "notes"]
    assert saq_worker.serving(settings, registry, ["notes"]) == ["notes"]
    with pytest.raises(ValueError, match="nope"):
        saq_worker.serving(settings, registry, ["nope"])


def test_job_queue_maps_task_types_to_saq_queues():
    from saq import Queue

    queue = saq_worker.job_queue(worker_settings(), toy_registry())
    assert sorted(queue.queues) == ["alerts", "notes"] and queue.job_options == JOB_OPTIONS
    assert all(isinstance(q, Queue) and q.name == task for task, q in queue.queues.items())


async def test_build_workers_one_per_queue(worker):
    from saq import Queue

    worker.state.queue = SaqJobQueue(
        {name: Queue.from_url("redis://127.0.0.1:1/0", name=name) for name in ("notes", "alerts")}
    )
    workers = saq_worker.build_workers(worker.state, worker.locks, ["notes", "alerts"], concurrency=3)
    assert [w.queue.name for w in workers] == ["notes", "alerts"]
    notes, alerts = workers
    assert set(alerts.functions) == {"run_job", "redrive_run", "restart_run", "resume_run", "decide_run", "maintain"}
    assert set(notes.functions) == {
        "run_job",
        "redrive_run",
        "restart_run",
        "resume_run",
        "decide_run",
        "maintain",
        "schedule:notes.nightly",
        "schedule:notes.sweep",
    }
    for w in workers:
        assert w.concurrency == 3 and w.context[saq_worker.CONTEXT_KEY] is worker.state
        assert w.context["redis"] is worker.locks
        assert str(w.id).startswith(f"{socket.gethostname()}.") and ":" not in str(w.id)
    await worker.state.queue.close()


# ----------------------------------------------------------------------------- jobs other services submit


async def test_jobs_run_under_the_workers_policy():
    job = FakeJob()
    job.timeout, job.heartbeat, job.retries = 10, 0, 1  # SAQ's defaults for a bare enqueue
    await saq_worker._own_policy({"job": job})
    assert (job.timeout, job.heartbeat, job.retries, job.touched) == (0, saq_worker.HEARTBEAT, MAX_RETRIES + 1, 1)
    await saq_worker._own_policy({"job": job})
    assert job.touched == 1  # already right: nothing written


def test_prepare_runs_every_installed_tasks_hook(monkeypatch, capsys):
    from forge_async_worker import cli

    prepared = []

    class Prepared:
        name = queue = "prepared"
        schedules: list = []

        def prepare(self) -> None:
            prepared.append(self.name)

        def build(self, ctx):
            raise AssertionError("prepare builds nothing")

    registry = toy_registry()
    registry.register(Prepared())
    monkeypatch.setattr(cli, "default_registry", lambda: registry)
    cli.main(["prepare"])
    assert prepared == ["prepared"] and "prepared prepared" in capsys.readouterr().out
