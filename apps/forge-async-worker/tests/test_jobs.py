"""What the worker's jobs do to a run (jobs.py): run_adk through each way a
run's job can end (an abandonment too), what became of a run written while the
database hiccups, expire_pause and the maintenance, on a SQLite run store with
the stand-in ADK workflows task."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from forge_async_worker import jobs
from forge_async_worker.jobs import MAX_ATTEMPTS, TIMED_OUT
from forge_async_worker.watchdog import Watchdog
from forge_task_adk_workflows.run_store import (
    ABANDONED,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    WAITING,
    Renewal,
    open_gates,
    runs,
)
from forge_tasks.control import JobControl
from forge_tasks.errors import TransientError

from .conftest import BOB, START, FakeJob, Worker, at
from .toy_tasks import nothing as jobs_nothing

STEP = {"do": "step"}
HOOK = {"do": "hook"}


def hiccup() -> Exception:
    """What the database says when it's gone for a moment."""
    return sa.exc.OperationalError("UPDATE adk_runs", {}, Exception("MySQL server has gone away"))


def kept(worker: Worker, run_id: str, epoch: int = 1) -> Any:
    """The failure the run's ``epoch``-th taking kept in Redis, if any."""
    raw = worker.saq.redis.values.get(f"forge:adk-run-failure:{run_id}:{epoch}")
    return None if raw is None else json.loads(raw)


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


async def test_a_run_waits_at_several_gates_each_decided_on_its_own(worker: Worker) -> None:
    run = await worker.start({"do": "board", "choices": 3, "timeout": 3600})

    assert await worker.run(run["id"]) == "paused"

    first, second, third = open_gates(await worker.get(run["id"]))
    assert [gate["key"] for gate in (first, second, third)] == ["gate-0-0", "gate-0-1", "gate-0-2"]
    # Each gate's expiry, at its deadline.
    assert [(f, kwargs["pause_id"]) for f, kwargs in worker.sent] == [
        ("expire_pause", first["id"]),
        ("expire_pause", second["id"]),
        ("expire_pause", third["id"]),
    ]

    # The last decided: the run carries on, and waits at the others again,
    # under the same IDs, their expiry queued already.
    await worker.store.decide(run["id"], request_id=third["id"], approved=False, comment="", actor=BOB)
    assert await worker.run(run["id"]) == "paused"
    assert [gate["id"] for gate in open_gates(await worker.get(run["id"]))] == [first["id"], second["id"]]
    assert len(worker.sent) == 3

    # The first decided while the run runs (with the second's decision): its
    # pause queues it again instead.
    async def decide_meanwhile(control: JobControl, index: int) -> None:
        await worker.store.decide(run["id"], request_id=first["id"], approved=True, comment="", actor=BOB)

    worker.task.hook = decide_meanwhile
    await worker.store.decide(run["id"], request_id=second["id"], approved=True, comment="", actor=BOB)
    assert await worker.run(run["id"]) == "queued"
    assert (await worker.get(run["id"]))["status"] == QUEUED
    assert worker.sent[-1][0] == "run_adk"

    worker.task.hook = jobs_nothing
    assert await worker.run(run["id"]) == "succeeded"
    said = (await worker.get(run["id"]))["result"]["said"]
    assert said == [{"gate-0-0": True, "gate-0-1": True, "gate-0-2": False}]


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
    # The run store holds it to its backoff too: a job for it sooner (a sweep, a duplicate) takes nothing.
    due = queued["next_attempt_at"]
    assert at(due) <= kwargs["scheduled"] <= at(due) + 1
    assert await worker.run(run["id"]) == "skipped"

    worker.clock.now = due
    assert await worker.run(run["id"]) == "succeeded"
    done = await worker.get(run["id"])
    assert (done["attempt"], done["next_attempt_at"]) == (2, None)
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


