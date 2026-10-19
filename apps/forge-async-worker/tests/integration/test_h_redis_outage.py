"""
(h) Redis goes away for about two minutes (FORGE_IT_REDIS_OUTAGE_SECONDS,
default 120) while runs are going, then comes back (stopped and started:
its append-only file keeps the queue, as a restarted Redis would). The runs
live in MySQL, so the outage should only delay them:

- one in flight (its step held by the mock), answered during the outage:
  it ends in MySQL, though SAQ can't mark its job done;
- one paused at an approval, decided during the outage: the admin API
  can't queue its job (it carries on: the maintenance finds it);
- one waiting out a delay that ends during the outage: its job is
  scheduled in Redis;
- one started during the outage: its job can't be queued either.

The workers run their own maintenance, every five seconds, a run queued and
untaken for ten seconds counting as stalled (the scaled two minutes).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from .harness import workflows as w
from .harness.mock_server import MockServer
from .harness.scenario import Scenario, Services, WorkerProcess, played

OUTAGE = float(os.environ.get("FORGE_IT_REDIS_OUTAGE_SECONDS") or 120)
#: Seconds after Redis is back by which every run should have gone on to its end.
RECOVERY = 30.0


def _compose(services: Services, *args: str) -> None:
    subprocess.run([*services.compose, *args], check=True, capture_output=True)


def _cpu_seconds(worker: WorkerProcess) -> float:
    """The CPU time the worker process has used, as ps says it (user and system)."""
    found = subprocess.run(["ps", "-o", "time=", "-p", str(worker.pid)], capture_output=True, text=True).stdout.strip()
    minutes, _, seconds = found.rpartition(":")
    hours, _, minutes = minutes.rpartition(":")
    return float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0)


async def _queue_quietly(scenario: Scenario, run_id: str) -> str:
    """Queues the run's job as the admin API does: should Redis not take it, the maintenance will."""
    try:
        async with asyncio.timeout(5):
            await scenario.enqueue(run_id)
    except Exception as error:
        return f"not queued ({type(error).__name__})"
    return "queued"


async def _play(scenario: Scenario) -> dict[str, Any]:
    services = scenario.services
    await scenario.warm_sessions()
    workers = await scenario.spawn_all("w1", "w2", concurrency=4, maintenance=5, stalled_after=10)
    url = scenario.mock.effect_url
    scenario.mock.hold("in-flight")
    in_flight = await scenario.start(
        w.line(w.http("call", url("in-flight")), w.end("done", "steps.call.output.body")), {}
    )
    gate = [w.start(), w.approval("review"), w.http("call", url("approved")), w.end("done", "steps.call.output.body")]
    gated = w.document(gate, [("start", "next", "review"), ("review", "approved", "call"), ("call", "success", "done")])
    paused = await scenario.start(gated, {})
    delay = w.node("pause", "delay", {"amount": 20, "unit": "seconds"})
    waiting = await scenario.start(
        w.line(delay, w.http("call", url("woken")), w.end("done", "steps.call.output.body")), {}
    )
    await scenario.mock.arrived("in-flight")
    pause = (await scenario.until(paused, "paused", seconds=30))["pause"]
    await scenario.until(waiting, "waiting", seconds=30)
    cpu = {worker.name: _cpu_seconds(worker) for worker in workers}

    went = time.time()
    scenario.say(f"Redis stopped for {OUTAGE:g}s")
    _compose(services, "stop", services.redis_service)
    try:
        await asyncio.sleep(10)
        scenario.mock.release("in-flight")
        scenario.say("the in-flight step answered")
        await asyncio.sleep(5)
        await scenario.store.decide(paused, request_id=pause["id"], approved=True, comment="", actor=w.DECIDER)
        scenario.say(f"decided the paused run: its job {await _queue_quietly(scenario, paused)}")
        await asyncio.sleep(5)
        started = await scenario.start(
            w.line(w.http("call", url("started-during")), w.end("done", "steps.call.output.body")), {}, queue=False
        )
        scenario.say(f"started a run during the outage: its job {await _queue_quietly(scenario, started)}")
        await asyncio.sleep(max(0.0, went + OUTAGE - time.time()))
        alive_during = {worker.name: worker.alive for worker in workers}
        used = {worker.name: round(_cpu_seconds(worker) - cpu[worker.name], 1) for worker in workers if worker.alive}
    finally:
        _compose(services, "start", services.redis_service)
    back = time.time()
    scenario.say("Redis started again")

    runs = {"in flight": in_flight, "decided during": paused, "woken during": waiting, "started during": started}
    ended: dict[str, dict[str, Any]] = {}
    for what, run_id in runs.items():
        try:
            ended[what] = await scenario.finished(run_id, seconds=RECOVERY + 60)
        except AssertionError:
            ended[what] = await scenario.run(run_id)
    effects = {name: scenario.mock.count(name) for name in ("in-flight", "approved", "woken", "started-during")}
    late = {
        what: round(run["finished_at"].timestamp() - back, 1) if run.get("finished_at") else None
        for what, run in ended.items()
    }
    lines = {worker.name: len(worker.text().splitlines()) for worker in workers}
    scenario.observe("workers alive during the outage", alive_during)
    scenario.observe("CPU seconds each used during the outage", used)
    scenario.observe("log lines each wrote, all told", lines)
    scenario.observe("workers alive after", {worker.name: worker.alive for worker in workers})
    scenario.observe("runs", {what: (run["status"], run["attempt"]) for what, run in ended.items()})
    scenario.observe("seconds after Redis was back each ended", late)
    scenario.observe("effects", effects)
    return {"ended": ended, "late": late, "effects": effects, "alive": alive_during, "workers": workers, "used": used}


@pytest.fixture(scope="module")
def outage(
    services: Services, mock: MockServer, tmp_path_factory: pytest.TempPathFactory, reports: Path
) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("redis-outage")
    return played(services, mock, folder, name="h_redis_outage", reports=reports, play=_play)


@pytest.mark.parametrize("what", ["in flight", "decided during", "woken during", "started during"])
def test_runs_go_on_to_their_end_once_redis_is_back(outage: dict[str, Any], what: str) -> None:
    run = outage["ended"][what]
    assert run["status"] == "succeeded", (run["status"], run["error"])
    assert outage["late"][what] is not None and outage["late"][what] < RECOVERY


def test_each_runs_effect_happens_once(outage: dict[str, Any]) -> None:
    assert outage["effects"] == {"in-flight": 1, "approved": 1, "woken": 1, "started-during": 1}


def test_workers_live_through_a_redis_outage(outage: dict[str, Any]) -> None:
    assert all(outage["alive"].values())


def test_workers_dont_spin_while_redis_is_away(outage: dict[str, Any]) -> None:
    # A tenth of the outage's seconds in CPU, at most: retrying Redis, not spinning on it.
    assert all(used < OUTAGE / 10 for used in outage["used"].values()), outage["used"]
