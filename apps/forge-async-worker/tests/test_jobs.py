"""What the worker's jobs do to a run (jobs.py): run_adk through each way a
run's job can end, expire_pause and the maintenance, on a SQLite run store
with the stand-in ADK workflows task."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from forge_async_worker import jobs
from forge_async_worker.jobs import MAX_ATTEMPTS, TIMED_OUT
from forge_task_adk_workflows.run_store import FAILED, PAUSED, QUEUED, RUNNING, SUCCEEDED, WAITING, runs
from forge_tasks.control import JobControl
from forge_tasks.errors import TransientError

from .conftest import BOB, START, FakeJob, Worker, at

STEP = {"do": "step"}
HOOK = {"do": "hook"}


async def set_attempt(worker: Worker, run_id: str, attempt: int) -> None:
    async with worker.store.engine.begin() as conn:
        await conn.execute(sa.update(runs).where(runs.c.id == run_id).values(attempt=attempt))


async def steal(worker: Worker, run_id: str) -> None:
    """Another worker takes the run: this one's lease ran out, and the run was recovered."""
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert await worker.store.requeue(run_id, owner=None, error={"message": "gone", "category": "interrupted"})
    assert await worker.store.claim(run_id, owner="w2", lease_seconds=600)


# ----------------------------------------------------------------------------- run_adk: results


async def test_a_run_succeeds_with_its_result(worker: Worker) -> None:
    run = await worker.start(STEP, STEP)

    assert await worker.run(run["id"]) == "succeeded"

    done = await worker.get(run["id"])
    assert done["status"] == SUCCEEDED and done["result"] == {"said": []} and done["error"] is None
    assert done["state"] == {"done": 2, "said": []}
    assert done["lease_owner"] is None and done["finished_at"] == START
    assert await worker.kinds(run["id"]) == ["created", "started", "note", "note", "succeeded"]
    [_, _, note, *_] = await worker.store.events(run["id"])
    assert (note["message"], note["attributes"]) == ("step 0 finished", {"step": 0})
    # Finished: another job for it does nothing.
    assert await worker.run(run["id"]) == "skipped"
    assert worker.task.attempts == [run["id"]]


async def test_a_failed_result_fails_the_run_with_its_message_step_and_result(worker: Worker) -> None:
    run = await worker.start(STEP, {"do": "fail"})

    assert await worker.run(run["id"]) == "failed"

    failed = await worker.get(run["id"])
    assert failed["status"] == FAILED and failed["result"] == {"why": "no"}
    assert failed["error"]["message"] == "Refund failed"
    assert (failed["error"]["category"], failed["error"]["step"]) == ("failed", "step-1")
    assert failed["state"] == {"done": 1, "said": []}


async def test_a_payload_that_doesnt_fit_fails_the_run_unrun(worker: Worker) -> None:
    run = await worker.start(raw={"agent_id": "ag-1"})

    assert await worker.run(run["id"]) == "failed"

    failed = await worker.get(run["id"])
    assert failed["error"]["category"] == "error"
    assert failed["error"]["message"].startswith("The run can't start: its payload isn't valid")
    assert worker.task.attempts == []


async def test_a_run_that_isnt_to_be_run_now_is_left_alone(worker: Worker) -> None:
    assert await worker.run("0" * 32) == "skipped"
    run = await worker.start(STEP)
    await worker.store.claim(run["id"], owner="w2", lease_seconds=60)

    assert await worker.run(run["id"]) == "skipped"

    assert (await worker.get(run["id"]))["lease_owner"] == "w2"
    assert worker.task.attempts == []


async def test_an_unexpected_error_fails_the_run(worker: Worker) -> None:
    async def oops(control: JobControl, index: int) -> None:
        raise RuntimeError("oops")

    worker.task.hook = oops
    run = await worker.start(STEP, HOOK)

    assert await worker.run(run["id"]) == "failed"

    failed = await worker.get(run["id"])
    assert failed["error"]["message"] == "RuntimeError: oops" and failed["error"]["category"] == "error"
    assert failed["state"]["done"] == 1  # what it did before is kept, for a retry


# ----------------------------------------------------------------------------- run_adk: waits