async def test_a_run_taken_too_late_to_run_isnt_run(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    claim = worker.store.claim

    async def slow(run_id: str, **kwargs: Any) -> Any:
        taken = await claim(run_id, **kwargs)
        await asyncio.sleep(0.3)  # the database answered late: the lease it gave is all but gone
        return taken

    monkeypatch.setattr(worker.store, "claim", slow)
    run = await worker.start(STEP)

    assert await worker.run(run["id"]) == "dropped"

    assert worker.task.attempts == []  # never started
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert (await worker.state.jobs.maintain())["recovered"] == [run["id"]]


async def test_a_run_whose_lease_ran_out_on_the_run_stores_clock_isnt_run(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = worker.store.claim

    async def suspended(run_id: str, **kwargs: Any) -> Any:
        taken = await claim(run_id, **kwargs)
        worker.clock.advance(jobs.LEASE_SECONDS)  # this process stood still, its own clock with it
        return taken

    monkeypatch.setattr(worker.store, "claim", suspended)
    run = await worker.start(STEP)

    assert await worker.run(run["id"]) == "dropped"

    assert worker.task.attempts == []


async def test_a_job_whose_lease_ran_out_meanwhile_stops_at_its_next_renewal(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    stopped = asyncio.Event()

    async def suspended(control: JobControl, index: int) -> None:
        worker.clock.advance(jobs.LEASE_SECONDS)  # the run store's clock: its lease ran out
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    worker.task.hook = suspended
    run = await worker.start(HOOK)

    # Its renewal is refused: any worker may take the run by now.
    assert await asyncio.wait_for(worker.run(run["id"]), 5) == "dropped"

    assert stopped.is_set()
    worker.clock.advance(1)
    assert (await worker.state.jobs.maintain())["recovered"] == [run["id"]]


async def test_a_long_run_keeps_its_lease_and_its_job_fresh(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    renewed: list[str] = []
    renew = worker.store.renew

    async def counted(run_id: str, **kwargs: Any) -> Renewal:
        renewed.append(run_id)
        return await renew(run_id, **kwargs)

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


async def test_an_attempt_past_its_deadline_is_stopped_and_tried_again_later(worker: Worker) -> None:
    worker.state.jobs.attempt_seconds = 0.2
    stopped = asyncio.Event()

    async def hangs(control: JobControl, index: int) -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()

    worker.task.hook = hangs
    run = await worker.start(STEP, HOOK)

    assert await asyncio.wait_for(worker.run(run["id"]), 5) == "retrying"

    assert stopped.is_set()
    queued = await worker.get(run["id"])
    assert queued["status"] == QUEUED and queued["attempt"] == 2 and queued["lease_owner"] is None
    assert queued["error"]["category"] == "transient"
    assert queued["error"]["message"] == "Its attempt went on for more than 0.2 seconds, so the worker stopped it"
    assert queued["state"] == {"done": 1, "said": []}
    [(function, sent)] = worker.sent
    assert function == "run_adk" and sent["key"].endswith(":retry-2")


def stubborn(busy: asyncio.Event, release: asyncio.Event) -> Any:
    """A hook whose step swallows every cancellation until ``release``."""

    async def hook(control: JobControl, index: int) -> None:
        busy.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # it won't stop

    return hook


def halting(worker: Worker) -> list[tuple[float, str]]:
    """The worker's watchdog, recording when (on the loop's clock) and why it would end the process."""
    halted: list[tuple[float, str]] = []
    worker.state.jobs.watchdog = Watchdog(halt=lambda why: halted.append((time.monotonic(), why)))
    return halted


async def test_a_job_that_wont_stop_holds_its_run_until_the_process_ends(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    monkeypatch.setattr(jobs, "STOP_SECONDS", 0.1)
    worker.state.jobs.attempt_seconds = 0.2
    halted, busy, release = halting(worker), asyncio.Event(), asyncio.Event()
    worker.task.hook = stubborn(busy, release)
    run = await worker.start(HOOK)
    running = asyncio.create_task(worker.run(run["id"]))
    try:
        await busy.wait()
        began = time.monotonic()

        await asyncio.sleep(0.6)  # past its deadline (0.2s), and the time it had to stop (0.1s)

        # Its lease is held all the while: no other worker's maintenance can take the run from under it...
        assert (await worker.state.jobs.maintain())["recovered"] == []
        held = await worker.get(run["id"])
        assert held["status"] == RUNNING and held["lease_until"] == START + timedelta(seconds=jobs.LEASE_SECONDS)
        # ...and the process ends once the time it had to stop is up, before the lease could run out.
        [(when, why)] = halted
        assert 0.25 <= when - began < 0.5
        assert why == f"run {run['id']}'s job didn't stop in time (overdue)"
    finally:
        release.set()
        await asyncio.wait_for(running, 5)


async def test_a_job_that_wont_stop_as_its_lease_runs_out_ends_the_process_before_it_has(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.6)

    async def down(run_id: str, **kwargs: Any) -> Renewal:
        raise hiccup()

    monkeypatch.setattr(worker.store, "renew", down)
    halted, busy, release = halting(worker), asyncio.Event(), asyncio.Event()
    worker.task.hook = stubborn(busy, release)
    run = await worker.start(HOOK)
    began = time.monotonic()
    running = asyncio.create_task(worker.run(run["id"]))
    try:
        await busy.wait()
        await asyncio.sleep(0.8)  # past its lease

        # Stopped a quarter of the lease before its end; ended a twelfth before it, as it didn't stop.
        [(when, why)] = halted
        assert jobs.LEASE_SECONDS * 3 / 4 <= when - began < jobs.LEASE_SECONDS * 11 / 12 + 0.05
        assert why == f"run {run['id']}'s job didn't stop in time (lost)"
    finally:
        release.set()  # the process would have ended: what's left of the attempt doesn't matter
        await asyncio.wait_for(running, 5)


async def test_a_job_holding_the_event_loop_ends_the_process_before_its_lease_runs_out(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    halted = halting(worker)
    held: list[float] = []

    async def blocking(control: JobControl, index: int) -> None:
        held.append(time.monotonic())
        time.sleep(0.6)  # nothing else of the loop runs: no renewal, no stop
        held.append(time.monotonic())

    worker.task.hook = blocking
    run = await worker.start(HOOK)
    began = time.monotonic()

    await asyncio.wait_for(worker.run(run["id"]), 5)

    # The watchdog, a thread of its own, ended the process while the loop was held, a twelfth of the lease before its end.
    [(when, why)] = halted
    assert held[0] < when < held[1] and when - began < jobs.LEASE_SECONDS
    assert why == f"run {run['id']}'s job went on past what it could hold of its lease"


async def test_a_run_cut_off_by_a_stop_is_let_go_only_once_its_job_stopped(worker: Worker) -> None:
    busy, seen = asyncio.Event(), []

    async def slow_to_stop(control: JobControl, index: int) -> None:
        busy.set()
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.2)  # it closes what it opened first
            seen.append((await worker.get(run["id"]))["status"])

    worker.task.hook = slow_to_stop
    run = await worker.start(HOOK)
    running = asyncio.create_task(worker.run(run["id"]))
    await busy.wait()

    running.cancel()  # the worker stops
    with pytest.raises(asyncio.CancelledError):
        await running

    assert seen == [RUNNING]  # not given to another worker while its job went on
    queued = await worker.get(run["id"])
    assert (queued["status"], queued["error"]["category"]) == (QUEUED, "interrupted")


async def test_a_job_that_wont_stop_as_its_worker_stops_isnt_let_go_and_ends_the_process(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "STOP_SECONDS", 0.1)
    halted, busy, release = halting(worker), asyncio.Event(), asyncio.Event()
    worker.task.hook = stubborn(busy, release)
    run = await worker.start(HOOK)
    running = asyncio.create_task(worker.run(run["id"]))
    try:
        await busy.wait()
        running.cancel()  # the worker stops
        await asyncio.sleep(0.3)

        [(_, why)] = halted
        assert why == f"run {run['id']}'s job didn't stop in time (stopped)"
        assert (await worker.get(run["id"]))["status"] == RUNNING  # never let go while it ran
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)


@pytest.mark.parametrize("renewal", ["hangs", "fails"])
async def test_a_run_whose_lease_cant_be_renewed_stops_before_it_runs_out(
    worker: Worker, monkeypatch: pytest.MonkeyPatch, renewal: str
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.6)
    tries: list[str] = []

    async def unrenewed(run_id: str, **kwargs: Any) -> Renewal:
        tries.append(run_id)
        if renewal == "fails":
            raise hiccup()
        await asyncio.Event().wait()
        return Renewal(kept=True)

    monkeypatch.setattr(worker.store, "renew", unrenewed)
    loop, stopped = asyncio.get_running_loop(), []

    async def busy(control: JobControl, index: int) -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.append(loop.time())

    worker.task.hook = busy
    run = await worker.start(HOOK)
    began = loop.time()

    assert await asyncio.wait_for(worker.run(run["id"]), 5) == "dropped"

    # Stopped before its lease ran out, though renewals were tried till then.
    assert stopped and stopped[0] - began < jobs.LEASE_SECONDS and len(tries) >= 2
    # Left as it is: once its lease runs out, the maintenance has it.
    assert (await worker.get(run["id"]))["status"] == RUNNING
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert (await worker.state.jobs.maintain())["recovered"] == [run["id"]]


async def test_a_renewal_that_fails_now_and_then_is_tried_again(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    renew, tries = worker.store.renew, []

    async def flaky(run_id: str, **kwargs: Any) -> Renewal:
        tries.append(run_id)
        if len(tries) % 2:
            raise hiccup()
        return await renew(run_id, **kwargs)

    monkeypatch.setattr(worker.store, "renew", flaky)

    async def slow(control: JobControl, index: int) -> None:
        await asyncio.sleep(0.7)  # past two leases

    worker.task.hook = slow
    run = await worker.start(HOOK)

    assert await worker.run(run["id"]) == "succeeded"
    assert len(tries) >= 4


# ----------------------------------------------------------------------------- run_adk: abandoned while it runs


async def test_a_run_abandoned_while_it_runs_stops_at_its_next_step(worker: Worker) -> None:
    run = await worker.start(STEP, HOOK, STEP)

    async def abandoned(control: JobControl, index: int) -> None:
        await worker.store.abandon(run["id"], actor=BOB)

    worker.task.hook = abandoned

    assert await worker.run(run["id"]) == "abandoned"

    done = await worker.get(run["id"])
    assert (done["status"], done["lease_owner"], done["finished_at"]) == (ABANDONED, None, START)
    assert done["state"] == {"done": 2, "said": []}  # as far as it got
    assert await worker.kinds(run["id"]) == ["created", "started", "note", "cancel_requested", "abandoned"]
    assert worker.sent == [] and await worker.run(run["id"]) == "skipped"


async def test_a_run_abandoned_during_a_long_step_is_stopped_as_its_lease_is_renewed(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    run, told = await worker.start(HOOK, STEP), []

    async def long(control: JobControl, index: int) -> None:
        await worker.store.abandon(run["id"], actor=BOB)
        try:
            await asyncio.sleep(30)
        finally:
            told.append(control.should_stop())

    worker.task.hook = long

    assert await asyncio.wait_for(worker.run(run["id"]), 5) == "abandoned"

    assert told == [True]  # stopped, and told why
    assert (await worker.get(run["id"]))["status"] == ABANDONED
    assert await worker.kinds(run["id"]) == ["created", "started", "cancel_requested", "abandoned"]


@pytest.mark.parametrize(
    ("step", "write"),
    [
        ({"do": "approve", "timeout": 60}, "pause"),
        ({"do": "wait", "until": (START + timedelta(minutes=5)).isoformat()}, "wait"),
        (HOOK, "requeue"),  # a hiccup's retry
    ],
)
async def test_a_run_abandoned_as_it_would_carry_on_later_is_abandoned_instead(
    worker: Worker, monkeypatch: pytest.MonkeyPatch, step: dict[str, Any], write: str
) -> None:
    async def overloaded(control: JobControl, index: int) -> None:
        raise TransientError("Model overloaded")

    worker.task.hook = overloaded
    run, real = await worker.start(step), getattr(worker.store, write)

    async def abandoned_first(run_id: str, **kwargs: Any) -> Any:
        await worker.store.abandon(run_id, actor=BOB)
        return await real(run_id, **kwargs)

    monkeypatch.setattr(worker.store, write, abandoned_first)

    assert await worker.run(run["id"]) == "abandoned"

    done = await worker.get(run["id"])
    assert (done["status"], done["pause"], done["waiting_until"], done["attempt"]) == (ABANDONED, None, None, 1)
    assert worker.sent == []  # nothing carries it on


async def test_a_run_that_ends_before_it_stops_ends_as_it_did(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    run, finish = await worker.start(STEP), worker.store.finish

    async def abandoned_first(run_id: str, **kwargs: Any) -> None:
        await worker.store.abandon(run_id, actor=BOB)
        await finish(run_id, **kwargs)

    monkeypatch.setattr(worker.store, "finish", abandoned_first)

    assert await worker.run(run["id"]) == "succeeded"

    done = await worker.get(run["id"])
    assert (done["status"], done["cancel_requested_at"]) == (SUCCEEDED, None)


async def test_a_run_abandoned_as_its_worker_stops_is_abandoned(worker: Worker) -> None:
    run, busy = await worker.start(HOOK), asyncio.Event()

    async def hangs(control: JobControl, index: int) -> None:
        await worker.store.abandon(run["id"], actor=BOB)
        busy.set()
        await asyncio.sleep(3600)

    worker.task.hook = hangs
    running = asyncio.create_task(worker.run(run["id"]))
    await busy.wait()

    running.cancel()  # the worker stops
    with pytest.raises(asyncio.CancelledError):
        await running

    assert (await worker.get(run["id"]))["status"] == ABANDONED  # not queued again


# ----------------------------------------------------------------------------- run_adk: what became of it, written


async def test_writing_what_became_of_a_run_is_tried_again_while_the_database_hiccups(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "WRITE_PAUSE_SECONDS", 0.01)
    finish, hiccups = worker.store.finish, [hiccup(), hiccup()]

    async def flaky(run_id: str, **kwargs: Any) -> None:
        if hiccups:
            raise hiccups.pop()
        await finish(run_id, **kwargs)

    monkeypatch.setattr(worker.store, "finish", flaky)
    run = await worker.start(STEP)

    assert await worker.run(run["id"]) == "succeeded"
    assert hiccups == [] and (await worker.get(run["id"]))["status"] == SUCCEEDED


async def test_a_failure_the_store_cant_take_is_kept_and_the_maintenance_records_it(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "WRITE_PAUSE_SECONDS", 0.01)
    finish, tries = worker.store.finish, []

    async def down(run_id: str, **kwargs: Any) -> None:
        tries.append(run_id)
        raise hiccup()

    monkeypatch.setattr(worker.store, "finish", down)
    run = await worker.start(STEP, {"do": "fail"})

    with pytest.raises(sa.exc.OperationalError):
        await worker.run(run["id"])

    assert len(tries) == jobs.WRITE_TRIES
    assert kept(worker, run["id"]) == {
        "error": {"message": "Refund failed", "category": "failed", "step": "step-1"},
        "result": {"why": "no"},
        "final": True,
        "epoch": 1,
    }
    monkeypatch.setattr(worker.store, "finish", finish)  # the database is back
    assert (await worker.state.jobs.maintain())["failed"] == []  # its lease hasn't run out

    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    done = await worker.state.jobs.maintain()

    assert (done["failed"], done["recovered"]) == ([run["id"]], [])  # as its worker meant: not run again
    failed = await worker.get(run["id"])
    assert (failed["status"], failed["result"]) == (FAILED, {"why": "no"})
    assert (failed["error"]["message"], failed["error"]["category"]) == ("Refund failed", "failed")
    assert failed["error"]["step"] == "step-1"
    assert kept(worker, run["id"]) is None and worker.sent == []


async def test_a_hiccup_the_store_cant_take_is_kept_and_the_maintenance_queues_the_run_again(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "WRITE_PAUSE_SECONDS", 0.01)

    async def overloaded(control: JobControl, index: int) -> None:
        raise TransientError("Model overloaded")

    async def down(run_id: str, **kwargs: Any) -> Any:
        raise hiccup()

    worker.task.hook = overloaded
    requeue = worker.store.requeue
    monkeypatch.setattr(worker.store, "requeue", down)
    run = await worker.start(HOOK)

    with pytest.raises(sa.exc.OperationalError):
        await worker.run(run["id"])

    assert kept(worker, run["id"])["final"] is False
    monkeypatch.setattr(worker.store, "requeue", requeue)
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert (await worker.state.jobs.maintain())["recovered"] == [run["id"]]

    queued = await worker.get(run["id"])
    assert (queued["status"], queued["attempt"]) == (QUEUED, 2)
    assert (queued["error"]["message"], queued["error"]["category"]) == ("Model overloaded", "transient")
    [*_, recovered] = await worker.store.events(run["id"])
    assert (recovered["kind"], recovered["message"]) == ("recovered", "Model overloaded: queued again")
    assert kept(worker, run["id"]) is None


async def test_a_failure_is_kept_before_it_is_written_so_a_write_that_hangs_loses_nothing(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.3)
    monkeypatch.setattr(jobs, "WRITE_SECONDS", 0.2)
    monkeypatch.setattr(jobs, "WRITE_TRIES", 2)
    monkeypatch.setattr(jobs, "WRITE_PAUSE_SECONDS", 0.01)
    renew, renewals = worker.store.renew, []
    hung: list[Any] = []

    async def counted(run_id: str, **kwargs: Any) -> Renewal:
        renewals.append(time.monotonic())
        return await renew(run_id, **kwargs)

    async def hangs(run_id: str, **kwargs: Any) -> None:
        hung.append((time.monotonic(), kept(worker, run_id)))
        await asyncio.Event().wait()  # the database never answers

    monkeypatch.setattr(worker.store, "renew", counted)
    monkeypatch.setattr(worker.store, "finish", hangs)
    run = await worker.start(STEP, {"do": "fail"})

    with pytest.raises(TimeoutError):  # each try bounded: the attempt doesn't hang with its write
        await asyncio.wait_for(worker.run(run["id"]), 5)

    # Kept for the maintenance before the first try, and its lease held while it was tried.
    failure = {
        "error": {"message": "Refund failed", "category": "failed", "step": "step-1"},
        "result": {"why": "no"},
        "final": True,
        "epoch": 1,
    }
    assert [found for _, found in hung] == [failure, failure]
    assert any(hung[0][0] < when for when in renewals)
    # Once its lease runs out, the maintenance fails it as its worker meant, not as "its worker went".
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert (await worker.state.jobs.maintain())["failed"] == [run["id"]]
    failed = await worker.get(run["id"])
    assert (failed["status"], failed["error"]["message"], failed["result"]) == (FAILED, "Refund failed", {"why": "no"})


async def test_a_failure_written_isnt_kept_for_the_maintenance(worker: Worker) -> None:
    run = await worker.start({"do": "fail"})

    assert await worker.run(run["id"]) == "failed"

    assert kept(worker, run["id"]) is None


async def test_maintenance_waits_while_redis_cant_say_what_a_run_kept(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await worker.start(STEP)
    claimed = await worker.store.claim(run["id"], owner="dead", lease_seconds=60)
    error = {"message": "Refund failed", "category": "failed"}
    # Its worker failed it and kept the failure, but went before writing it.
    epoch = claimed["lease_epoch"]
    await worker.state.queue.keep_failure(run["id"], epoch, {"error": error, "final": True, "epoch": epoch})
    worker.clock.advance(61)
    reads = worker.state.queue.kept_failure

    async def unanswered(run_id: str) -> Any:
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(worker.state.queue, "kept_failure", unanswered)

    done = await worker.state.jobs.maintain()

    # Not queued again: another taking of it would make what its worker kept an earlier taking's, and lost.
    assert (done["deferred"], done["recovered"], done["failed"]) == ([run["id"]], [], [])
    assert (await worker.get(run["id"]))["status"] == RUNNING and kept(worker, run["id"]) is not None
    assert worker.sent == []

    monkeypatch.setattr(worker.state.queue, "kept_failure", reads)  # Redis answers again
    assert (await worker.state.jobs.maintain())["failed"] == [run["id"]]
    assert (await worker.get(run["id"]))["error"]["message"] == "Refund failed"


async def test_maintenance_doesnt_wait_for_redis_that_hangs(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "REDIS_SECONDS", 0.05)
    run = await worker.start(STEP)
    await worker.store.claim(run["id"], owner="dead", lease_seconds=60)
    worker.clock.advance(61)

    async def hangs(run_id: str) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(worker.state.queue, "kept_failure", hangs)

    done = await asyncio.wait_for(worker.state.jobs.maintain(), 5)

    assert done["deferred"] == [run["id"]]


async def test_a_failure_an_earlier_taking_of_the_run_kept_is_no_later_ones(worker: Worker) -> None:
    run = await worker.start(STEP)
    error = {"message": "Refund failed", "category": "failed"}
    # Its first worker failed it, but saw the database go as it wrote so: it kept the failure.
    await worker.store.claim(run["id"], owner="w1", lease_seconds=60)
    await worker.store.finish(run["id"], owner="w1", state=None, succeeded=False, error=error)
    await worker.state.queue.keep_failure(run["id"], 1, {"error": error, "final": True, "epoch": 1})
    # Retried, and taken again, by a worker that dies.
    await worker.store.retry(run["id"], actor=BOB)
    await worker.store.claim(run["id"], owner="dead", lease_seconds=60)
    worker.clock.advance(61)

    done = await worker.state.jobs.maintain()

    assert (done["recovered"], done["failed"]) == ([run["id"]], [])
    assert (await worker.get(run["id"]))["error"]["category"] == "interrupted"


async def test_a_failure_a_worker_from_before_kept_for_the_run_is_its_takings_only(worker: Worker) -> None:
    run = await worker.start(STEP)
    error = {"message": "Refund failed", "category": "failed"}
    claimed = await worker.store.claim(run["id"], owner="old", lease_seconds=60)
    # Kept as workers did before takings kept their own: one key for the run.
    worker.saq.redis.values[f"forge:adk-run-failure:{run['id']}"] = json.dumps(
        {"error": error, "final": True, "epoch": claimed["lease_epoch"]}
    )
    worker.clock.advance(61)

    assert (await worker.state.jobs.maintain())["failed"] == [run["id"]]
    assert (await worker.get(run["id"]))["error"]["message"] == "Refund failed"


async def test_an_earlier_takings_cleanup_never_takes_a_later_takings_failure(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "WRITE_PAUSE_SECONDS", 0.01)
    forget, held_up = worker.state.queue.forget_failure, asyncio.Event()

    async def slow_for_the_first(run_id: str, epoch: int) -> None:
        if epoch == 1:
            await held_up.wait()
        await forget(run_id, epoch)

    monkeypatch.setattr(worker.state.queue, "forget_failure", slow_for_the_first)
    run = await worker.start({"do": "fail"})
    # Its first taking fails it, and writes so; forgetting what it kept first is held up...
    first = asyncio.create_task(worker.run(run["id"]))
    while (await worker.get(run["id"]))["status"] != FAILED:
        await asyncio.sleep(0.01)
    # ...meanwhile someone retries it, and its next taking fails too, the database gone as it writes so.
    await worker.store.retry(run["id"], actor=BOB)
    finish = worker.store.finish

    async def down(run_id: str, **kwargs: Any) -> None:
        raise hiccup()

    monkeypatch.setattr(worker.store, "finish", down)
    with pytest.raises(sa.exc.OperationalError):
        await worker.run(run["id"])
    assert kept(worker, run["id"], epoch=2)["error"]["message"] == "Refund failed"

    held_up.set()  # the first taking's cleanup goes through at last
    assert await asyncio.wait_for(first, 5) == "failed"

    assert kept(worker, run["id"], epoch=2) is not None  # it forgot its own only
    monkeypatch.setattr(worker.store, "finish", finish)  # the database is back
    worker.clock.advance(jobs.LEASE_SECONDS + 1)
    assert (await worker.state.jobs.maintain())["failed"] == [run["id"]]
    failed = await worker.get(run["id"])
    assert (failed["error"]["message"], failed["error"]["category"]) == ("Refund failed", "failed")


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
    assert await worker.state.jobs.maintain() == {
        "recovered": [],
        "failed": [],
        "deferred": [],
        "abandoned": [],
        "queued": [],
        "timed_out": [],
    }

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
    due = (await worker.get(run["id"]))["next_attempt_at"]
    assert START + timedelta(minutes=6) <= due <= START + timedelta(minutes=10)

    # Its job is queued for then: only a while after it, with no worker having taken it, is it stalled.
    worker.clock.now = due + timedelta(seconds=jobs.STALLED_AFTER)
    assert (await worker.state.jobs.maintain())["queued"] == []
    worker.clock.advance(1)
    assert (await worker.state.jobs.maintain())["queued"] == [run["id"]]


async def test_maintenance_expires_overdue_pauses(worker: Worker) -> None:
    run = await worker.start({"do": "approve", "timeout": 60})
    await worker.run(run["id"])
    worker.sent.clear()
    worker.clock.advance(61)

    assert (await worker.state.jobs.maintain())["timed_out"] == [run["id"]]

    assert (await worker.get(run["id"]))["decisions"]["gate-0"]["approved"] is False
    assert [(f, kwargs["run_id"]) for f, kwargs in worker.sent] == [("run_adk", run["id"])]


async def test_maintenance_abandons_runs_whose_worker_went_before_it_stopped_them(worker: Worker) -> None:
    run = await worker.start(STEP)
    await worker.store.claim(run["id"], owner="dead", lease_seconds=60)
    await worker.store.abandon(run["id"], actor=BOB)
    assert (await worker.state.jobs.maintain())["abandoned"] == []  # its worker may yet stop it

    worker.clock.advance(61)
    done = await worker.state.jobs.maintain()

    assert (done["abandoned"], done["recovered"]) == ([run["id"]], [])  # not run again
    assert (await worker.get(run["id"]))["status"] == ABANDONED and worker.sent == []


async def test_one_runs_upkeep_failing_doesnt_hold_up_the_others(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await worker.start(STEP), await worker.start(STEP)
    for run in (first, second):
        await worker.store.claim(run["id"], owner="dead", lease_seconds=60)
    worker.clock.advance(61)
    requeue = worker.store.requeue

    async def broken_for_the_first(run_id: str, **kwargs: Any) -> Any:
        if run_id == first["id"]:
            raise RuntimeError("no")
        return await requeue(run_id, **kwargs)

    monkeypatch.setattr(worker.store, "requeue", broken_for_the_first)

    assert (await worker.state.jobs.maintain())["recovered"] == [second["id"]]
    monkeypatch.setattr(worker.store, "requeue", requeue)
    assert (await worker.state.jobs.maintain())["recovered"] == [first["id"]]  # next time
