"""
(d) Races at a run's gate: two people deciding the same approval at once,
and a decision against an abandonment. Each side is its own admin API
request: its own engine, so its own MySQL connection and transaction, as
two API replicas would have. ``decide`` and ``abandon`` lock the run's row
(SELECT ... FOR UPDATE) and then write; whichever is second reads the row as
the first left it.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest

from forge_task_adk_workflows.run_store import NotAllowed, NotOpen, RunStore

from .harness import workflows as w
from .harness.scenario import Scenario, mysql_engine

RUNS = 10


def _gated(scenario: Scenario, n: int) -> dict[str, Any]:
    """An approval: approved, it calls ``yes-<n>``; rejected, ``no-<n>``."""
    nodes = [
        w.start(),
        w.approval("review"),
        w.http("ship", scenario.mock.effect_url(f"yes-{n}")),
        w.http("drop", scenario.mock.effect_url(f"no-{n}")),
        w.end("shipped", '"shipped"'),
        w.end("dropped", '"dropped"'),
    ]
    edges = [
        ("start", "next", "review"),
        ("review", "approved", "ship"),
        ("review", "rejected", "drop"),
        ("ship", "success", "shipped"),
        ("drop", "success", "dropped"),
    ]
    return w.document(nodes, edges, name=f"Gated {n}")


@pytest.fixture
async def admins(scenario: Scenario) -> AsyncIterator[tuple[RunStore, RunStore]]:
    """Two admin API replicas' run stores, each on connections of its own."""
    engines = [mysql_engine(scenario.services.mysql_url) for _ in range(2)]
    stores = (RunStore(engines[0]), RunStore(engines[1]))
    yield stores
    for engine in engines:
        await engine.dispose()


async def _paused(scenario: Scenario) -> list[dict[str, Any]]:
    await scenario.warm_sessions()
    await scenario.spawn("w1", concurrency=8)
    run_ids = [await scenario.start(_gated(scenario, n), {}) for n in range(RUNS)]
    return [await scenario.until(run_id, "paused", seconds=60) for run_id in run_ids]


async def _decided(store: RunStore, run: dict[str, Any], approved: bool, actor: Any) -> str:
    try:
        await store.decide(run["id"], request_id=run["pause"]["id"], approved=approved, comment="", actor=actor)
    except NotOpen:
        return "refused"
    return "approved" if approved else "rejected"


async def _abandoned(store: RunStore, run: dict[str, Any]) -> str:
    try:
        found = await store.abandon(run["id"], actor=w.CANCELLER)
    except NotAllowed:
        return "refused"
    return f"abandoned ({found['status']})"


async def test_two_decisions_at_once_on_one_approval_one_lands(
    scenario: Scenario, admins: tuple[RunStore, RunStore]
) -> None:
    paused = await _paused(scenario)
    first, second = admins
    for store in admins:
        await store.get(paused[0]["id"])  # connected before the race
    outcomes: Counter[tuple[str, str]] = Counter()
    landed: dict[str, str] = {}
    for run in paused:
        yes, no = await asyncio.gather(_decided(first, run, True, w.DECIDER), _decided(second, run, False, w.MEMBER))
        outcomes[(yes, no)] += 1
        assert sorted([yes, no]).count("refused") == 1, f"run {run['id'][:8]}: {yes}, {no}"
        landed[run["id"]] = yes if yes != "refused" else no
        await scenario.enqueue(run["id"])  # as the request that decided it queues its job

    for n, run in enumerate(paused):
        done = await scenario.finished(run["id"], seconds=60)
        kinds = await scenario.kinds(run["id"])
        assert kinds.count("decided") == 1
        assert list((await scenario.run(run["id"]))["decisions"]) == [run["pause"]["key"]]
        way = landed[run["id"]]
        assert done["status"] == "succeeded", done["error"]
        assert done["result"] == ("shipped" if way == "approved" else "dropped")
        assert (scenario.mock.count(f"yes-{n}"), scenario.mock.count(f"no-{n}")) == (
            (1, 0) if way == "approved" else (0, 1)
        )
    scenario.observe("(approve, reject) outcomes", dict(outcomes))


async def test_a_decision_and_an_abandonment_at_once_leave_the_run_abandoned(
    scenario: Scenario, admins: tuple[RunStore, RunStore]
) -> None:
    paused = await _paused(scenario)
    deciding, abandoning = admins
    for store in admins:
        await store.get(paused[0]["id"])
    outcomes: Counter[tuple[str, str]] = Counter()
    for run in paused:
        decided, abandoned = await asyncio.gather(_decided(deciding, run, True, w.DECIDER), _abandoned(abandoning, run))
        outcomes[(decided, abandoned)] += 1
        if decided != "refused":
            await scenario.enqueue(run["id"])

    await asyncio.sleep(3)  # the decided runs' jobs have come and gone
    for n, run in enumerate(paused):
        done = await scenario.finished(run["id"], seconds=30)
        kinds = await scenario.kinds(run["id"])
        assert done["status"] == "abandoned", kinds
        assert "succeeded" not in kinds
        if "resumed" not in kinds:  # abandoned before any worker took it again: nothing ran
            assert scenario.mock.count(f"yes-{n}") == 0
    scenario.observe("(decision, abandonment) outcomes", dict(outcomes))
    taken = [run["id"][:8] for run in paused if "resumed" in await scenario.kinds(run["id"])]
    scenario.observe("runs a worker took again before the abandonment", taken)
