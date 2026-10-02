"""The run store on SQLite: what the admin API and the worker do to a run, and
what each status allows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
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
    LostLease,
    NoSuchRun,
    NotAllowed,
    NotOpen,
    RunStore,
    metadata,
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
    assert await store.renew(run["id"], owner="w1", lease_seconds=60)
    assert not await store.renew(run["id"], owner="w2", lease_seconds=60)


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
    with pytest.raises(NotAllowed):
        await store.abandon(run["id"], actor=BOB)
    with pytest.raises(NotAllowed):
        await store.resubmit(run["id"], actor=BOB)
    await store.finish(run["id"], owner="w1", state=None, succeeded=False, error={"message": "x", "category": "error"})
    retried = await store.retry(run["id"], actor=BOB)
    assert retried["status"] == QUEUED and retried["attempt"] == 2 and retried["finished_at"] is None
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)
    again = await store.resubmit(run["id"], actor=BOB)
    assert again["id"] != run["id"] and again["resubmit_of"] == run["id"]
    assert again["session_id"] == "session-1" and again["payload"] == run["payload"]
    assert again["requested_by_name"] == "Bob"
    abandoned = await store.abandon(again["id"], actor=BOB)
    assert abandoned["status"] == ABANDONED and abandoned["finished_at"] is not None
    with pytest.raises(NoSuchRun):
        await store.abandon(run["id"], actor=BOB, organization_id="org-2")


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
