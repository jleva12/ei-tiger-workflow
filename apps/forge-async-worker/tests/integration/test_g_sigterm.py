"""
(g) SIGTERM to a worker while it runs a run, under the shutdown code as it
is (saq_worker.run_workers): it takes no more jobs, waits up to its grace
period for running ones (``_drain``), then stops SAQ, which cancels what
still runs; the run's attempt stops its job and lets the run go
(``_let_go``: queued again, with a job of its own), and SAQ re-queues its
job too (``job.retry("cancelled")``), when that's done within SAQ's
cancellation deadline (a second).

- A step that ends within the grace period: the run ends on that worker.
- A step that doesn't (``cut_at_grace``): the run is let go, and another
  worker should take it again at once, not when the maintenance finds it
  stalled (here after 10 seconds, the scaled two minutes); and go on to its
  end. Its step is a GET, which runs again (a POST cut off would fail the
  run closed, saying it may have run: test_b_kill_recovery.py).
- While it drains, the stopping worker shouldn't take new runs: whatever it
  takes then, it cuts off at the end of its grace period.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from .harness import workflows as w
from .harness.mock_server import MockServer
from .harness.scenario import Scenario, Services, played

GRACE = 3
STALLED = 10


async def test_a_step_that_ends_within_the_grace_period_ends_its_run_there(scenario: Scenario) -> None:
    await scenario.warm_sessions()
    worker = await scenario.spawn("w1", grace=10)
    scenario.mock.hold("short")
    flow = w.line(w.http("short", scenario.mock.effect_url("short")), w.end("done", "steps.short.output.body"))
    run_id = await scenario.start(flow, {})
    await scenario.mock.arrived("short")
    worker.signal(signal.SIGTERM)
    await asyncio.sleep(1)
    scenario.mock.release("short")

    code = await worker.exited(30)
    run = await scenario.finished(run_id, seconds=10)

    scenario.observe("exit status", code)
    scenario.observe("run status, attempt", (run["status"], run["attempt"]))
    assert code == 0
    assert (run["status"], run["attempt"], run["result"]["name"]) == ("succeeded", 1, "short")
    assert scenario.mock.count("short") == 1


# ----------------------------------------------------------------------------- cut off at the grace period


async def _play_cut_at_grace(scenario: Scenario) -> dict[str, Any]:
    await scenario.warm_sessions()
    stopping = await scenario.spawn("w1", grace=GRACE, maintenance=2, stalled_after=STALLED)
    scenario.mock.hold("long")
    flow = w.line(
        w.http("long", scenario.mock.effect_url("long"), method="GET"), w.end("done", "steps.long.output.body")
    )
    run_id = await scenario.start(flow, {})
    await scenario.mock.arrived("long")
    other = await scenario.spawn("w2", maintenance=2, stalled_after=STALLED)
    stopping.signal(signal.SIGTERM)
    code = await stopping.exited(GRACE + 60)
    scenario.mock.release("long")  # the cut-off request, answered to nobody
    run = await scenario.finished(run_id, seconds=STALLED + 40)

    activity = await scenario.events(run_id)
    let_go = next((e for e in activity if e["kind"] == "retried"), None)
    taken = next((e for e in activity if e["kind"] == "resumed"), None)
    rescued = [line for line in other.text().splitlines() if "maintenance" in line and "queued" in line]
    seconds = round((taken["at"] - let_go["at"]).total_seconds(), 1) if let_go and taken else None
    scenario.observe("stopping worker's exit status", code)
    scenario.observe("let go as", let_go and let_go["message"])
    scenario.observe("seconds from being let go to being taken again", seconds)
    scenario.observe("the maintenance queued it as stalled", bool(rescued))
    scenario.observe("status, attempt, lease epoch", (run["status"], run["attempt"], run["lease_epoch"]))
    scenario.observe("result", run["result"])
    scenario.observe("'long' requests (its effect)", scenario.mock.count("long"))
    return {
        "code": code,
        "seconds": seconds,
        "rescued": bool(rescued),
        "run": run,
        "effects": scenario.mock.count("long"),
    }


@pytest.fixture(scope="module")
def cut_at_grace(
    services: Services, mock: MockServer, tmp_path_factory: pytest.TempPathFactory, reports: Path
) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("cut-at-grace")
    return played(
        services,
        mock,
        folder,
        name="g_sigterm_step_outlasts_the_grace_period",
        reports=reports,
        play=_play_cut_at_grace,
    )


def test_a_worker_stopped_mid_step_exits_once_its_run_is_let_go(cut_at_grace: dict[str, Any]) -> None:
    assert cut_at_grace["code"] == 0


def test_a_run_let_go_at_shutdown_is_taken_again_at_once(cut_at_grace: dict[str, Any]) -> None:
    # _let_go queues a job of its own once the run is queued again; SAQ's job.retry("cancelled")
    # does too, if the attempt stopped within SAQ's cancellation deadline (a second), but may come
    # first and find the run still running. Not the maintenance, finding it stalled.
    assert cut_at_grace["seconds"] is not None and cut_at_grace["seconds"] < 5 and not cut_at_grace["rescued"]


def test_a_run_let_go_at_shutdown_goes_on_to_its_end(cut_at_grace: dict[str, Any]) -> None:
    run = cut_at_grace["run"]
    assert run["status"] == "succeeded" and run["result"] is not None and run["result"]["name"] == "long"
    # The GET cut off at shutdown, and the next attempt's (FINDINGS.md IT-2: it was skipped).
    assert cut_at_grace["effects"] == 2


# ----------------------------------------------------------------------------- draining


async def test_a_worker_that_is_stopping_takes_no_new_runs(scenario: Scenario) -> None:
    await scenario.warm_sessions()
    stopping = await scenario.spawn("w1", grace=8)
    scenario.mock.hold("busy")
    busy = await scenario.start(w.line(w.http("busy", scenario.mock.effect_url("busy")), w.end("done")), {})
    await scenario.mock.arrived("busy")
    stopping.signal(signal.SIGTERM)
    await asyncio.sleep(0.5)
    new = await scenario.start(w.line(w.http("new", scenario.mock.effect_url("new")), w.end("done")), {})
    await asyncio.sleep(3)

    taken = [e for e in await scenario.events(new) if e["kind"] == "started"]
    by = scenario.worker_of(scenario.owner_pid(taken[0])) if taken else None
    scenario.observe("new run, 3s after SIGTERM", (await scenario.run(new))["status"])
    scenario.observe("taken by", by)
    scenario.mock.release("busy")
    await stopping.exited(30)
    scenario.observe("busy run", (await scenario.run(busy))["status"])
    assert taken == []