async def test_an_approval_pauses_the_run_until_someone_decides(worker: Worker) -> None:
    run = await worker.start(STEP, {"do": "approve", "timeout": 3600}, STEP)

    assert await worker.run(run["id"]) == "paused"

    paused = await worker.get(run["id"])
    assert paused["status"] == PAUSED and paused["lease_owner"] is None
    pause = paused["pause"]
    assert (pause["key"], pause["kind"], pause["reason"]) == ("gate-1", "approval", "Ship it?")
    assert pause["details"] == {"kind": "approval", "approvers": "org:admin", "step": "step-1"}
    assert pause["deadline"] == (START + timedelta(hours=1)).isoformat()
    assert paused["state"] == {"done": 1, "said": []}
    # Its expiry, at the deadline.
    [(function, kwargs)] = worker.sent
    assert function == "expire_pause"
    assert (kwargs["run_id"], kwargs["pause_id"]) == (run["id"], pause["id"])
    assert kwargs["scheduled"] == at(START + timedelta(hours=1)) and kwargs["key"].startswith("adk-expire:")
    assert (kwargs["timeout"], kwargs["retries"]) == (60, 1)
    # Undecided, a job for it does nothing.
    assert await worker.run(run["id"]) == "skipped"

    await worker.store.decide(run["id"], request_id=pause["id"], approved=True, comment="fine", actor=BOB)
    assert await worker.run(run["id"]) == "succeeded"

    done = await worker.get(run["id"])
    assert done["result"] == {"said": [{"approved": True, "by": "Bob", "comment": "fine"}]}
    assert await worker.kinds(run["id"]) == [
        "created",
        "started",
        "note",
        "paused",
        "decided",
        "resumed",
        "note",
        "note",
        "succeeded",
    ]
    assert worker.task.attempts == [run["id"], run["id"]]


async def test_a_question_pauses_the_run_until_someone_answers(worker: Worker) -> None:
    run = await worker.start({"do": "ask"})

    assert await worker.run(run["id"]) == "paused"

    pause = (await worker.get(run["id"]))["pause"]
    assert pause["kind"] == "human_input" and pause["deadline"] is None
    assert pause["details"]["response_schema"] == {"type": "object"}
    assert worker.sent == []  # no deadline, no expiry

    await worker.store.decide(run["id"], request_id=pause["id"], approved=True, comment='{"count": 3}', actor=BOB)
    assert await worker.run(run["id"]) == "succeeded"
    assert (await worker.get(run["id"]))["result"] == {"said": [{"count": 3}]}


async def test_a_wait_lets_the_worker_go_until_its_time(worker: Worker) -> None:
    until = START + timedelta(minutes=10)
    run = await worker.start(STEP, {"do": "wait", "until": until.isoformat()}, STEP)

    assert await worker.run(run["id"]) == "waiting"

    waiting = await worker.get(run["id"])
    assert waiting["status"] == WAITING and waiting["waiting_until"] == until
    assert waiting["waiting_reason"] == "Delay waits"
    assert waiting["state"] == {"done": 1, "said": [], "waited-1": True}
    [(function, kwargs)] = worker.sent
    assert (function, kwargs["run_id"], kwargs["scheduled"]) == ("run_adk", run["id"], at(until))
    assert kwargs["key"].startswith(f"adk-run:{run['id']}:") and kwargs["timeout"] == 0
    assert await worker.run(run["id"]) == "skipped"  # too early

    worker.clock.advance(600)
    assert await worker.run(run["id"]) == "succeeded"
    assert (await worker.get(run["id"]))["state"]["done"] == 3


async def test_a_job_that_cant_be_queued_is_left_to_the_maintenance(worker: Worker) -> None:
    worker.saq.down = True
    run = await worker.start({"do": "wait", "until": (START + timedelta(minutes=1)).isoformat()})

    assert await worker.run(run["id"]) == "waiting"  # its job wasn't queued, but the run waits

    worker.saq.down = False
    worker.clock.advance(60 + jobs.STALLED_AFTER)
    assert (await worker.state.jobs.maintain())["queued"] == [run["id"]]


# ----------------------------------------------------------------------------- run_adk: hiccups and lost runs


async def test_a_hiccup_queues_the_run_again_with_backoff(worker: Worker) -> None:
    hiccups = [TransientError("Model overloaded")]

    async def flaky(control: JobControl, index: int) -> None:
        if hiccups:
            raise hiccups.pop()

    worker.task.hook = flaky
    run = await worker.start(STEP, HOOK)

    assert await worker.run(run["id"]) == "retrying"

    queued = await worker.get(run["id"])
    assert queued["status"] == QUEUED and queued["attempt"] == 2 and queued["lease_owner"] is None
    assert queued["error"]["message"] == "Model overloaded" and queued["error"]["category"] == "transient"
    assert queued["state"] == {"done": 1, "said": []}
    [(function, kwargs)] = worker.sent
    assert (function, kwargs["key"]) == ("run_adk", f"adk-run:{run['id']}:retry-2")
    assert at(START + timedelta(seconds=22.5)) <= kwargs["scheduled"] <= at(START + timedelta(seconds=38))

    assert await worker.run(run["id"]) == "succeeded"
    assert (await worker.get(run["id"]))["attempt"] == 2
    assert await worker.kinds(run["id"]) == ["created", "started", "note", "retried", "resumed", "note", "succeeded"]


