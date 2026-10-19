"""The usage store on SQLite: model and tool calls and invocations kept as
they happen, and read back summed by hour, workflow or agent and model, as
an organization's overview reads them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import anyio
import pytest
import sqlalchemy as sa
from forge_common.adk.turns import TurnOutcome, turn_scope
from forge_common.adk.usage import Attribution, UsageCall
from google.adk.events import Event
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_task_adk_workflows.usage_store import (
    CANCELLED,
    FAILED,
    SUCCEEDED,
    UsageStore,
    hour_of,
    invocations,
    metadata,
)

SUPPORT = Attribution("org-1", "agent", "ca_support", "Support desk")
REFUNDS = Attribution("org-1", "workflow", "wf_refunds", "Refunds", run_id="run-1", user_id="user-bob")
ELSEWHERE = Attribution("org-2", "agent", "ca_other", "Other")
T0 = datetime(2026, 10, 4, 9, 15, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(clock: Clock) -> AsyncIterator[UsageStore]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield UsageStore(engine, clock=clock)
    await engine.dispose()


def model_call(
    who: Attribution,
    at: datetime,
    *,
    model: str = "openai/gpt-5.2",
    tokens: tuple[int, int, int, int] = (1000, 200, 300, 50),
    cost: float | None = 0.01,
    user: str = "user-ada",
    author: str = "triage",
    failed: bool = False,
) -> UsageCall:
    return UsageCall(
        attribution=who,
        call="model",
        name=model,
        author=author,
        at=at,
        session_id="s-1",
        user_id=user,
        run_id=who.run_id or "s-1",
        input_tokens=tokens[0],
        cached_tokens=tokens[1],
        output_tokens=tokens[2],
        thinking_tokens=tokens[3],
        cost=cost,
        failed=failed,
    )


def tool_call(who: Attribution, at: datetime, name: str, *, failed: bool = False) -> UsageCall:
    return UsageCall(attribution=who, call="tool", name=name, author="triage", at=at, failed=failed)


def test_an_hour_is_its_utc_start() -> None:
    eastern = datetime(2026, 10, 4, 5, 59, 30, tzinfo=UTC).astimezone()
    assert hour_of(eastern) == datetime(2026, 10, 4, 5, 0)


async def test_model_calls_sum_by_hour_subject_and_model(store: UsageStore) -> None:
    await store.record(model_call(SUPPORT, T0))
    await store.record(model_call(SUPPORT, T0 + timedelta(minutes=30), cost=None))
    await store.record(model_call(SUPPORT, T0 + timedelta(minutes=40), model="anthropic/claude-opus-5"))
    await store.record(model_call(SUPPORT, T0 + timedelta(hours=1), failed=True, tokens=(0, 0, 0, 0)))
    await store.record(model_call(REFUNDS, T0))
    await store.record(model_call(ELSEWHERE, T0))
    await store.record(tool_call(SUPPORT, T0, "lookup"))

    rows = await store.model_hours("org-1", T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    by_key = {(r["hour"], r["subject_id"], r["model"]): r for r in rows}
    nine = datetime(2026, 10, 4, 9, tzinfo=UTC)
    gpt = by_key[(nine, "ca_support", "openai/gpt-5.2")]
    assert gpt["calls"] == 2
    assert (gpt["input"], gpt["cached"], gpt["output"], gpt["thinking"]) == (2000, 400, 600, 100)
    # One call had no price: its cost is left out and counted.
    assert gpt["cost"] == pytest.approx(0.01)
    assert gpt["unpriced"] == 1
    assert by_key[(nine, "ca_support", "anthropic/claude-opus-5")]["calls"] == 1
    assert by_key[(nine + timedelta(hours=1), "ca_support", "openai/gpt-5.2")]["failed"] == 1
    assert by_key[(nine, "wf_refunds", "openai/gpt-5.2")]["kind"] == "workflow"
    # Another organization's, and tools, aren't model calls of this one.
    assert {r["subject_id"] for r in rows} == {"ca_support", "wf_refunds"}


async def test_a_fenced_call_is_kept_only_when_its_fence_lets_it(store: UsageStore) -> None:
    class Gone(Exception):
        pass

    fenced: list[bool] = []

    async def holds(conn: AsyncConnection) -> None:
        fenced.append(conn.in_transaction())

    async def lost(conn: AsyncConnection) -> None:
        raise Gone("the worker lost the run")

    await store.record(model_call(REFUNDS, T0), fence=holds)
    with pytest.raises(Gone):
        await store.record(model_call(REFUNDS, T0, model="anthropic/claude-opus-5"), fence=lost)

    [kept] = await store.model_hours("org-1", T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert (kept["model"], kept["calls"]) == ("openai/gpt-5.2", 1)
    assert fenced == [True]  # asked on the connection that keeps it, in its transaction


async def test_the_window_is_by_hour(store: UsageStore) -> None:
    await store.record(model_call(SUPPORT, T0))
    assert await store.model_hours("org-1", T0 + timedelta(minutes=30), T0 + timedelta(hours=1))
    assert not await store.model_hours("org-1", T0 + timedelta(hours=1), T0 + timedelta(hours=2))


async def test_steps_tools_and_people(store: UsageStore) -> None:
    await store.record(model_call(SUPPORT, T0, author="triage", tokens=(100, 0, 20, 0)))
    await store.record(model_call(SUPPORT, T0, author="writer", tokens=(50, 0, 10, 0), user="user-bob"))
    await store.record(tool_call(SUPPORT, T0, "lookup"))
    await store.record(tool_call(SUPPORT, T0, "lookup", failed=True))
    async with store.invocation(SUPPORT, session_id="s-1", user_id="user-ada"):
        pass

    since, until = T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    steps = {r["author"]: r for r in await store.authors("org-1", since, until)}
    assert steps["triage"]["tokens"] == 120
    assert steps["writer"]["calls"] == 1
    assert await store.tools("org-1", since, until) == [
        {"kind": "agent", "subject_id": "ca_support", "tool": "lookup", "calls": 2, "failed": 1}
    ]
    people = {r["user_id"]: r for r in await store.people("org-1", since, until)}
    assert people["user-ada"]["runs"] == 1
    assert people["user-ada"]["tokens"] == 120
    assert people["user-bob"]["runs"] == 0
    assert people["user-bob"]["calls"] == 1


async def test_an_invocation_ends_as_its_block_does(store: UsageStore, clock: Clock) -> None:
    async with store.invocation(SUPPORT, version="3", session_id="s-1", user_id="user-ada"):
        clock.now = T0 + timedelta(seconds=2)
    with pytest.raises(RuntimeError):
        async with store.invocation(SUPPORT, session_id="s-2"):
            raise RuntimeError("model down")

    async def streamed():  # type: ignore[no-untyped-def]
        async with store.invocation(SUPPORT, session_id="s-3"):
            yield 1
            yield 2

    stream = streamed()
    await anext(stream)
    await stream.aclose()  # the caller went

    rows = await store.invocation_hours("org-1", T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    by_status = {r["status"]: r for r in rows}
    assert by_status[SUCCEEDED]["count"] == 1
    assert by_status[SUCCEEDED]["duration_ms"] == 2000
    assert by_status[FAILED]["count"] == 1
    assert by_status[CANCELLED]["count"] == 1
    failures = await store.invocation_failures("org-1", T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [(f["error"], f["count"]) for f in failures] == [("RuntimeError: model down", 1)]


async def invocation_rows(store: UsageStore) -> list[dict]:
    async with store.engine.connect() as connection:
        rows = await connection.execute(sa.select(invocations))
        return [dict(row) for row in rows.mappings()]


async def test_native_error_event_is_a_failed_invocation_with_the_turns_id(store: UsageStore) -> None:
    with turn_scope() as turn:
        async with store.invocation(SUPPORT):
            turn.observe(Event(author="helper", error_code="SAFETY", error_message="Response blocked"))
    [row] = await invocation_rows(store)
    assert row["id"] == turn.run_id
    assert row["status"] == FAILED
    assert row["error"] == "SAFETY: Response blocked"
    assert row["finished_at"] is not None


async def test_nested_observers_keep_distinct_audit_rows(store: UsageStore) -> None:
    with turn_scope() as turn:
        async with store.invocation(SUPPORT):
            async with store.invocation(SUPPORT):
                pass
    rows = await invocation_rows(store)
    assert len(rows) == 2
    assert turn.run_id in {row["id"] for row in rows}
    assert all(row["status"] == SUCCEEDED for row in rows)


async def test_a_turn_cancelled_by_an_event_is_not_recorded_as_success(store: UsageStore) -> None:
    turn = TurnOutcome()
    with turn_scope(turn):
        async with store.invocation(SUPPORT):
            turn.observe(Event(author="helper", interrupted=True))
    [row] = await invocation_rows(store)
    assert row["status"] == CANCELLED


@pytest.mark.parametrize("backend", ["asyncio", "anyio"])
@pytest.mark.parametrize("timed_out", [False, True])
async def test_cancellation_after_start_commits_still_finishes_the_row(
    store: UsageStore,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    timed_out: bool,
) -> None:
    committed = asyncio.Event()
    scopes: list[anyio.CancelScope] = []
    turn = TurnOutcome()
    original_start = store._start

    async def start(*args) -> bool:
        await original_start(*args)
        committed.set()
        await asyncio.Future()
        return True

    monkeypatch.setattr(store, "_start", start)

    async def run() -> None:
        with anyio.CancelScope() as scope, turn_scope(turn):
            scopes.append(scope)
            async with store.invocation(SUPPORT):
                pytest.fail("The invocation body cannot run before its audit starts")

    task = asyncio.create_task(run())
    await committed.wait()
    if timed_out:
        turn.fail("TURN_TIMEOUT", "Deadline reached")
    if backend == "asyncio":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        scopes[0].cancel()
        await task
    [row] = await invocation_rows(store)
    assert row["status"] == (FAILED if timed_out else CANCELLED)
    assert row["error"] == ("TURN_TIMEOUT: Deadline reached" if timed_out else None)
    assert row["finished_at"] is not None


@pytest.mark.parametrize("native_failure", [False, True])
async def test_anyio_disconnect_finishes_the_audit_without_erasing_a_failure(
    store: UsageStore,
    native_failure: bool,
) -> None:
    with turn_scope() as turn, anyio.CancelScope() as scope:
        async with store.invocation(SUPPORT):
            if native_failure:
                turn.observe(Event(author="helper", error_code="SAFETY", error_message="Blocked"))
            scope.cancel()
            await anyio.sleep(0)
        pytest.fail("The disconnect cancellation must still propagate")
    [row] = await invocation_rows(store)
    assert row["status"] == (FAILED if native_failure else CANCELLED)
    assert row["finished_at"] is not None
    assert row["error"] == ("SAFETY: Blocked" if native_failure else None)


async def test_asyncio_cancellation_during_cleanup_keeps_the_row_and_propagates(
    store: UsageStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    finishing = asyncio.Event()
    release = asyncio.Event()
    original_finish = store._finish

    async def finish(*args) -> None:
        finishing.set()
        await release.wait()
        await original_finish(*args)

    monkeypatch.setattr(store, "_finish", finish)

    async def run() -> None:
        with turn_scope():
            async with store.invocation(SUPPORT):
                entered.set()
                await asyncio.Future()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await finishing.wait()
    task.cancel()  # A second cancellation while the database is finishing.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    [row] = await invocation_rows(store)
    assert row["status"] == CANCELLED
    assert row["finished_at"] is not None


@pytest.mark.parametrize("backend", ["asyncio", "anyio"])
@pytest.mark.parametrize("failure", ["none", "body", "cleanup"])
async def test_cancellation_during_a_success_write_reconciles_the_persisted_outcome(
    store: UsageStore,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    failure: str,
) -> None:
    finishing = asyncio.Event()
    release = asyncio.Event()
    scopes: list[anyio.CancelScope] = []
    turn = TurnOutcome()
    original_finish = store._finish

    async def finish(*args) -> None:
        finishing.set()
        await release.wait()
        await original_finish(*args)

    monkeypatch.setattr(store, "_finish", finish)

    async def run() -> None:
        with anyio.CancelScope() as scope, turn_scope(turn):
            scopes.append(scope)
            try:
                async with store.invocation(SUPPORT):
                    if failure == "body":
                        turn.observe(Event(author="helper", error_code="SAFETY", error_message="Blocked"))
                await anyio.lowlevel.checkpoint_if_cancelled()
            except asyncio.CancelledError:
                turn.cancel()
                raise

    task = asyncio.create_task(run())
    await finishing.wait()
    if failure == "cleanup":
        turn.fail("TURN_TIMEOUT", "Deadline reached")
    if backend == "asyncio":
        task.cancel()
    else:
        scopes[0].cancel()
    release.set()
    if backend == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task  # The owning AnyIO scope consumes its own cancellation.
    [row] = await invocation_rows(store)
    assert row["status"] == turn.status == (CANCELLED if failure == "none" else FAILED)
    assert (
        row["error"]
        == {
            "none": None,
            "body": "SAFETY: Blocked",
            "cleanup": "TURN_TIMEOUT: Deadline reached",
        }[failure]
    )
    assert row["finished_at"] is not None


async def test_a_stalled_audit_write_has_a_bounded_cleanup(
    store: UsageStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stopped = asyncio.Event()

    async def never_finishes(*args) -> None:
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(store, "_finish", never_finishes)
    monkeypatch.setattr("forge_task_adk_workflows.usage_store.FINISH_TIMEOUT", 0.02)
    async with asyncio.timeout(1):
        async with store.invocation(SUPPORT):
            pass
        await stopped.wait()
    assert "exceeded" in caplog.text


async def test_subjects_take_their_latest_name(store: UsageStore) -> None:
    await store.record(model_call(SUPPORT, T0))
    renamed = Attribution("org-1", "agent", "ca_support", "Help desk")
    await store.record(model_call(renamed, T0 + timedelta(hours=3)))
    async with store.invocation(REFUNDS):
        pass
    subjects = {r["subject_id"]: r for r in await store.subjects("org-1")}
    assert subjects["ca_support"]["subject_name"] == "Help desk"
    assert subjects["ca_support"]["last_at"] == T0 + timedelta(hours=3)
    assert subjects["wf_refunds"]["kind"] == "workflow"
    assert await store.first_call("org-1") == T0
    assert await store.first_call("org-3") is None


async def test_an_organization_is_forgotten(store: UsageStore) -> None:
    await store.record(model_call(SUPPORT, T0))
    await store.record(model_call(ELSEWHERE, T0))
    async with store.invocation(SUPPORT):
        pass
    assert await store.delete_organization("org-1") == 2
    assert await store.subjects("org-1") == []
    assert len(await store.subjects("org-2")) == 1
