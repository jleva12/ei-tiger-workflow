"""The usage store on SQLite: model and tool calls and invocations kept as
they happen, and read back summed by hour, workflow or agent and model, as
an organization's overview reads them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from forge_common.adk.usage import Attribution, UsageCall
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_task_adk_workflows.usage_store import (
    CANCELLED,
    FAILED,
    SUCCEEDED,
    UsageStore,
    hour_of,
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