async def test_a_run_gives_up_after_its_last_attempt(worker: Worker) -> None:
    async def overloaded(control: JobControl, index: int) -> None:
        raise TransientError("Model overloaded")

    worker.task.hook = overloaded
    run = await worker.start(HOOK)
    await set_attempt(worker, run["id"], MAX_ATTEMPTS)

    assert await worker.run(run["id"]) == "failed"

    failed = await worker.get(run["id"])
    assert failed["error"]["category"] == "transient"
    assert failed["error"]["message"] == f"Model overloaded (it gave up after {MAX_ATTEMPTS} attempts)"
    assert worker.sent == []


def test_backoff_grows_and_caps() -> None:
    assert 22.5 <= jobs.backoff(0) <= 37.5
    assert jobs.backoff(3) > jobs.backoff(0) * 4
    assert jobs.backoff(20) <= jobs.BACKOFF_MAX * 1.25


async def test_a_worker_that_lost_the_run_leaves_it_to_the_one_that_has_it(worker: Worker) -> None:
    run = await worker.start(HOOK, STEP)

    async def taken(control: JobControl, index: int) -> None:
        await steal(worker, run["id"])

    worker.task.hook = taken

    assert await worker.run(run["id"]) == "dropped"  # its checkpoint found it lost

    now = await worker.get(run["id"])
    assert (now["status"], now["lease_owner"], now["state"]) == (RUNNING, "w2", None)
    assert "succeeded" not in await worker.kinds(run["id"])


