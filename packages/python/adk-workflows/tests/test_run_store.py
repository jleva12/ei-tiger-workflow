"""The run store on SQLite: what the admin API and the worker do to a run, and
what each status allows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_task_adk_workflows.run_store import (
    ABANDONED,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    WAITING,
    Actor,
    CancelRequested,
    LostLease,
    NoSuchRun,
    NotAllowed,
    NotOpen,
    RunStore,
    metadata,
    runs,
)

ALICE = Actor("user-alice", "Alice")
BOB = Actor("user-bob", "Bob")
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(clock: Clock) -> AsyncIterator[RunStore]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield RunStore(engine, clock=clock)
    await engine.dispose()


async def started(store: RunStore, *, organization: str = "org-1", agent: str = "ag-1") -> dict:
    return await store.create(
        organization_id=organization,
        agent_id=agent,
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={"document": {"name": "Support desk"}, "input": {"message": "hi"}},
        requested_by=ALICE,
    )


async def test_a_new_run_is_queued_with_its_payload(store: RunStore) -> None:
    run = await started(store)
    assert run["status"] == QUEUED
    assert run["attempt"] == 1
    assert run["payload"]["input"] == {"message": "hi"}
    assert run["requested_by"] == "user-alice"
    assert run["created_at"] == START
    [event] = await store.events(run["id"])
    assert (event["kind"], event["actor_name"]) == ("created", "Alice")


async def test_runs_list_newest_first_without_payloads(store: RunStore, clock: Clock) -> None:
    first = await started(store)
    clock.advance(1)
    second = await started(store, agent="ag-2")
    await started(store, organization="org-2")
    items, total = await store.page("org-1")
    assert [r["id"] for r in items] == [second["id"], first["id"]]
    assert total == 2
    assert "payload" not in items[0]
    only, total = await store.page("org-1", agent_id="ag-1")
    assert [r["id"] for r in only] == [first["id"]] and total == 1
    assert (await store.page("org-1", statuses=[RUNNING]))[1] == 0


async def test_a_run_is_one_workers_at_a_time(store: RunStore) -> None:
    run = await started(store)
    claimed = await store.claim(run["id"], owner="w1", lease_seconds=60)
    assert claimed is not None and claimed["status"] == RUNNING
    assert claimed["lease_owner"] == "w1" and claimed["started_at"] == START
    assert await store.claim(run["id"], owner="w2", lease_seconds=60) is None
    await store.checkpoint(run["id"], owner="w1", state={"step": 1})
    with pytest.raises(LostLease):
        await store.checkpoint(run["id"], owner="w2", state={"step": 2})
    assert (await store.get(run["id"]))["state"] == {"step": 1}
    renewal = await store.renew(run["id"], owner="w1", lease_seconds=60)
    assert renewal and renewal.until == START + timedelta(seconds=60)
    assert not await store.renew(run["id"], owner="w2", lease_seconds=60)


async def test_a_worker_whose_lease_ran_out_cant_write_though_nobody_took_the_run(
    store: RunStore, clock: Clock
) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    clock.advance(60)  # its lease ran out: any worker's maintenance may take the run now

    with pytest.raises(LostLease):
        await store.checkpoint(run["id"], owner="w1", state={"step": 1})
    with pytest.raises(LostLease):
        await store.finish(run["id"], owner="w1", state=None, succeeded=True)
    with pytest.raises(LostLease):
        await store.pause(
            run["id"], owner="w1", state={}, key="gate", kind="approval", reason="?", details={}, deadline=None
        )
    assert not await store.renew(run["id"], owner="w1", lease_seconds=60)  # not brought back to life
    assert await store.requeue(run["id"], owner="w1", error={"message": "A hiccup", "category": "transient"}) is None
    with pytest.raises(NotAllowed, match="isn't running"):
        await child_of(store, run, "c" * 32, owner="w1")
    # As it was: running, w1's lease run out, for the maintenance to find.
    left = await store.get(run["id"])
    assert (left["status"], left["lease_owner"], left["state"]) == (RUNNING, "w1", None)
    clock.advance(1)
    assert [found["id"] for found in await store.interrupted()] == [run["id"]]


async def test_a_paused_run_carries_on_once_decided(store: RunStore) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    pause = await store.pause(
        run["id"],
        owner="w1",
        state={"where": "approval"},
        key="gate-1",
        kind="approval",
        reason="Refund 40 EUR?",
        details={"step_name": "Approve the refund", "approvers": "org:admin"},
        deadline=START + timedelta(hours=1),
    )
    paused = await store.get(run["id"])
    assert paused["status"] == PAUSED and paused["lease_owner"] is None
    assert paused["pause"]["id"] == pause["id"] and paused["pause"]["deadline"] == "2026-10-01T13:00:00+00:00"
    with pytest.raises(NotOpen):
        await store.decide(run["id"], request_id="nope", approved=True, comment="", actor=BOB)
    decided = await store.decide(run["id"], request_id=pause["id"], approved=True, comment="fine", actor=BOB)
    assert decided["status"] == QUEUED and decided["pause"] is None
    assert decided["decisions"]["gate-1"]["approved"] is True
    assert decided["decisions"]["gate-1"]["actor_name"] == "Bob"
    with pytest.raises(NotOpen):
        await store.decide(run["id"], request_id=pause["id"], approved=False, comment="", actor=BOB)
    kinds = [e["kind"] for e in await store.events(run["id"])]
    assert kinds == ["created", "started", "paused", "decided"]


async def test_an_approval_nobody_decided_in_time_is_rejected(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    pause = await store.pause(
        run["id"],
        owner="w1",
        state={},
        key="gate-1",
        kind="approval",
        reason="Ship it?",
        details={},
        deadline=START + timedelta(minutes=5),
    )
    assert await store.overdue() == []
    clock.advance(301)
    assert [r["id"] for r in await store.overdue()] == [run["id"]]
    timed_out = await store.time_out(run["id"], pause_id=pause["id"], comment="Nobody decided within 1 hour(s)")
    assert timed_out is not None and timed_out["status"] == QUEUED
    assert timed_out["decisions"]["gate-1"]["approved"] is False
    assert timed_out["decisions"]["gate-1"]["actor_id"] is None
    assert (await store.events(run["id"]))[-1]["kind"] == "timed_out"
    assert await store.time_out(run["id"], pause_id=pause["id"], comment="again") is None


async def test_a_waiting_run_is_taken_again_when_its_time_comes(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.wait(run["id"], owner="w1", state={"n": 1}, until=START + timedelta(minutes=10), reason="Delay waits")
    waiting = await store.get(run["id"])
    assert waiting["status"] == WAITING and waiting["waiting_until"] == START + timedelta(minutes=10)
    assert await store.claim(run["id"], owner="w2", lease_seconds=60) is None
    clock.advance(600)
    resumed = await store.claim(run["id"], owner="w2", lease_seconds=60)
    assert resumed is not None and resumed["status"] == RUNNING and resumed["waiting_until"] is None
    assert (await store.events(run["id"]))[-1]["kind"] == "resumed"


async def test_a_run_finishes_succeeded_or_failed(store: RunStore) -> None:
    good = await started(store)
    await store.claim(good["id"], owner="w1", lease_seconds=60)
    await store.finish(good["id"], owner="w1", state=None, succeeded=True, result={"answer": 42})
    done = await store.get(good["id"])
    assert done["status"] == SUCCEEDED and done["result"] == {"answer": 42} and done["error"] is None
    bad = await started(store)
    await store.claim(bad["id"], owner="w1", lease_seconds=60)
    await store.finish(
        bad["id"], owner="w1", state={}, succeeded=False, error={"message": "Refund failed", "category": "failed"}
    )
    failed = await store.get(bad["id"])
    assert failed["status"] == FAILED and failed["finished_at"] == START
    assert failed["error"]["message"] == "Refund failed" and failed["error"]["category"] == "failed"


async def test_retry_resubmit_and_abandon(store: RunStore) -> None:
    run = await started(store)
    with pytest.raises(NotAllowed):
        await store.retry(run["id"], actor=BOB)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    # Running, it's marked: its worker stops it.
    stopping = await store.abandon(run["id"], actor=BOB)
    assert stopping["status"] == RUNNING and stopping["cancel_requested_at"] == START
    with pytest.raises(NotAllowed):
        await store.resubmit(run["id"], actor=BOB)
    await store.finish(run["id"], owner="w1", state=None, succeeded=False, error={"message": "x", "category": "error"})
    retried = await store.retry(run["id"], actor=BOB)
    assert retried["status"] == QUEUED and retried["attempt"] == 2 and retried["finished_at"] is None
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)
    again = await store.resubmit(run["id"], actor=BOB)
    assert again["id"] != run["id"] and again["resubmit_of"] == run["id"]
    # In a session of its own.
    assert again["session_id"] != "session-1"
    assert again["payload"] == {**run["payload"], "session_id": again["session_id"]}
    assert again["requested_by_name"] == "Bob"
    abandoned = await store.abandon(again["id"], actor=BOB)
    assert abandoned["status"] == ABANDONED and abandoned["finished_at"] is not None
    with pytest.raises(NoSuchRun):
        await store.abandon(run["id"], actor=BOB, organization_id="org-2")


async def test_a_run_isnt_retried_while_another_of_its_session_goes_on(store: RunStore) -> None:
    # Two runs of one session, as resubmitted runs were before they had sessions of their own.
    run, other = await started(store), await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=False, error={"message": "x", "category": "error"})

    # Two runs waiting at the same step of one session would take each other's answers.
    with pytest.raises(NotAllowed, match=f"Run {other['id'][:8]} shares this run's session and is still going"):
        await store.retry(run["id"], actor=BOB)
    # A resubmission has a session of its own.
    assert (await store.resubmit(run["id"], actor=BOB))["session_id"] != "session-1"

    await store.abandon(other["id"], actor=BOB)
    assert (await store.retry(run["id"], actor=BOB))["attempt"] == 2


async def test_a_resubmitted_run_takes_its_files_from_where_they_were_saved(store: RunStore) -> None:
    files = [{"name": "notes.txt", "media_type": "text/plain", "size_bytes": 4, "type": "text", "version": 0}]
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={"session_id": "session-1", "files": files, "input": {}},
        requested_by=ALICE,
    )
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)

    again = await store.resubmit(run["id"], actor=BOB)
    await store.abandon(again["id"], actor=BOB)
    third = await store.resubmit(again["id"], actor=BOB)

    assert again["payload"]["files_session_id"] == third["payload"]["files_session_id"] == "session-1"
    assert len({run["session_id"], again["session_id"], third["session_id"]}) == 3


async def test_a_running_run_abandoned_is_stopped_by_its_worker(store: RunStore) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.abandon(run["id"], actor=BOB)
    again = await store.abandon(run["id"], actor=ALICE)  # asking again changes nothing
    assert (again["status"], again["cancel_requested_at"]) == (RUNNING, START)
    [*_, asked] = await store.events(run["id"])
    assert (asked["kind"], asked["actor_name"]) == ("cancel_requested", "Bob")

    # Its worker learns it as it renews its lease, and can't carry it on any more.
    renewal = await store.renew(run["id"], owner="w1", lease_seconds=60)
    assert renewal.kept and renewal.stopping
    with pytest.raises(CancelRequested):
        await store.checkpoint(run["id"], owner="w1", state={"done": 1})
    with pytest.raises(CancelRequested):
        await store.pause(
            run["id"], owner="w1", state={}, key="k", kind="approval", reason="Ship?", details={}, deadline=None
        )
    with pytest.raises(CancelRequested):
        await store.wait(run["id"], owner="w1", state={}, until=START, reason="Later")
    with pytest.raises(CancelRequested):
        await store.requeue(run["id"], owner="w1", error={"message": "A hiccup", "category": "transient"})
    with pytest.raises(LostLease):
        await store.checkpoint(run["id"], owner="w2", state={})  # not w2's

    await store.abandon_running(run["id"], owner="w1", state={"done": 1})

    done = await store.get(run["id"])
    assert (done["status"], done["state"], done["lease_owner"]) == (ABANDONED, {"done": 1}, None)
    [*_, abandoned] = await store.events(run["id"])
    assert abandoned["message"] == "Abandoned: its worker stopped it"


async def test_a_run_abandoned_while_it_ran_is_abandoned_once_its_worker_went(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.abandon(run["id"], actor=BOB)
    assert await store.stopping_left() == []  # its worker still has it

    clock.advance(61)

    assert await store.interrupted() == []  # it isn't queued again
    assert [r["id"] for r in await store.stopping_left()] == [run["id"]]
    assert await store.abandon_stopping(run["id"])
    assert (await store.get(run["id"]))["status"] == ABANDONED
    assert not await store.abandon_stopping(run["id"])


async def test_a_run_abandoned_as_it_ended_can_still_be_retried(store: RunStore) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.abandon(run["id"], actor=BOB)
    await store.finish(run["id"], owner="w1", state=None, succeeded=False, error={"message": "x"})
    retried = await store.retry(run["id"], actor=BOB)
    assert retried["cancel_requested_at"] is None
    assert await store.claim(run["id"], owner="w1", lease_seconds=60) is not None


async def child_of(store: RunStore, parent: dict, run_id: str, *, owner: str | None = "w1") -> tuple[dict, bool]:
    """A run ``parent`` starts (a workflow its LLM agent calls as a tool)."""
    return await store.create_child(
        run_id=run_id,
        parent_run_id=parent["id"],
        owner=owner,
        organization_id="org-1",
        agent_id="ag-child",
        agent_name="Refunds",
        revision=0,
        session_id=f"session-{run_id}",
        payload={"input": {"order": "A1"}, "trigger": {"type": "workflow", "run_id": parent["id"]}},
        requested_by=ALICE,
    )


async def test_a_run_starts_a_run_once_under_the_id_its_call_makes(store: RunStore) -> None:
    parent = await started(store)
    with pytest.raises(NotAllowed, match="isn't running"):
        await child_of(store, parent, "c" * 32)  # it isn't running
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    with pytest.raises(NotAllowed):
        await child_of(store, parent, "c" * 32, owner="w2")  # not w2's

    child, created = await child_of(store, parent, "c" * 32)
    again, created_again = await child_of(store, parent, "c" * 32)

    assert (created, created_again) == (True, False)
    assert again["id"] == child["id"] == "c" * 32
    assert (child["status"], child["parent_run_id"]) == (QUEUED, parent["id"])
    [event] = await store.events(child["id"])
    assert event["message"] == f"Started by run {parent['id'][:8]}"
    assert [run["id"] for run in await store.children(parent["id"])] == [child["id"]]
    assert await store.children(child["id"]) == []
    # Another run's ID isn't taken over.
    other = await started(store)
    with pytest.raises(NotAllowed, match="isn't one run"):
        await child_of(store, parent, other["id"])


async def test_a_run_being_abandoned_starts_no_runs(store: RunStore) -> None:
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    child, _ = await child_of(store, parent, "c" * 32)
    await store.abandon(parent["id"], actor=BOB)

    with pytest.raises(NotAllowed, match="being abandoned"):
        await child_of(store, parent, "d" * 32)
    # The one it started is the one it started, whatever became of it.
    assert (await child_of(store, parent, "c" * 32))[0]["id"] == child["id"]


async def test_abandoning_a_run_abandons_the_runs_it_started_still_going(store: RunStore) -> None:
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    queued, _ = await child_of(store, parent, "a" * 32)
    running, _ = await child_of(store, parent, "b" * 32)
    done, _ = await child_of(store, parent, "c" * 32)
    await store.claim(running["id"], owner="w2", lease_seconds=60)
    grandchild, _ = await child_of(store, running, "d" * 32, owner="w2")
    await store.claim(done["id"], owner="w3", lease_seconds=60)
    await store.finish(done["id"], owner="w3", state=None, succeeded=True)

    await store.abandon(parent["id"], actor=BOB)

    status = {run["id"]: (run["status"], run["cancel_requested_at"]) for run in await store.children(parent["id"])}
    assert status == {
        queued["id"]: (ABANDONED, None),
        running["id"]: (RUNNING, START),  # its worker stops it
        done["id"]: (SUCCEEDED, None),  # it ended: it stays as it is
    }
    assert (await store.get(grandchild["id"]))["status"] == ABANDONED  # theirs too
    [*_, abandoned] = await store.events(queued["id"])
    assert (abandoned["message"], abandoned["actor_name"]) == (
        "Abandoned: the run that started it was abandoned",
        "Bob",
    )
    [*_, noted] = await store.events(parent["id"])
    assert noted["message"] == "The runs it started that were still going are abandoned too (3)"


async def asked_to_stop_long_ago(store: RunStore, run_id: str) -> None:
    """Someone abandoned the run while it ran, as an admin API from before
    runs started runs did: what it started goes on."""
    async with store.engine.begin() as conn:
        await conn.execute(sa.update(runs).where(runs.c.id == run_id).values(cancel_requested_at=START))


async def test_what_a_run_started_is_abandoned_as_its_worker_abandons_it(store: RunStore, clock: Clock) -> None:
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    stopped, _ = await child_of(store, parent, "a" * 32)
    await asked_to_stop_long_ago(store, parent["id"])

    await store.abandon_running(parent["id"], owner="w1", state=None)

    assert (await store.get(stopped["id"]))["status"] == ABANDONED

    # Or as the maintenance does, its worker gone.
    other = await started(store)
    await store.claim(other["id"], owner="w1", lease_seconds=60)
    left, _ = await child_of(store, other, "b" * 32)
    await asked_to_stop_long_ago(store, other["id"])
    clock.advance(61)

    assert await store.abandon_stopping(other["id"])

    assert (await store.get(left["id"]))["status"] == ABANDONED


async def test_a_run_queued_again_after_a_hiccup_isnt_taken_before_its_backoff(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    due = START + timedelta(seconds=30)
    hiccup = {"message": "Model overloaded", "category": "transient"}

    queued = await store.requeue(run["id"], owner="w1", error=hiccup, not_before=due)

    assert (queued["status"], queued["next_attempt_at"]) == (QUEUED, due)
    assert await store.claim(run["id"], owner="w2", lease_seconds=60) is None  # a job for it too soon
    # It isn't stalled before its job's time has come, and gone a while.
    clock.advance(30 + 120)
    assert await store.stalled(idle_seconds=120) == []
    clock.advance(1)
    assert [r["id"] for r in await store.stalled(idle_seconds=120)] == [run["id"]]
    taken = await store.claim(run["id"], owner="w2", lease_seconds=60)
    assert taken is not None and taken["next_attempt_at"] is None
    # Interrupted, it's queued again at once.
    clock.advance(61)
    again = await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"})
    assert again is not None and again["next_attempt_at"] is None


async def test_each_taking_of_a_run_counts(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    assert run["lease_epoch"] == 0
    assert (await store.claim(run["id"], owner="w1", lease_seconds=60) or {})["lease_epoch"] == 1
    clock.advance(61)
    await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"})
    assert (await store.claim(run["id"], owner="w2", lease_seconds=60) or {})["lease_epoch"] == 2
    [*_, taken] = await store.events(run["id"])
    assert taken["attributes"] == {"worker": "w2", "epoch": 2}


async def test_a_hiccup_queues_the_run_as_its_next_attempt(store: RunStore) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    queued = await store.requeue(run["id"], owner="w1", error={"message": "Model overloaded", "category": "transient"})
    assert queued is not None and queued["status"] == QUEUED and queued["attempt"] == 2
    assert queued["error"]["category"] == "transient"
    assert await store.requeue(run["id"], owner="w1", error={"message": "again"}) is None


async def test_an_interrupted_run_is_found_and_queued_again(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    assert await store.interrupted() == []
    clock.advance(61)
    assert [r["id"] for r in await store.interrupted()] == [run["id"]]
    queued = await store.requeue(run["id"], owner=None, error={"message": "Its worker went", "category": "interrupted"})
    assert queued is not None and queued["attempt"] == 2
    assert (await store.events(run["id"]))[-1]["kind"] == "recovered"
    await store.claim(run["id"], owner="w2", lease_seconds=60)
    clock.advance(61)
    assert await store.fail_interrupted(
        run["id"], error={"message": "Interrupted too often", "category": "interrupted"}
    )
    assert (await store.get(run["id"]))["status"] == FAILED


async def test_stalled_runs_are_found(store: RunStore, clock: Clock) -> None:
    queued = await started(store)
    waiting = await started(store)
    await store.claim(waiting["id"], owner="w1", lease_seconds=60)
    await store.wait(waiting["id"], owner="w1", state={}, until=START + timedelta(seconds=30), reason="Delay")
    assert await store.stalled(idle_seconds=120) == []
    clock.advance(200)
    assert {r["id"] for r in await store.stalled(idle_seconds=120)} == {queued["id"], waiting["id"]}


async def test_deleting_an_organization_forgets_its_runs(store: RunStore) -> None:
    run = await started(store)
    other = await started(store, organization="org-2")
    assert await store.delete_organization("org-1") == 1
    assert await store.get(run["id"]) is None
    assert await store.events(run["id"]) == []
    assert await store.get(other["id"]) is not None


# ------------------------------------------------------------------ the overview


async def test_runs_started_since_a_time_newest_first(store: RunStore, clock: Clock) -> None:
    first = await started(store)
    clock.advance(3600)
    second = await started(store, agent="ag-2")
    await started(store, organization="org-2")
    found, truncated = await store.started_since("org-1", START)
    assert [run["id"] for run in found] == [second["id"], first["id"]]
    assert not truncated
    assert {"payload", "state"}.isdisjoint(found[0])
    found, truncated = await store.started_since("org-1", START + timedelta(minutes=30))
    assert [run["id"] for run in found] == [second["id"]]
    found, truncated = await store.started_since("org-1", START, limit=1)
    assert [run["id"] for run in found] == [second["id"]] and truncated


async def test_open_runs_are_counted_by_status(store: RunStore) -> None:
    await started(store)
    running = await started(store)
    await store.claim(running["id"], owner="w1", lease_seconds=60)
    done = await started(store)
    await store.claim(done["id"], owner="w1", lease_seconds=60)
    await store.finish(done["id"], owner="w1", state=None, succeeded=True)
    assert await store.open_counts("org-1") == {QUEUED: 1, RUNNING: 1, PAUSED: 0, WAITING: 0}
    assert await store.open_counts("org-2") == {QUEUED: 0, RUNNING: 0, PAUSED: 0, WAITING: 0}


async def test_each_workflows_latest_run(store: RunStore, clock: Clock) -> None:
    await started(store)
    clock.advance(60)
    await started(store, agent="ag-2")
    clock.advance(60)
    await started(store)
    latest = {row["agent_id"]: row for row in await store.latest_by_agent("org-1")}
    assert latest["ag-1"]["last_at"] == START + timedelta(seconds=120)
    assert latest["ag-2"]["agent_name"] == "Support desk"
