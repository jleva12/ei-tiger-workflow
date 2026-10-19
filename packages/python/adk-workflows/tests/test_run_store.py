"""The run store on SQLite: what the admin API and the worker do to a run, and
what each status allows."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from forge_task_adk_workflows.run_store import (
    ABANDONED,
    CHILD_RUN,
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
    Provenance,
    RunStore,
    RunTaken,
    metadata,
    new_session_id,
    open_gates,
    overdue_gates,
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


@pytest.fixture
async def shared(clock: Clock, tmp_path: Path) -> AsyncIterator[RunStore]:
    """A store whose callers each have a connection of their own, as two
    processes would: their writes race in the database."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield RunStore(engine, clock=clock)
    await engine.dispose()


def gate(
    key: str,
    *,
    kind: str = "approval",
    reason: str = "?",
    details: dict | None = None,
    deadline: datetime | None = None,
) -> dict:
    """A gate as a worker pauses a run at it."""
    return {"key": key, "kind": kind, "reason": reason, "details": details or {}, "deadline": deadline}


async def opened(store: RunStore, run_id: str, *, gates: list[dict], state: dict | None = None, **options) -> list:
    """The run's open gates, once w1 paused it at ``gates``."""
    found = await store.pause(run_id, owner="w1", state=state or {}, gates=gates, **options)
    assert found is not None
    return found


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
        await store.pause(run["id"], owner="w1", state={}, gates=[gate("gate")])
    assert not await store.renew(run["id"], owner="w1", lease_seconds=60)  # not brought back to life
    assert await store.requeue(run["id"], owner="w1", error={"message": "A hiccup", "category": "transient"}) is None
    with pytest.raises(NotAllowed, match="isn't running"):
        await child_of(store, run, "c" * 32, owner="w1")
    # As it was: running, w1's lease run out, for the maintenance to find.
    left = await store.get(run["id"])
    assert (left["status"], left["lease_owner"], left["state"]) == (RUNNING, "w1", None)
    clock.advance(1)
    assert [found["id"] for found in await store.interrupted()] == [run["id"]]