async def test_a_lost_lease_stops_the_runs_job(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    run = await worker.start(HOOK, STEP)
    stopped = asyncio.Event()

    async def taken_then_busy(control: JobControl, index: int) -> None:
        await steal(worker, run["id"])
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    worker.task.hook = taken_then_busy

    assert await asyncio.wait_for(worker.run(run["id"]), 5) == "dropped"

    assert stopped.is_set()
    assert (await worker.get(run["id"]))["lease_owner"] == "w2"


async def test_a_long_run_keeps_its_lease_and_its_job_fresh(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    renewed: list[str] = []
    renew = worker.store.renew

    async def counted(run_id: str, **kwargs: object) -> bool:
        renewed.append(run_id)
        return await renew(run_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker.store, "renew", counted)

    async def slow(control: JobControl, index: int) -> None:
        await asyncio.sleep(0.5)

    worker.task.hook = slow
    run, job = await worker.start(HOOK), FakeJob()

    assert await worker.run(run["id"], job) == "succeeded"

    assert len(renewed) >= job.touched >= 2  # renewed every 0.1s, and touched with it
    touched = job.touched
    await asyncio.sleep(0.2)
    assert job.touched == touched  # stopped with the run


async def test_a_run_cut_off_by_a_stop_is_queued_again(worker: Worker) -> None:
    busy = asyncio.Event()

    async def hangs(control: JobControl, index: int) -> None:
        busy.set()
        await asyncio.sleep(3600)

    worker.task.hook = hangs
    run = await worker.start(STEP, HOOK)
    running = asyncio.create_task(worker.run(run["id"]))
    await busy.wait()

    running.cancel()  # the worker stops: SAQ cancels its jobs
    with pytest.raises(asyncio.CancelledError):
        await running

    queued = await worker.get(run["id"])
    assert queued["status"] == QUEUED and queued["attempt"] == 2 and queued["lease_owner"] is None
    assert queued["error"]["category"] == "interrupted"
    assert queued["state"] == {"done": 1, "said": []}


# ----------------------------------------------------------------------------- expire_pause


async def test_an_approval_nobody_decided_by_its_deadline_is_rejected(worker: Worker) -> None:
    run = await worker.start({"do": "approve", "timeout": 60})
    await worker.run(run["id"])
    pause = (await worker.get(run["id"]))["pause"]
    worker.sent.clear()

    # Early (another clock): queued again for the deadline.
    assert (await worker.state.jobs.expire_pause(run["id"], pause["id"]))["outcome"] == "rescheduled"
    [(function, kwargs)] = worker.sent
    assert (function, kwargs["scheduled"]) == ("expire_pause", at(START + timedelta(seconds=60)))

    worker.clock.advance(60)
    assert (await worker.state.jobs.expire_pause(run["id"], pause["id"]))["outcome"] == "timed_out"

    timed_out = await worker.get(run["id"])
    assert timed_out["status"] == QUEUED
    assert timed_out["decisions"]["gate-0"]["approved"] is False
    assert timed_out["decisions"]["gate-0"]["actor_id"] is None
    assert worker.sent[-1][0] == "run_adk" and worker.sent[-1][1]["run_id"] == run["id"]
    assert await worker.run(run["id"]) == "succeeded"
    assert (await worker.get(run["id"]))["result"] == {"said": [{"approved": False, "by": "", "comment": TIMED_OUT}]}
    # Decided already: nothing to do.
    assert (await worker.state.jobs.expire_pause(run["id"], pause["id"]))["outcome"] == "skipped"


async def test_an_approval_decided_in_time_doesnt_expire(worker: Worker) -> None:
    run = await worker.start({"do": "approve", "timeout": 60})
    await worker.run(run["id"])
    pause = (await worker.get(run["id"]))["pause"]
    await worker.store.decide(run["id"], request_id=pause["id"], approved=True, comment="", actor=BOB)

    worker.clock.advance(61)
    assert (await worker.state.jobs.expire_pause(run["id"], pause["id"]))["outcome"] == "skipped"
    assert (await worker.get(run["id"]))["decisions"]["gate-0"]["approved"] is True


# ----------------------------------------------------------------------------- maintain


async def test_maintenance_queues_again_runs_whose_worker_went(worker: Worker) -> None:
    gone = await worker.start(STEP)
    await worker.store.claim(gone["id"], owner="dead", lease_seconds=60)
    too_often = await worker.start(STEP)
    await set_attempt(worker, too_often["id"], MAX_ATTEMPTS)
    await worker.store.claim(too_often["id"], owner="dead", lease_seconds=60)
    assert await worker.state.jobs.maintain() == {"recovered": [], "failed": [], "queued": [], "timed_out": []}

    worker.clock.advance(61)
    done = await worker.state.jobs.maintain()

    assert (done["recovered"], done["failed"]) == ([gone["id"]], [too_often["id"]])
    recovered = await worker.get(gone["id"])
    assert recovered["status"] == QUEUED and recovered["attempt"] == 2
    assert recovered["error"]["category"] == "interrupted"
    assert [(f, kwargs["run_id"]) for f, kwargs in worker.sent] == [("run_adk", gone["id"])]
    failed = await worker.get(too_often["id"])
    assert failed["status"] == FAILED and failed["error"]["category"] == "interrupted"
    assert await worker.run(gone["id"]) == "succeeded"


async def test_maintenance_queues_runs_no_job_took_and_waits_that_are_over(worker: Worker) -> None:
    queued = await worker.start(STEP)
    waiting = await worker.start({"do": "wait", "until": (START + timedelta(seconds=30)).isoformat()})
    await worker.run(waiting["id"])
    worker.sent.clear()
    assert (await worker.state.jobs.maintain())["queued"] == []

    worker.clock.advance(200)
    done = await worker.state.jobs.maintain()

    assert set(done["queued"]) == {queued["id"], waiting["id"]}
    assert {kwargs["key"] for _, kwargs in worker.sent} == {
        f"adk-run:{queued['id']}:sweep",
        f"adk-run:{waiting['id']}:sweep",
    }


async def test_maintenance_leaves_a_run_to_wait_out_its_backoff(worker: Worker) -> None:
    async def overloaded(control: JobControl, index: int) -> None:
        raise TransientError("Model overloaded")

    worker.task.hook = overloaded
    run = await worker.start(HOOK)
    await set_attempt(worker, run["id"], 5)  # its next backoff: 8 minutes (+-25%)
    assert await worker.run(run["id"]) == "retrying"

    worker.clock.advance(300)
    assert (await worker.state.jobs.maintain())["queued"] == []  # stalled, but backing off
    worker.clock.advance(301)
    assert (await worker.state.jobs.maintain())["queued"] == [run["id"]]


async def test_maintenance_expires_overdue_pauses(worker: Worker) -> None:
    run = await worker.start({"do": "approve", "timeout": 60})
    await worker.run(run["id"])
    worker.sent.clear()
    worker.clock.advance(61)

    assert (await worker.state.jobs.maintain())["timed_out"] == [run["id"]]

    assert (await worker.get(run["id"]))["decisions"]["gate-0"]["approved"] is False
    assert [(f, kwargs["run_id"]) for f, kwargs in worker.sent] == [("run_adk", run["id"])]
