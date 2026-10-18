"""
(e) Abandoning a run as it completes. Each run's last step is held by the
mock; the test answers it and abandons the run a few milliseconds later, a
little later for each run, so the abandonment lands all over the window
between the step's answer and the run's ``finish``: before its last
checkpoints, between them, after the finish.

Whichever lands first, the run ends one way, and says so once: succeeded
(its result, no abandonment after it) or abandoned (nothing after it, no
lease left). A request abandoning a run that's running is answered "its
worker stops it"; the run may still end succeeded, if it ended first
(run_store.finish: "it ended first, and nothing stops any more"): counted,
not failed.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from forge_task_adk_workflows.run_store import NotAllowed, RunStore

from .harness import workflows as w
from .harness.scenario import Scenario, mysql_engine

RUNS = 24
#: How far past the window the abandonments go, in windows.
SPAN = 2.5


def _flow(scenario: Scenario, name: str) -> dict[str, Any]:
    return w.line(w.http("last", scenario.mock.effect_url(name)), w.end("done", "steps.last.output.body"))


async def _held(scenario: Scenario, name: str) -> str:
    """A run, started, its last step held by the mock."""
    scenario.mock.hold(name)
    run_id = await scenario.start(_flow(scenario, name), {})
    await scenario.mock.arrived(name)
    return run_id


async def _abandoned_after(admin: RunStore, run_id: str, seconds: float) -> str:
    await asyncio.sleep(seconds)
    try:
        found = await admin.abandon(run_id, actor=w.CANCELLER)
    except NotAllowed:
        return "refused (it had finished)"
    if found["status"] == "running":
        return "accepted: its worker stops it"
    # Accepted as it ran, and read back after: the run ended first (abandoned, or succeeded).
    return f"accepted, and it was {found['status']} by then"


async def test_an_abandonment_as_a_run_completes_leaves_it_one_way_or_the_other(scenario: Scenario) -> None:
    await scenario.warm_sessions()
    await scenario.spawn_all("w1", "w2", concurrency=4)
    # How long a run takes from its last step's answer to its end, here: the window to land in.
    calibration = await _held(scenario, "calibration")
    released = time.time()
    scenario.mock.release("calibration")
    done = await scenario.finished(calibration, seconds=30)
    window = done["finished_at"].timestamp() - released
    scenario.observe("seconds from the last step's answer to the run's end", round(window, 3))
    engine = mysql_engine(scenario.services.mysql_url)
    admin = RunStore(engine)
    await admin.get(calibration)
    outcomes: Counter[tuple[str, str]] = Counter()
    by_delay: list[str] = []
    try:
        for n in range(RUNS):
            name = f"last-{n}"
            run_id = await _held(scenario, name)
            delay = window * SPAN * n / (RUNS - 1)
            scenario.mock.release(name)
            answer = await _abandoned_after(admin, run_id, delay)
            run: dict[str, Any] = await scenario.finished(run_id, seconds=30)
            kinds = await scenario.kinds(run_id)
            outcomes[(answer, run["status"])] += 1
            by_delay.append(f"{delay * 1000:.0f}ms: {run['status']}")
            assert run["status"] in ("succeeded", "abandoned"), (n, run["status"], kinds)
            assert (run["lease_owner"], run["lease_until"]) == (None, None)
            assert kinds.count("succeeded") + kinds.count("abandoned") == 1, (n, kinds)
            if run["status"] == "succeeded":
                assert run["result"] == {"ok": True, "name": name, "count": 1}
                assert run["cancel_requested_at"] is None
            else:
                assert answer != "refused (it had finished)", (n, kinds)
            assert scenario.mock.count(name) == 1
    finally:
        await engine.dispose()
    scenario.observe("(abandon answered, run ended)", {f"{k[0]} -> {k[1]}": v for k, v in outcomes.items()})
    scenario.observe("by delay after the answer", by_delay)
    assert len({status for _, status in outcomes}) == 2, "the abandonments all landed on one side of the end"