async def test_a_workers_notes_land_only_while_it_has_the_run(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.note(run["id"], "Step 1 finished", owner="w1")
    with pytest.raises(LostLease):
        await store.note(run["id"], "Not w2's to say", owner="w2")

    clock.advance(60)  # its lease ran out, though nobody took the run yet
    with pytest.raises(LostLease):
        await store.note(run["id"], "Step 2 finished", owner="w1")
    clock.advance(1)
    await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"}, lease_epoch=1)
    await store.claim(run["id"], owner="w2", lease_seconds=60)
    with pytest.raises(LostLease):
        await store.note(run["id"], "Step 2 finished", owner="w1")  # on its own clock, it may hold it still
    await store.note(run["id"], "Carries on", owner="w2")
    await store.note(run["id"], "Said by nobody's worker")  # the admin API's: not fenced

    noted = [event["message"] for event in await store.events(run["id"]) if event["kind"] == "note"]
    assert noted == ["Step 1 finished", "Carries on", "Said by nobody's worker"]


async def test_a_worker_holds_its_run_for_a_write_elsewhere_while_it_has_it(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    written: list[str] = []
    async with store.holding(run["id"], owner="w1"):
        written.append("w1")
    with pytest.raises(LostLease):
        async with store.holding(run["id"], owner="w2"):
            written.append("w2")
    clock.advance(60)
    with pytest.raises(LostLease):
        async with store.holding(run["id"], owner="w1"):
            written.append("w1, its lease run out")
    assert written == ["w1"]

    # In a write's own transaction (a usage row, on the same database): it lands with the lease, or not at all.
    clock.advance(1)
    await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"}, lease_epoch=1)
    await store.claim(run["id"], owner="w2", lease_seconds=60)
    async with store.engine.begin() as conn:
        await store.hold(conn, run["id"], owner="w2")
        with pytest.raises(LostLease):
            await store.hold(conn, run["id"], owner="w1")


async def test_a_paused_run_carries_on_once_decided(store: RunStore) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    [pause] = await opened(
        store,
        run["id"],
        state={"where": "approval"},
        gates=[
            gate(
                "gate-1",
                reason="Refund 40 EUR?",
                details={"step_name": "Approve the refund", "approvers": "org:admin"},
                deadline=START + timedelta(hours=1),
            )
        ],
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
    [pause] = await opened(
        store, run["id"], gates=[gate("gate-1", reason="Ship it?", deadline=START + timedelta(minutes=5))]
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


async def test_a_run_waits_at_several_gates_each_decided_on_its_own(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    ship, hold, ask = await opened(
        store,
        run["id"],
        gates=[gate("ship", reason="Ship?"), gate("hold", reason="Hold?"), gate("ask", kind="human_input")],
    )
    paused = await store.get(run["id"])
    assert paused["status"] == PAUSED and paused["pause"]["id"] == ship["id"]
    assert [g["key"] for g in open_gates(paused)] == ["ship", "hold", "ask"]

    # Any of them, in any order: the run is queued with it, the others stay open.
    decided = await store.decide(run["id"], request_id=hold["id"], approved=False, comment="", actor=BOB)
    assert decided["status"] == QUEUED and decided["pause"] is None
    assert [g["key"] for g in open_gates(decided)] == ["ship", "ask"]
    # Decided while it's queued, or running: kept for its next attempt.
    clock.advance(1)
    assert (await store.decide(run["id"], request_id=ask["id"], approved=True, comment="{}", actor=BOB))[
        "status"
    ] == QUEUED
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    with pytest.raises(NotOpen):
        await store.decide(run["id"], request_id=hold["id"], approved=True, comment="", actor=BOB)
    running = await store.decide(run["id"], request_id=ship["id"], approved=True, comment="", actor=BOB)
    assert running["status"] == RUNNING and open_gates(running) == []
    assert set(running["decisions"]) == {"ship", "hold", "ask"}

    # Its attempt read the decisions before ship's was made: pausing at it
    # queues the run again instead, for the next attempt to read it.
    assert await store.pause(run["id"], owner="w1", state={"n": 2}, gates=[gate("ship"), gate("more")]) is None
    again = await store.get(run["id"])
    assert (again["status"], again["state"], again["lease_owner"]) == (QUEUED, {"n": 2}, None)
    [more] = open_gates(again)
    assert more["key"] == "more"


async def test_a_gate_still_open_keeps_its_id(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    first, second = await opened(store, run["id"], gates=[gate("a"), gate("b")])
    await store.decide(run["id"], request_id=first["id"], approved=True, comment="", actor=BOB)
    clock.advance(5)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    still, new = await opened(store, run["id"], gates=[gate("b"), gate("c")])
    assert (still["id"], still["requested_at"]) == (second["id"], second["requested_at"])
    assert new["id"] not in (first["id"], second["id"])
    kinds = [(e["kind"], e["message"]) for e in await store.events(run["id"])][-2:]
    # Only the new one is noted as asked.
    assert kinds == [("resumed", "A worker carries it on"), ("paused", "Waits for a decision: ?")]


async def test_a_run_waiting_for_a_time_at_gates_is_queued_by_a_decision(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    [only] = await opened(store, run["id"], gates=[gate("a")], until=START + timedelta(hours=1), reason="Delay waits")
    waiting = await store.get(run["id"])
    assert (waiting["status"], waiting["pause"], waiting["waiting_reason"]) == (WAITING, None, "Delay waits")
    decided = await store.decide(run["id"], request_id=only["id"], approved=True, comment="", actor=BOB)
    assert (decided["status"], decided["waiting_until"]) == (QUEUED, None)


async def test_each_gate_past_its_deadline_is_overdue(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await opened(
        store,
        run["id"],
        gates=[
            gate("soon", deadline=START + timedelta(minutes=5)),
            gate("never"),
            gate("later", deadline=START + timedelta(hours=1)),
        ],
    )
    clock.advance(301)
    [late] = await store.overdue()
    assert [g["key"] for g in overdue_gates(late, clock())] == ["soon"]


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
    # Its trigger says it's Bob's resubmission (the run had none to keep).
    assert again["payload"] == {
        **run["payload"],
        "session_id": again["session_id"],
        "trigger": {"type": "resubmit", "by": BOB.id, "run_id": run["id"]},
        "agent_access": None,
    }
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


async def test_a_resubmission_never_carries_forward_the_access_its_run_held(store: RunStore) -> None:
    held = {"held": [7], "names": {"7": "refunds:approve"}, "everything": False, "known": True}
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={"session_id": "session-1", "input": {}, "run_as": ALICE.id, "agent_access": held},
        requested_by=ALICE,
    )
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)

    # Nothing said of what whoever it's for holds now: they hold nothing.
    assert (await store.resubmit(run["id"], actor=BOB))["payload"]["agent_access"] is None
    # What they hold now, as the admin API works it out; it acts as the run did.
    now = {**held, "held": []}
    again, created = await store.resubmit_once(
        run["id"], again_id="d" * 32, actor=BOB, organization_id="org-1", agent_access=now
    )
    assert created and again["payload"]["agent_access"] == now and again["payload"]["run_as"] == ALICE.id
    # Asked again for that ID, it's that resubmission, as it was made.
    same, created = await store.resubmit_once(
        run["id"], again_id="d" * 32, actor=BOB, organization_id="org-1", agent_access=held
    )
    assert not created and same["id"] == again["id"] and same["payload"]["agent_access"] == now


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
        await store.pause(run["id"], owner="w1", state={}, gates=[gate("k", reason="Ship?")])
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


def waits_for(child: dict, *, deadline: datetime | None = None) -> dict:
    """The gate a run waits at for the run its workflow's call started."""
    reason = f"Waits for Refunds (run {child['id'][:8]}) to end"
    details = {"child_run_id": child["id"], "step_name": "Helper"}
    return gate(child["id"], kind=CHILD_RUN, reason=reason, details=details, deadline=deadline)


async def waiting_parent(store: RunStore, child_id: str = "c" * 32, **options) -> tuple[dict, dict]:
    """A run, paused by w1 at the gate of a run it started (queued)."""
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    child, _ = await child_of(store, parent, child_id)
    await opened(store, parent["id"], gates=[waits_for(child, **options)])
    return parent, child


async def test_a_run_waiting_for_a_run_it_started_gives_its_worker_back(store: RunStore) -> None:
    parent, child = await waiting_parent(store)

    paused = await store.get(parent["id"])
    assert (paused["status"], paused["lease_owner"], paused["lease_until"]) == (PAUSED, None, None)
    [waiting] = open_gates(paused)
    assert (waiting["kind"], waiting["details"]["child_run_id"]) == (CHILD_RUN, child["id"])
    [*_, said] = await store.events(parent["id"])
    assert (said["kind"], said["message"]) == ("waiting", f"Waits for Refunds (run {child['id'][:8]}) to end")
    # Nobody decides it: the run it waits for ending does.
    with pytest.raises(NotAllowed, match="not for a decision"):
        await store.decide(parent["id"], request_id=waiting["id"], approved=True, comment="", actor=BOB)
    assert (await store.get(parent["id"]))["status"] == PAUSED
    assert await store.claim(parent["id"], owner="w2", lease_seconds=60) is None


@pytest.mark.parametrize("succeeded", [True, False])
async def test_the_end_of_a_run_another_waits_for_queues_that_run(store: RunStore, succeeded: bool) -> None:
    parent, child = await waiting_parent(store)
    [waiting] = open_gates(await store.get(parent["id"]))
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    error = None if succeeded else {"message": "Declined", "category": "failed"}

    await store.finish(child["id"], owner="w2", state=None, succeeded=succeeded, result={"to": "A1"}, error=error)

    ended = SUCCEEDED if succeeded else FAILED
    woken = await store.get(parent["id"])
    assert (woken["status"], open_gates(woken), woken["pause"]) == (QUEUED, [], None)
    decision = woken["decisions"][waiting["key"]]
    assert (decision["approved"], decision["run_id"], decision["status"]) == (True, child["id"], ended)
    assert decision["request_id"] == waiting["id"]
    [*_, said] = await store.events(parent["id"])
    assert (said["kind"], said["message"]) == (
        "child_ended",
        f"Run {child['id'][:8]} it waits for {ended}: it carries on",
    )
    assert said["attributes"] == {"run_id": child["id"]}
    assert await store.claim(parent["id"], owner="w1", lease_seconds=60) is not None


async def test_a_run_another_waits_for_abandoned_queues_that_run(store: RunStore, clock: Clock) -> None:
    # At once, queued.
    parent, child = await waiting_parent(store, "a" * 32)
    await store.abandon(child["id"], actor=BOB)
    woken = await store.get(parent["id"])
    assert woken["status"] == QUEUED
    assert woken["decisions"][child["id"]]["status"] == ABANDONED
    [*_, said] = await store.events(parent["id"])
    assert said["message"] == f"Run {child['id'][:8]} it waits for was abandoned: it carries on"

    # Running: as its worker stops it.
    parent, child = await waiting_parent(store, "b" * 32)
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    await store.abandon(child["id"], actor=BOB)
    assert (await store.get(parent["id"]))["status"] == PAUSED  # it hasn't stopped yet
    await store.abandon_running(child["id"], owner="w2", state=None)
    assert (await store.get(parent["id"]))["status"] == QUEUED

    # Its worker gone before it stopped it: as the maintenance abandons it.
    parent, child = await waiting_parent(store, "d" * 32)
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    await store.abandon(child["id"], actor=BOB)
    clock.advance(61)
    assert await store.abandon_stopping(child["id"])
    assert (await store.get(parent["id"]))["status"] == QUEUED


async def test_a_run_another_waits_for_failing_as_its_worker_went_queues_that_run(
    store: RunStore, clock: Clock
) -> None:
    parent, child = await waiting_parent(store)
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    clock.advance(61)

    assert await store.fail_interrupted(
        child["id"], lease_epoch=1, error={"message": "Its worker went", "category": "interrupted"}
    )

    woken = await store.get(parent["id"])
    assert (woken["status"], woken["decisions"][child["id"]]["status"]) == (QUEUED, FAILED)


async def test_a_run_pausing_for_a_run_that_already_ended_carries_on_at_once(store: RunStore) -> None:
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    child, _ = await child_of(store, parent, "c" * 32)
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    # It ends while the run that started it still runs: nothing waits for it yet.
    await store.finish(child["id"], owner="w2", state=None, succeeded=True)
    assert (await store.get(parent["id"]))["status"] == RUNNING

    assert await store.pause(parent["id"], owner="w1", state={"at": 1}, gates=[waits_for(child)]) is None

    run = await store.get(parent["id"])
    assert (run["status"], run["lease_owner"], open_gates(run), run["state"]) == (QUEUED, None, [], {"at": 1})
    assert run["decisions"][child["id"]]["status"] == SUCCEEDED
    [*_, said] = await store.events(parent["id"])
    assert said["message"] == f"Run {child['id'][:8]} it waits for succeeded: it carries on"

    # One that's gone answers at once too: as the run finds it, gone.
    gone = await started(store)
    await store.claim(gone["id"], owner="w1", lease_seconds=60)
    assert await store.pause(gone["id"], owner="w1", state={}, gates=[waits_for({"id": "e" * 32})]) is None
    assert (await store.get(gone["id"]))["decisions"]["e" * 32]["status"] is None


async def test_a_run_whose_wait_for_another_is_up_carries_on(store: RunStore, clock: Clock) -> None:
    parent, child = await waiting_parent(store, deadline=START + timedelta(seconds=30))
    clock.advance(31)
    [overdue] = await store.overdue()
    [late] = overdue_gates(overdue, clock())
    assert late["details"]["child_run_id"] == child["id"]

    timed_out = await store.time_out(parent["id"], pause_id=late["id"], comment="Nobody decided by its deadline")

    assert timed_out is not None and timed_out["status"] == QUEUED
    assert timed_out["decisions"][child["id"]]["approved"] is False
    [*_, said] = await store.events(parent["id"])
    assert (said["kind"], said["message"]) == (
        "timed_out",
        f"Stopped waiting for run {child['id'][:8]}: it didn't end in time",
    )
    # The run it waited for ending later changes nothing there.
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    await store.finish(child["id"], owner="w2", state=None, succeeded=True)
    after = await store.get(parent["id"])
    assert (after["status"], after["decisions"]) == (QUEUED, timed_out["decisions"])


async def test_abandoning_a_run_waiting_for_another_abandons_that_one(store: RunStore) -> None:
    parent, child = await waiting_parent(store)

    await store.abandon(parent["id"], actor=BOB)

    assert (await store.get(parent["id"]))["status"] == ABANDONED
    assert (await store.get(child["id"]))["status"] == ABANDONED
    assert [e["kind"] for e in await store.events(parent["id"])].count("child_ended") == 0


async def test_a_run_ends_whatever_became_of_the_run_that_waited_for_it(store: RunStore) -> None:
    # The run waiting for it failed meanwhile (another of its ways failed it):
    # it goes on, and its end changes nothing there.
    parent = await started(store)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    child, _ = await child_of(store, parent, "c" * 32)
    [_, approval] = await opened(store, parent["id"], gates=[waits_for(child), gate("review")])
    await store.decide(parent["id"], request_id=approval["id"], approved=True, comment="", actor=BOB)
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    await store.finish(parent["id"], owner="w1", state=None, succeeded=False, error={"message": "A way failed"})
    assert (await store.get(child["id"]))["status"] == QUEUED  # it goes on

    await store.claim(child["id"], owner="w2", lease_seconds=60)
    await store.finish(child["id"], owner="w2", state=None, succeeded=True)

    failed = await store.get(parent["id"])
    assert (failed["status"], list(failed["decisions"])) == (FAILED, ["review"])

    # The run waiting for it is gone (its organization forgotten, say): it ends all the same.
    other = await started(store)
    await store.claim(other["id"], owner="w1", lease_seconds=60)
    orphan, _ = await child_of(store, other, "d" * 32)
    async with store.engine.begin() as conn:
        await conn.execute(sa.delete(runs).where(runs.c.id == other["id"]))
    await store.claim(orphan["id"], owner="w2", lease_seconds=60)
    await store.finish(orphan["id"], owner="w2", state=None, succeeded=True)
    assert (await store.get(orphan["id"]))["status"] == SUCCEEDED


async def test_runs_waiting_for_runs_that_wait_for_runs_carry_on_in_turn(store: RunStore) -> None:
    parent, child = await waiting_parent(store)
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    grandchild, _ = await child_of(store, child, "d" * 32, owner="w2")
    await store.pause(child["id"], owner="w2", state={}, gates=[waits_for(grandchild)])
    await store.claim(grandchild["id"], owner="w3", lease_seconds=60)

    await store.finish(grandchild["id"], owner="w3", state=None, succeeded=True)

    assert (await store.get(child["id"]))["status"] == QUEUED
    assert (await store.get(parent["id"]))["status"] == PAUSED  # its own still goes
    await store.claim(child["id"], owner="w2", lease_seconds=60)
    await store.finish(child["id"], owner="w2", state=None, succeeded=True)
    assert (await store.get(parent["id"]))["status"] == QUEUED


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
    again = await store.requeue(
        run["id"], owner=None, error={"message": "gone", "category": "interrupted"}, lease_epoch=2
    )
    assert again is not None and again["next_attempt_at"] is None


async def test_each_taking_of_a_run_counts(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    assert run["lease_epoch"] == 0
    assert (await store.claim(run["id"], owner="w1", lease_seconds=60) or {})["lease_epoch"] == 1
    clock.advance(61)
    await store.requeue(run["id"], owner=None, error={"message": "gone", "category": "interrupted"}, lease_epoch=1)
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
    queued = await store.requeue(
        run["id"], owner=None, error={"message": "Its worker went", "category": "interrupted"}, lease_epoch=1
    )
    # An interruption, not an attempt: it carries on as the same one.
    assert queued is not None and (queued["attempt"], queued["interruptions"]) == (1, 1)
    assert (await store.events(run["id"]))[-1]["kind"] == "recovered"
    await store.claim(run["id"], owner="w2", lease_seconds=60)
    clock.advance(61)
    assert await store.fail_interrupted(
        run["id"], lease_epoch=2, error={"message": "Interrupted too often", "category": "interrupted"}
    )
    assert (await store.get(run["id"]))["status"] == FAILED


async def test_a_recovery_of_one_taking_of_a_run_leaves_a_later_one_alone(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    clock.advance(61)
    [found] = await store.interrupted()  # as one maintenance found it: its first taking
    gone = {"message": "gone", "category": "interrupted"}
    # Another maintenance recovers it meanwhile; a worker takes it again, and goes too.
    assert await store.requeue(run["id"], owner=None, error=gone, lease_epoch=found["lease_epoch"])
    assert (await store.claim(run["id"], owner="w2", lease_seconds=60) or {})["lease_epoch"] == 2
    clock.advance(61)

    # The first maintenance, from what it found, neither fails the second taking nor queues it.
    kept = {"message": "w1's failure", "category": "failed"}
    assert not await store.fail_interrupted(run["id"], lease_epoch=found["lease_epoch"], error=kept)
    assert await store.requeue(run["id"], owner=None, error=gone, lease_epoch=found["lease_epoch"]) is None
    taken = await store.get(run["id"]) or {}
    assert (taken["status"], taken["lease_owner"], taken["interruptions"]) == (RUNNING, "w2", 1)
    assert taken["error"]["message"] == "gone"
    # Its own recovery does; one that doesn't say which taking it found, nothing.
    with pytest.raises(ValueError, match="lease_epoch"):
        await store.requeue(run["id"], owner=None, error=gone)
    queued = await store.requeue(run["id"], owner=None, error=gone, lease_epoch=2)
    assert queued is not None and (queued["status"], queued["interruptions"]) == (QUEUED, 2)


async def test_a_runs_interruptions_in_a_row_end_as_it_waits_or_is_retried(store: RunStore, clock: Clock) -> None:
    run = await started(store)
    interrupted = {"message": "Its worker stopped while it ran", "category": "interrupted"}
    for owner in ("w1", "w2"):  # let go by a worker that stops, twice
        await store.claim(run["id"], owner=owner, lease_seconds=60)
        await store.requeue(run["id"], owner=owner, error=interrupted)
    await store.claim(run["id"], owner="w3", lease_seconds=60)
    hiccup = {"message": "Model overloaded", "category": "transient"}
    queued = await store.requeue(run["id"], owner="w3", error=hiccup)
    assert queued is not None and (queued["attempt"], queued["interruptions"]) == (2, 2)

    await store.claim(run["id"], owner="w4", lease_seconds=60)
    await store.wait(run["id"], owner="w4", state={}, until=START + timedelta(seconds=30), reason="Delay")
    assert (await store.get(run["id"]) or {})["interruptions"] == 0

    clock.advance(31)
    await store.claim(run["id"], owner="w5", lease_seconds=60)
    await store.requeue(run["id"], owner="w5", error=interrupted)
    await store.claim(run["id"], owner="w6", lease_seconds=60)
    [asked] = await store.pause(run["id"], owner="w6", state={}, gates=[gate("ship")]) or []
    assert (await store.get(run["id"]) or {})["interruptions"] == 0

    await store.decide(run["id"], request_id=asked["id"], approved=True, comment="", actor=BOB)
    await store.claim(run["id"], owner="w7", lease_seconds=60)
    await store.requeue(run["id"], owner="w7", error=interrupted)
    await store.claim(run["id"], owner="w8", lease_seconds=60)
    clock.advance(61)
    await store.fail_interrupted(run["id"], lease_epoch=8, error=interrupted)
    assert (await store.get(run["id"]) or {})["interruptions"] == 1
    retried = await store.retry(run["id"], actor=BOB)
    assert (retried["attempt"], retried["interruptions"]) == (3, 0)


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


# ------------------------------------------------------------------ provenance

CONSOLE_START = Provenance(
    "builder_test",
    channel="console",
    on_behalf_of=ALICE.id,
    on_behalf_of_name=ALICE.name,
    on_behalf_of_verified=True,
    auth_method="jwt",
    client_ip="203.0.113.7",
    user_agent="Mozilla/5.0",
    request_id="req-1",
)


async def test_a_run_keeps_how_it_was_started_and_is_its_own_root(store: RunStore) -> None:
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={"trigger": {"type": "manual", "by": ALICE.id}},
        requested_by=ALICE,
        provenance=CONSOLE_START,
    )
    assert Provenance.of(run) == CONSOLE_START
    assert run["root_run_id"] == run["id"]
    [created] = await store.events(run["id"])
    assert created["attributes"] == {
        "trigger_type": "builder_test",
        "channel": "console",
        "on_behalf_of": ALICE.id,
        "on_behalf_of_verified": True,
    }
    # One said nothing of it: it's unknown, but it's still its own root.
    unsaid = await started(store)
    assert Provenance.of(unsaid) is None and unsaid["root_run_id"] == unsaid["id"]


async def test_what_a_run_keeps_of_its_start_is_cut_to_its_columns(store: RunStore) -> None:
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={},
        requested_by=ALICE,
        provenance=Provenance("chat_agent", on_behalf_of="v" * 300, on_behalf_of_name="", user_agent="u" * 900),
    )
    assert (len(run["on_behalf_of"]), len(run["user_agent"])) == (255, 500)
    assert run["on_behalf_of_name"] is None


async def test_a_run_another_starts_is_for_whom_its_starter_is_under_its_root(store: RunStore) -> None:
    parent = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={},
        requested_by=Actor("apikey:k-1", "API key Billing"),
        version="4",
        provenance=Provenance(
            "chat_agent",
            channel="api",
            on_behalf_of="end-user-9",
            on_behalf_of_verified=False,
            via_agent_id="ca_desk",
            via_agent_version="2",
            api_key_id="k-1",
            auth_method="api_key",
            client_ip="198.51.100.1",
        ),
    )
    await store.claim(parent["id"], owner="w1", lease_seconds=60)
    child, _ = await child_of(store, parent, "c" * 32)
    await store.claim(child["id"], owner="w1", lease_seconds=60)
    grandchild, _ = await child_of(store, child, "g" * 32)

    assert Provenance.of(child) == Provenance(
        "workflow",
        channel="worker",
        on_behalf_of="end-user-9",
        on_behalf_of_verified=False,
        via_agent_id="ag-1",
        via_agent_version="4",
        api_key_id="k-1",
        auth_method="api_key",
    )
    # Its request was its starter's, not its own.
    assert child["client_ip"] is None
    assert (child["root_run_id"], grandchild["root_run_id"]) == (parent["id"], parent["id"])
    assert grandchild["via_agent_id"] == "ag-child" and grandchild["on_behalf_of"] == "end-user-9"
    under, total = await store.page("org-1", root_run_id=parent["id"])
    assert total == 3 and {r["id"] for r in under} == {parent["id"], child["id"], grandchild["id"]}


async def test_a_resubmission_is_its_resubmitters_and_keeps_the_first_trigger(store: RunStore) -> None:
    run = await store.create(
        organization_id="org-1",
        agent_id="ag-1",
        agent_name="Support desk",
        revision=3,
        session_id="session-1",
        payload={
            "trigger": {"type": "chat_agent", "agent_id": "ca_desk", "user": "u-1"},
            "run_as": "chat-agent:ca_desk",
        },
        requested_by=Actor("chat-agent:ca_desk", "Chat agent Desk"),
        provenance=Provenance("chat_agent", on_behalf_of="u-1", via_agent_id="ca_desk"),
    )
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)
    # Whatever the route says it is, it's a resubmission.
    again = await store.resubmit(
        run["id"],
        actor=BOB,
        provenance=Provenance("manual", channel="console", on_behalf_of=BOB.id, on_behalf_of_name="Bob"),
    )
    assert Provenance.of(again) == Provenance(
        "resubmit", channel="console", on_behalf_of=BOB.id, on_behalf_of_name="Bob"
    )
    assert again["root_run_id"] == again["id"]
    assert again["payload"]["trigger"] == {"type": "resubmit", "by": BOB.id, "run_id": run["id"]}
    assert again["payload"]["original_trigger"] == run["payload"]["trigger"]
    # It acts as the run did, whose files it reads.
    assert again["payload"]["run_as"] == "chat-agent:ca_desk"

    # Again: the first trigger stays the original; said nothing of, it's the resubmitter's.
    await store.claim(again["id"], owner="w1", lease_seconds=60)
    await store.finish(again["id"], owner="w1", state=None, succeeded=True)
    third = await store.resubmit(again["id"], actor=ALICE)
    assert third["payload"]["original_trigger"] == run["payload"]["trigger"]
    assert (third["trigger_type"], third["on_behalf_of"], third["on_behalf_of_name"]) == (
        "resubmit",
        ALICE.id,
        "Alice",
    )


async def test_runs_are_listed_by_how_they_were_started(store: RunStore, clock: Clock) -> None:
    async def run_of(provenance: Provenance) -> str:
        clock.advance(60)
        found = await store.create(
            organization_id="org-1",
            agent_id="ag-1",
            agent_name="Support desk",
            revision=3,
            session_id=new_session_id(),
            payload={},
            requested_by=ALICE,
            provenance=provenance,
        )
        return str(found["id"])

    manual = await run_of(Provenance("manual", on_behalf_of=ALICE.id))
    keyed = await run_of(Provenance("api_key", on_behalf_of="apikey:k-1", api_key_id="k-1"))
    chat = await run_of(Provenance("chat_agent", on_behalf_of=BOB.id, via_agent_id="ca_desk"))
    intake = await run_of(Provenance("intake", on_behalf_of=BOB.id, via_agent_id="ca_desk"))

    async def ids(**filters: object) -> list[str]:
        found, total = await store.page("org-1", **filters)  # type: ignore[arg-type]
        assert total == len(found)
        return [run["id"] for run in found]

    assert await ids(trigger_types=["chat_agent", "intake"]) == [intake, chat]
    assert await ids(on_behalf_of=BOB.id) == [intake, chat]
    assert await ids(via_agent_id="ca_desk", trigger_types=["intake"]) == [intake]
    assert await ids(api_key_id="k-1") == [keyed]
    # From the second run's minute to the fourth's, the fourth not included.
    assert await ids(created_from=START + timedelta(minutes=2), created_to=START + timedelta(minutes=4)) == [
        chat,
        keyed,
    ]
    assert await ids(trigger_types=["a2a"]) == []
    assert manual in await ids()
    assert await store.page("org-2", trigger_types=["manual"]) == ([], 0)


async def test_a_refused_start_is_kept_for_the_organizations_audit(store: RunStore, clock: Clock) -> None:
    first = await store.refuse(
        organization_id="org-1",
        agent_id="ag-1",
        version="draft",
        status_code=403,
        reason="Requires agents:run",
        requested_by=BOB,
        provenance=Provenance("manual", channel="console", on_behalf_of=BOB.id, auth_method="jwt"),
    )
    clock.advance(5)
    second = await store.refuse(
        organization_id="org-1",
        agent_id="ag-2",
        status_code=422,
        reason="x" * 2000,
        requested_by=None,
        provenance=Provenance("runtime_rest", channel="api", on_behalf_of="anonymous", auth_method="anonymous"),
    )
    found, total = await store.refused("org-1")
    assert total == 2 and [r["id"] for r in found] == [second, first]
    newest, oldest = found
    assert (oldest["status_code"], oldest["reason"], oldest["requested_by_name"]) == (403, "Requires agents:run", "Bob")
    assert oldest["version"] == "draft" and oldest["at"] == START
    assert Provenance.of(oldest) == Provenance("manual", channel="console", on_behalf_of=BOB.id, auth_method="jwt")
    assert len(newest["reason"]) == 1000 and newest["requested_by"] is None
    assert [r["id"] for r in (await store.refused("org-1", trigger_types=["manual"]))[0]] == [first]
    assert [r["id"] for r in (await store.refused("org-1", agent_id="ag-2"))[0]] == [second]
    assert (await store.refused("org-1", created_from=START + timedelta(seconds=1)))[1] == 1
    assert await store.refused("org-2") == ([], 0)

    # They go with the organization.
    await store.delete_organization("org-1")
    assert await store.refused("org-1") == ([], 0)


async def once(store: RunStore, run_id: str, *, organization: str = "org-1", agent: str = "ag-1", by: Actor = ALICE):
    return await store.create_once(
        run_id=run_id,
        organization_id=organization,
        agent_id=agent,
        agent_name="Support desk",
        revision=3,
        session_id=new_session_id(),
        payload={"document": {"name": "Support desk"}, "input": {"message": "hi"}},
        requested_by=by,
    )


async def test_a_start_retried_under_its_id_is_the_run_it_made(store: RunStore) -> None:
    run, created = await once(store, "a" * 32)
    assert created and run["id"] == "a" * 32 and run["status"] == QUEUED
    again, created = await once(store, "a" * 32)
    assert not created and again["id"] == run["id"] and again["session_id"] == run["session_id"]
    items, total = await store.page("org-1")
    assert total == 1


async def test_starts_racing_for_one_id_make_one_run(shared: RunStore) -> None:
    made = await asyncio.gather(*(once(shared, "b" * 32) for _ in range(5)))
    assert sum(created for _, created in made) == 1
    assert {run["id"] for run, _ in made} == {"b" * 32}
    assert (await shared.page("org-1"))[1] == 1


async def test_an_id_another_start_made_isnt_handed_on(store: RunStore) -> None:
    await once(store, "c" * 32)
    # Someone else, another workflow, another organization: never that run.
    with pytest.raises(RunTaken):
        await once(store, "c" * 32, by=BOB)
    with pytest.raises(RunTaken):
        await once(store, "c" * 32, agent="ag-2")
    with pytest.raises(RunTaken):
        await once(store, "c" * 32, organization="org-2")


async def test_a_resubmission_retried_under_its_id_is_the_one_it_made(shared: RunStore) -> None:
    store = shared
    run = await started(store)
    await store.claim(run["id"], owner="w1", lease_seconds=60)
    await store.finish(run["id"], owner="w1", state=None, succeeded=True)
    again, created = await store.resubmit_once(run["id"], again_id="d" * 32, actor=BOB, organization_id="org-1")
    assert created and again["id"] == "d" * 32 and again["resubmit_of"] == run["id"]
    retried, created = await store.resubmit_once(run["id"], again_id="d" * 32, actor=BOB, organization_id="org-1")
    assert not created and retried["id"] == again["id"]
    # Racing asks make one.
    made = await asyncio.gather(
        *(store.resubmit_once(run["id"], again_id="e" * 32, actor=BOB, organization_id="org-1") for _ in range(4))
    )
    assert sum(created for _, created in made) == 1
    # The ID of Bob's resubmission isn't Alice's, nor a resubmission of another run.
    with pytest.raises(RunTaken):
        await store.resubmit_once(run["id"], again_id="d" * 32, actor=ALICE, organization_id="org-1")
    other = await started(store)
    await store.claim(other["id"], owner="w1", lease_seconds=60)
    await store.finish(other["id"], owner="w1", state=None, succeeded=True)
    with pytest.raises(RunTaken):
        await store.resubmit_once(other["id"], again_id="d" * 32, actor=BOB, organization_id="org-1")
