"""An organization's overview: the period's buckets in the reader's days, and
what it reads from the run store and the usage store (both on SQLite),
summed into them."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from forge_common.adk.usage import Attribution, UsageCall
from forge_task_adk_workflows import run_store, usage_store
from forge_task_adk_workflows.run_store import Actor, RunStore
from forge_task_adk_workflows.usage_store import UsageStore
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.overview.recording import assistant_attribution
from forge_admin.overview.report import Sources, build_overview, window, zone_of

NEW_YORK = ZoneInfo("America/New_York")
ORG = "org-1"
# A Wednesday afternoon in New York, three days after the clocks went back.
NOW = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)

# ------------------------------------------------------------------ the window


def test_days_are_the_readers_midnight_to_midnight() -> None:
    win = window("7d", NEW_YORK, NOW)
    starts = [start.astimezone(NEW_YORK) for start, _ in win.buckets]
    assert [start.day for start in starts] == [29, 30, 31, 1, 2, 3, 4]
    assert all((start.hour, start.minute) == (0, 0) for start in starts)
    # The day the clocks went back has 25 hours.
    long_day = win.buckets[3]
    assert long_day[1] - long_day[0] == timedelta(hours=25)
    assert win.buckets[-1][1] == NOW
    # The 7 days before.
    assert win.previous == (
        datetime(2026, 10, 22, tzinfo=NEW_YORK).astimezone(UTC),
        win.buckets[0][0],
    )


def test_ninety_days_go_by_week_from_monday() -> None:
    win = window("90d", NEW_YORK, NOW)
    assert win.unit == "week"
    first = win.buckets[0][0].astimezone(NEW_YORK)
    assert first.date() == (NOW.astimezone(NEW_YORK) - timedelta(days=89)).date()
    assert all(
        start.astimezone(NEW_YORK).weekday() == 0 for start, _ in win.buckets[1:]
    )
    assert win.buckets[-1][1] == NOW


def test_the_last_twelve_weeks_end_this_week() -> None:
    win = window("7d", NEW_YORK, NOW)
    assert len(win.weeks) == 12
    assert win.weeks[-1][0].astimezone(NEW_YORK) == datetime(
        2026, 11, 2, tzinfo=NEW_YORK
    )
    assert win.weeks[-1][1] == NOW


def test_a_time_lands_in_its_bucket_the_one_before_or_none() -> None:
    win = window("7d", NEW_YORK, NOW)
    assert win.bucket_of(NOW) == 6
    assert win.bucket_of(win.buckets[2][0]) == 2
    assert win.bucket_of(win.buckets[0][0] - timedelta(seconds=1)) == -1
    assert win.bucket_of(win.previous[0] - timedelta(seconds=1)) is None


def test_an_unknown_time_zone_is_utc() -> None:
    assert zone_of("Mars/Olympus").key == "UTC"
    assert zone_of(None).key == "UTC"
    assert zone_of("Europe/Oslo").key == "Europe/Oslo"


# ------------------------------------------------------------------ attribution


def test_the_assistant_counts_for_the_organization_its_page_is_in_if_theirs() -> None:
    page = {
        "entities": [{"kind": "agent", "id": "a"}, {"kind": "organization", "id": ORG}]
    }
    mine = [{"id": ORG, "name": "Org"}]
    found = assistant_attribution(page, mine, "user-ada")
    assert found == Attribution(
        ORG, "assistant", "assistant", "Assistant", user_id="user-ada"
    )
    assert assistant_attribution(page, [{"id": "org-2"}], "user-ada") is None
    assert assistant_attribution(None, mine, "user-ada") is None
    assert assistant_attribution({"entities": "x"}, mine, "user-ada") is None


# ------------------------------------------------------------------ reading


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


Stores = tuple[RunStore, UsageStore, Clock]


def with_stores(scenario: Callable[[Stores], Awaitable[None]]) -> None:
    """Runs ``scenario`` over a run store and a usage store on SQLite, sharing a clock."""

    async def main() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(run_store.metadata.create_all)
            await conn.run_sync(usage_store.metadata.create_all)
        clock = Clock(NOW)
        try:
            await scenario(
                (RunStore(engine, clock=clock), UsageStore(engine, clock=clock), clock)
            )
        finally:
            await engine.dispose()

    asyncio.run(main())


async def a_run(
    runs: RunStore, clock: Clock, at: datetime, *, ends: str, agent: str = "wf_refunds"
) -> str:
    clock.now = at
    run = await runs.create(
        organization_id=ORG,
        agent_id=agent,
        agent_name="Refunds",
        revision=1,
        session_id=f"s-{at.timestamp()}",
        payload={},
        requested_by=Actor("user-bob", "Bob Builder"),
    )
    if ends == "queued":
        return str(run["id"])
    await runs.claim(run["id"], owner="w1", lease_seconds=60)
    clock.now = at + timedelta(seconds=30)
    if ends == "paused":
        await runs.pause(
            run["id"],
            owner="w1",
            state={},
            key="review",
            kind="approval",
            reason="Refund 40 EUR?",
            details={"kind": "approval"},
            deadline=None,
        )
    else:
        await runs.finish(
            run["id"],
            owner="w1",
            state={},
            succeeded=ends == "succeeded",
            error=None
            if ends == "succeeded"
            else {"message": "No such order", "step": "lookup"},
        )
    return str(run["id"])


def call(
    who: Attribution,
    at: datetime,
    tokens: int,
    cost: float | None,
    model: str = "openai/gpt-5.2",
) -> UsageCall:
    return UsageCall(
        attribution=who,
        call="model",
        name=model,
        author="triage",
        at=at,
        user_id=who.user_id or "user-ada",
        input_tokens=tokens,
        cached_tokens=tokens // 2,
        output_tokens=tokens // 10,
        cost=cost,
    )


async def no_names(ids: list[str]) -> dict[str, str]:
    return {user: f"Name of {user}" for user in ids}


def test_the_overview_sums_runs_invocations_and_calls_into_the_days() -> None:
    with_stores(sums_runs_invocations_and_calls)


async def sums_runs_invocations_and_calls(stores: Stores) -> None:
    runs, usage, clock = stores
    win = window("7d", NEW_YORK, NOW)
    today, yesterday = win.buckets[6][0], win.buckets[5][0]
    before = win.previous[0] + timedelta(days=1)

    await a_run(runs, clock, today + timedelta(hours=1), ends="succeeded")
    await a_run(runs, clock, today + timedelta(hours=2), ends="failed")
    paused = await a_run(runs, clock, yesterday + timedelta(hours=3), ends="paused")
    await a_run(runs, clock, before, ends="succeeded")

    support = Attribution(ORG, "agent", "ca_support", "Support desk")
    refunds = Attribution(
        ORG, "workflow", "wf_refunds", "Refunds", run_id="r", user_id="user-bob"
    )
    await usage.record(call(support, today + timedelta(hours=1), 1000, 0.01))
    await usage.record(call(support, today + timedelta(hours=1, minutes=5), 1000, None))
    await usage.record(
        call(
            refunds,
            yesterday + timedelta(hours=4),
            2000,
            0.02,
            "anthropic/claude-opus-5",
        )
    )
    await usage.record(call(support, before, 500, 0.005))
    clock.now = today + timedelta(hours=5)
    async with usage.invocation(support, session_id="s-1", user_id="user-ada"):
        clock.now += timedelta(seconds=4)
    clock.now = NOW

    found = await build_overview(
        ORG,
        Sources(
            runs,
            usage,
            workflows=[("wf_refunds", "Refunds")],
            agents=[("ca_support", "Support desk")],
        ),
        win,
        no_names,
    )

    assert found["period"] == "7d" and found["time_zone"] == "America/New_York"
    assert len(found["buckets"]) == 7
    subjects = {s["key"]: s for s in found["subjects"]}
    assert subjects["workflow:wf_refunds"]["name"] == "Refunds"
    assert subjects["agent:ca_support"]["current"]

    usage_facts = {(f["bucket"], f["subject"], f["model"]): f for f in found["usage"]}
    today_support = usage_facts[(6, "agent:ca_support", "openai/gpt-5.2")]
    assert today_support["calls"] == 2
    assert today_support["input"] == 2000 and today_support["output"] == 200
    assert (
        today_support["cost"] == pytest.approx(0.01) and today_support["unpriced"] == 1
    )
    assert (
        usage_facts[(5, "workflow:wf_refunds", "anthropic/claude-opus-5")]["input"]
        == 2000
    )
    assert usage_facts[(-1, "agent:ca_support", "openai/gpt-5.2")]["input"] == 500

    activity = {(f["bucket"], f["subject"]): f for f in found["activity"]}
    workflow_today = activity[(6, "workflow:wf_refunds")]
    assert (
        workflow_today["started"],
        workflow_today["succeeded"],
        workflow_today["failed"],
    ) == (2, 1, 1)
    assert workflow_today["timed"] == 2 and workflow_today["duration_ms"] == 60_000
    assert activity[(5, "workflow:wf_refunds")]["open"] == 1
    assert activity[(-1, "workflow:wf_refunds")]["succeeded"] == 1
    agent_today = activity[(6, "agent:ca_support")]
    assert (
        agent_today["started"],
        agent_today["succeeded"],
        agent_today["duration_ms"],
    ) == (1, 1, 4000)

    weekly = {(f["subject"], f["week"]): f for f in found["weekly"]}
    assert weekly[("workflow:wf_refunds", 11)]["runs"] == 3

    assert [w["run_id"] for w in found["waiting"]] == [paused]
    assert found["waiting"][0]["reason"] == "Refund 40 EUR?"
    assert found["open"] == {"queued": 0, "running": 0, "paused": 1, "waiting": 0}
    assert found["failures"] == [
        {
            "subject": "workflow:wf_refunds",
            "step": "lookup",
            "message": "No such order",
            "count": 1,
            "last_at": (today + timedelta(hours=2, seconds=30)).isoformat(),
        }
    ]
    steps = {(s["subject"], s["author"]): s for s in found["steps"]}
    assert steps[("agent:ca_support", "triage")]["calls"] == 2

    people = {(p["subject"], p["user"]): p for p in found["people"]}
    assert people[("workflow:wf_refunds", "user-bob")]["runs"] == 3
    assert people[("agent:ca_support", "user-ada")]["runs"] == 1
    members = {m["id"]: m["name"] for m in found["members"]}
    # A run names who ran it; anyone else is looked up.
    assert members == {"user-bob": "Bob Builder", "user-ada": "Name of user-ada"}
    assert found["recording_since"] is not None
    assert found["truncated"] is False


def test_an_organization_with_nothing_yet() -> None:
    with_stores(nothing_yet)


async def nothing_yet(stores: Stores) -> None:
    runs, usage, _ = stores
    found = await build_overview(
        ORG, Sources(runs, usage), window("30d", NEW_YORK, NOW), no_names
    )
    assert len(found["buckets"]) == 30
    assert found["usage"] == found["activity"] == found["subjects"] == []
    assert found["recording_since"] is None
