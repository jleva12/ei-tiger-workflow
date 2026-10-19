"""
(a) Workers racing to claim the same queued run: exactly one wins.

On MySQL, ``claim`` is one conditional UPDATE (queued, or waiting and due,
and not being abandoned): under REPEATABLE READ the second UPDATE waits for
the first's row lock, then reads the row as committed, so it matches
nothing. SQLite's tests can't show that (one connection, no row locks).
"""

from __future__ import annotations

import asyncio
from collections import Counter

from forge_task_adk_workflows.run_store import Actor, RunStore

from .harness import workflows as w
from .harness.scenario import Scenario, mysql_engine

RUNS = 30
CLAIMERS = 4


async def test_concurrent_claims_on_separate_connections_take_a_run_once(scenario: Scenario) -> None:
    flow = w.line(w.end("done"))
    run_ids = [await scenario.start(flow, {}, queue=False) for _ in range(RUNS)]
    # Each claimer on an engine (a pool, its own connections) of its own, as each worker has.
    engines = [mysql_engine(scenario.services.mysql_url) for _ in range(CLAIMERS)]
    stores = [RunStore(engine) for engine in engines]
    try:
        for store in stores:
            await store.get(run_ids[0])  # connected before the race, so none starts late
        winners: Counter[int] = Counter()
        for run_id in run_ids:
            claimed = await asyncio.gather(
                *(store.claim(run_id, owner=f"claimer-{n}", lease_seconds=60) for n, store in enumerate(stores))
            )
            taken = [n for n, run in enumerate(claimed) if run is not None]
            assert len(taken) == 1, f"run {run_id[:8]} was taken by claimers {taken}"
            winners[taken[0]] += 1
            run = await scenario.run(run_id)
            assert (run["status"], run["lease_epoch"], run["lease_owner"]) == ("running", 1, f"claimer-{taken[0]}")
            assert (await scenario.kinds(run_id)).count("started") == 1
    finally:
        for engine in engines:
            await engine.dispose()
    scenario.observe("claims won by each claimer", dict(sorted(winners.items())))


async def test_worker_processes_given_the_same_run_run_it_once(scenario: Scenario) -> None:
    await scenario.spawn_all("w1", "w2", "w3", concurrency=4)
    await scenario.warm_sessions()  # the claims race here, not ADK's first session (IT-1)
    flows = [w.line(w.http("call", scenario.mock.effect_url(f"claim-{n}")), w.end("done")) for n in range(10)]
    run_ids = [await scenario.start(flow, {}, queue=False) for flow in flows]
    # Three jobs for each run, at once: as a decision's, the maintenance's and a retry's might land together.
    await asyncio.gather(*(scenario.enqueue(run_id) for run_id in run_ids for _ in range(3)))

    finished = [await scenario.finished(run_id, seconds=60) for run_id in run_ids]

    for n, run in enumerate(finished):
        assert run["status"] == "succeeded", run["error"]
        assert (run["attempt"], run["lease_epoch"]) == (1, 1)
        assert scenario.mock.count(f"claim-{n}") == 1
        assert (await scenario.kinds(run["id"])).count("started") == 1
    skipped = sum(worker.text().count("isn't to be run now") for worker in scenario.workers)
    takers: Counter[str] = Counter()
    for run_id in run_ids:
        for event in await scenario.events(run_id):
            if event["kind"] == "started":
                takers[scenario.worker_of(scenario.owner_pid(event))] += 1
    scenario.observe("jobs that found their run taken (skipped)", skipped)
    scenario.observe("runs taken by each worker", dict(sorted(takers.items())))
    assert skipped == 20


async def test_a_new_members_first_runs_started_together_all_run(scenario: Scenario) -> None:
    await scenario.spawn_all("w1", "w2", "w3", concurrency=4)
    await scenario.warm_sessions("someone-else")  # the app's state row is there; the member's isn't
    newcomer = Actor("user-newcomer", "Nico Newcomer")
    flow = w.line(w.http("call", scenario.mock.effect_url("first")), w.end("done"))
    run_ids = [await scenario.start(flow, {}, queue=False, run_as=newcomer) for _ in range(12)]
    await asyncio.gather(*(scenario.enqueue(run_id) for run_id in run_ids))

    finished = [await scenario.finished(run_id, seconds=60) for run_id in run_ids]

    failed = [run for run in finished if run["status"] != "succeeded"]
    scenario.observe("runs of the 12 that failed", len(failed))
    scenario.observe("their errors", sorted({str(run["error"]["message"])[:160] for run in failed}))
    scenario.observe("runs of the 12 tried again", sum(run["attempt"] > 1 for run in finished))
    assert failed == []
    # At their first attempt: the session's state rows don't race (IT-1's fix), not a hiccup tried again.
    assert [run["attempt"] for run in finished] == [1] * 12
