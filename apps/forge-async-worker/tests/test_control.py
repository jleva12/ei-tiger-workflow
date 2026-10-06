"""The control a run's job gets on the worker (control.py), over a run in a
SQLite run store."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import exc as db

from forge_async_worker.control import PauseRun, RunControl, WaitRun
from forge_task_adk_workflows.run_store import LostLease, RunStore
from forge_tasks.control import Decision, JobControl
from forge_tasks.errors import TransientError

from .conftest import ALICE, BOB, START, Clock, payload


async def claimed(store: RunStore, owner: str = "w1") -> tuple[dict[str, Any], RunControl]:
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload=payload(),
        requested_by=ALICE,
    )
    taken = await store.claim(run["id"], owner=owner, lease_seconds=60)
    assert taken is not None
    return taken, RunControl(taken, store=store, owner=owner)


async def test_it_is_a_job_control_named_after_its_run(store: RunStore) -> None:
    run, control = await claimed(store)
    assert isinstance(control, JobControl)
    assert control.run_id == control.instance_id == run["id"]
    assert control.should_stop() is False
    assert await control.find_run({"adk_session": "session-1"}) is None


async def test_what_it_keeps_is_a_copy_made_durable_at_a_checkpoint(store: RunStore) -> None:
    run, control = await claimed(store)
    kept = {"steps": [1]}
    control.keep("progress", kept)
    kept["steps"].append(2)
    assert control.load("progress") == {"steps": [1]}
    control.load("progress")["steps"].append(3)
    assert control.load("progress") == {"steps": [1]}
    assert control.load("missing", "default") == "default"
    control.keep("at", START)  # JSON, whatever it's given
    assert control.load("at") == "2026-10-01 12:00:00+00:00"
    assert (await store.get(run["id"]))["state"] is None

    await control.checkpoint("step 1 finished")

    assert (await store.get(run["id"]))["state"] == {"progress": {"steps": [1]}, "at": "2026-10-01 12:00:00+00:00"}
    again = RunControl(await store.get(run["id"]), store=store, owner="w1")  # type: ignore[arg-type]
    assert again.load("progress") == {"steps": [1]}


async def test_a_checkpoint_of_a_run_the_worker_lost_raises(store: RunStore, clock: Clock) -> None:
    run, control = await claimed(store)
    clock.advance(61)
    await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"})
    with pytest.raises(LostLease):
        await control.checkpoint()


async def test_a_checkpoint_the_database_couldnt_take_is_a_hiccup(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, control = await claimed(store)

    async def unreachable(*args: object, **kwargs: object) -> None:
        raise db.OperationalError("UPDATE adk_runs", {}, Exception("Lost connection to MySQL server"))

    monkeypatch.setattr(store, "checkpoint", unreachable)
    with pytest.raises(TransientError, match="OperationalError"):
        await control.checkpoint()

    async def broken(*args: object, **kwargs: object) -> None:
        raise db.IntegrityError("UPDATE adk_runs", {}, Exception("constraint"))

    monkeypatch.setattr(store, "checkpoint", broken)
    with pytest.raises(db.IntegrityError):
        await control.checkpoint()


async def test_notes_go_on_the_runs_activity(store: RunStore) -> None:
    run, control = await claimed(store)
    await control.note("Shape (shape) finished", session_id="session-1")
    await control.note("Plain")
    *_, shaped, plain = await store.events(run["id"])
    assert (shaped["kind"], shaped["message"], shaped["attributes"]) == (
        "note",
        "Shape (shape) finished",
        {"session_id": "session-1"},
    )
    assert plain["attributes"] is None


async def test_an_undecided_approval_pauses_the_run(store: RunStore) -> None:
    _, control = await claimed(store)
    details = {"kind": "approval", "approvers": "org:admin", "step": "review"}

    with pytest.raises(PauseRun) as paused:
        await control.approval(key="gate-1", reason="Ship it?", details=details, timeout_seconds=3600)

    signal = paused.value
    assert (signal.key, signal.kind, signal.reason, signal.details) == ("gate-1", "approval", "Ship it?", details)
    assert signal.deadline == START + timedelta(hours=1)

    with pytest.raises(PauseRun) as asked:
        await control.approval(key="gate-2", reason="How many?", details={"kind": "human_input"})
    assert (asked.value.kind, asked.value.deadline) == ("human_input", None)


async def test_a_decided_approval_answers_at_once(store: RunStore) -> None:
    run, control = await claimed(store)
    pause = await store.pause(
        run["id"],
        owner="w1",
        state={},
        key="gate-1",
        kind="approval",
        reason="Ship it?",
        details={},
        deadline=None,
    )
    await store.decide(run["id"], request_id=pause["id"], approved=True, comment="fine", actor=BOB)
    resumed = await store.claim(run["id"], owner="w2", lease_seconds=60)
    assert resumed is not None
    control = RunControl(resumed, store=store, owner="w2")

    decision = await control.approval(key="gate-1", reason="Ship it?", timeout_seconds=60)

    assert decision == Decision(
        approved=True, comment="fine", actor_id="user-bob", actor_name="Bob", request_id=pause["id"]
    )
    assert await control.approval(key="gate-1", reason="Ship it?") == decision  # every time it's asked
    with pytest.raises(PauseRun):
        await control.approval(key="gate-2", reason="And this?")


async def test_a_wait_ends_the_attempt_until_its_time(store: RunStore) -> None:
    _, control = await claimed(store)

    with pytest.raises(WaitRun) as waiting:
        await control.wait_until(datetime(2026, 10, 1, 12, 10), reason="Delay waits")  # naive: UTC

    assert waiting.value.when == START + timedelta(minutes=10)
    assert waiting.value.reason == "Delay waits"


async def test_a_run_starts_child_runs_queued_with_a_job_each(store: RunStore) -> None:
    queued: list[str] = []

    async def queue(run_id: str) -> None:
        queued.append(run_id)

    run, _ = await claimed(store)
    control = RunControl(run, store=store, owner="w1", queue=queue)
    payload = {
        "tenant_id": "org-1",
        "agent_id": "ag_called00001",
        "name": "Called",
        "session_id": "child-session",
        "run_as": "member-1",
        "run_as_name": "Ada",
        "document": {},
    }

    child = await control.children.start(payload)

    assert queued == [child]
    found = await control.children.get(child)
    assert found is not None
    assert (found["status"], found["agent_id"], found["requested_by"]) == ("queued", "ag_called00001", "member-1")
    assert found["payload"]["session_id"] == "child-session"
