"""
(c) A run's lease runs out while its worker is still alive: whatever that
worker's attempt writes from then on must not land (run_store.py: "every
write a worker makes while it has a run is fenced on its lease").

Two ways it happens, each played once and read by several tests:

- **Clock skew** (``taken_over``): the maintenance runs on a worker whose
  clock is ahead of the holder's by more than a lease, so it finds the
  holder's lease run out while, on the holder's own clock, it holds. It
  queues the run again, another worker takes it and finishes it; then the
  holder's model call (held by the mock until now) answers. Its run store
  writes are fenced; the tests check its activity notes, its usage rows and
  the step after.
- **Both mid-step** (``overlapped``): as above, but the holder's model call
  answers while the new attempt's own is out: the holder's ADK session
  append lands first, and the new attempt's is then refused as stale.
- **A frozen worker** (``frozen``): the holder is stopped (SIGSTOP) mid-step
  past its lease, another worker takes the run and finishes it, then the
  holder goes on (SIGCONT). Its watchdog's deadline has long passed: it
  should end the process before anything of it is written.
"""

from __future__ import annotations

import asyncio
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from .harness import workflows as w
from .harness.mock_server import LLM, MockServer
from .harness.scenario import Scenario, Services, eventually, played

#: The holder's lease in the skew scenario: it renews every third of it, after the takeover is over.
LONG_LEASE = 60.0


def _flow(scenario: Scenario, after: str) -> dict[str, Any]:
    return w.line(
        w.llm("think", "Say ok."),
        w.http("after", scenario.mock.effect_url(after)),
        w.end("done", "steps.think.output"),
    )


async def _snapshot(scenario: Scenario, run_id: str, session_id: str) -> dict[str, Any]:
    return {
        "run": await scenario.run(run_id),
        "activity": await scenario.events(run_id),
        "usage": await scenario.usage(run_id),
        "session": await scenario.session_events(session_id),
    }


def _since(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What was written between two snapshots."""
    known_activity = {event["id"] for event in before["activity"]}
    known_usage = {row["id"] for row in before["usage"]}
    known_session = {row["id"] for row in before["session"]}
    return {
        "activity": [f"{e['kind']}: {e['message']}" for e in after["activity"] if e["id"] not in known_activity],
        "usage": [f"{r['call']} {r['name']} by {r['author']}" for r in after["usage"] if r["id"] not in known_usage],
        "session": [r["id"] for r in after["session"] if r["id"] not in known_session],
        "run_changed": {
            name: (before["run"][name], after["run"][name])
            for name in ("status", "result", "state", "lease_epoch", "attempt", "finished_at")
            if before["run"][name] != after["run"][name]
        },
    }


# ----------------------------------------------------------------------------- clock skew


async def _play_taken_over(scenario: Scenario) -> dict[str, Any]:
    await scenario.warm_sessions()
    await scenario.spawn_all("w1", "w2", concurrency=1, lease=LONG_LEASE)
    scenario.mock.hold(LLM)
    run_id = await scenario.start(_flow(scenario, "after"), {})
    await scenario.mock.arrived(LLM)
    holding = await scenario.run(run_id)
    holder = scenario.owner_of(holding)
    scenario.say(f"worker {holder.name} holds the run, its model call held by the mock")

    # The maintenance, on a clock a lease and more ahead: the holder's lease ran out, as it sees it.
    done = await scenario.maintain(skew=LONG_LEASE + 5)
    assert run_id in done["recovered"], done
    taken = await scenario.until(run_id, lambda run: run["lease_epoch"] == 2, seconds=20)
    taker = scenario.owner_of(taken)
    finished = await scenario.until(run_id, "succeeded", seconds=30)
    scenario.say(f"worker {taker.name} took the run and finished it; worker {holder.name}'s attempt is still going")
    before = await _snapshot(scenario, run_id, holding["session_id"])

    scenario.mock.release(LLM)  # the holder's model call answers at last
    released = time.time()
    await eventually(lambda: _said(holder, run_id), seconds=30, what=f"worker {holder.name}'s attempt ending")
    await asyncio.sleep(2)
    after = await _snapshot(scenario, run_id, holding["session_id"])
    written = _since(before, after)

    scenario.observe("holder", f"{holder.name}, still alive: {holder.alive}")
    scenario.observe("its attempt ended", _ending(holder, run_id))
    scenario.observe("activity written after the takeover finished", written["activity"])
    scenario.observe("usage rows written after the takeover finished", written["usage"])
    # Refused here by ADK's own check (the session changed since the attempt read it), not by the
    # lease: see the overlapped scenario, where the old attempt writes first.
    scenario.observe("session events appended after the takeover finished", len(written["session"]))
    scenario.observe("run row changed after the takeover finished", written["run_changed"])
    scenario.observe("'after' step requests (its effect)", scenario.mock.count("after"))
    scenario.observe("model calls", scenario.mock.count(LLM))
    claimed = next(
        e["at"] for e in after["activity"] if e["kind"] == "resumed" and (e["attributes"] or {}).get("epoch") == 2
    )
    scenario.observe("seconds two attempts went on at once (at least)", round(released - claimed.timestamp(), 1))
    return {"finished": finished, "after": after, "written": written, "effects": scenario.mock.count("after")}


def _said(worker: Any, run_id: str) -> Any:
    async def check() -> bool:
        return _ending(worker, run_id) is not None

    return check()


def _ending(worker: Any, run_id: str) -> str | None:
    """How the worker's attempt at the run ended, as it logged it."""
    for record in worker.records():
        event = str(record.get("event") or "")
        if event.startswith(f"run {run_id}: ") or (run_id in event and "is dropped" in event):
            return event[:200]
    return None


@pytest.fixture(scope="module")
def taken_over(
    services: Services, mock: MockServer, tmp_path_factory: pytest.TempPathFactory, reports: Path
) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("taken-over")
    return played(services, mock, folder, name="c_lease_ran_out_by_clock_skew", reports=reports, play=_play_taken_over)


def test_the_run_keeps_what_the_attempt_that_took_it_wrote(taken_over: dict[str, Any]) -> None:
    run = taken_over["after"]["run"]
    assert (run["status"], run["lease_epoch"]) == ("succeeded", 2)
    assert taken_over["written"]["run_changed"] == {}


@pytest.mark.xfail(strict=True, reason="IT-4 (FINDINGS.md): RunStore.note isn't fenced on the lease")
def test_an_attempt_whose_lease_ran_out_notes_nothing(taken_over: dict[str, Any]) -> None:
    assert taken_over["written"]["activity"] == []


@pytest.mark.xfail(strict=True, reason="IT-4 (FINDINGS.md): usage rows (UsageStore.record) aren't fenced on the lease")
def test_an_attempt_whose_lease_ran_out_records_no_usage(taken_over: dict[str, Any]) -> None:
    assert taken_over["written"]["usage"] == []


def test_an_attempt_whose_lease_ran_out_takes_no_further_step(taken_over: dict[str, Any]) -> None:
    assert taken_over["effects"] == 1  # the taker's: the holder stops at its checkpoint


# ----------------------------------------------------------------------------- both attempts mid-step


async def _play_overlapped(scenario: Scenario) -> dict[str, Any]:
    """As ``taken_over``, but the old attempt's model call answers while the
    new attempt's own is still out: their writes to the run's ADK session interleave."""
    await scenario.warm_sessions()
    await scenario.spawn_all("w1", "w2", concurrency=1, lease=LONG_LEASE)
    scenario.mock.hold(LLM, times=2)
    run_id = await scenario.start(_flow(scenario, "after-overlapped"), {})
    await scenario.mock.arrived(LLM)
    holder = scenario.owner_of(await scenario.run(run_id))
    done = await scenario.maintain(skew=LONG_LEASE + 5)
    assert run_id in done["recovered"], done
    taker = scenario.owner_of(await scenario.until(run_id, lambda run: run["lease_epoch"] == 2, seconds=20))
    await scenario.mock.arrived(LLM, 2)
    scenario.say(f"both attempts' model calls are out: worker {holder.name}'s (lease gone) and worker {taker.name}'s")

    session_id = (await scenario.run(run_id))["session_id"]
    before = await scenario.session_events(session_id)
    scenario.mock.release(LLM, 1)  # the old attempt's answers first
    await eventually(lambda: _said(holder, run_id), seconds=30, what=f"worker {holder.name}'s attempt ending")
    stale = [row["id"] for row in await scenario.session_events(session_id) if row not in before]
    scenario.observe("session events the old attempt appended after its lease ran out", len(stale))
    scenario.mock.release(LLM)  # then the new attempt's
    run = await scenario.finished(run_id, seconds=30)
    scenario.observe("old attempt ended", _ending(holder, run_id))
    scenario.observe("new attempt ended", _ending(taker, run_id))
    scenario.observe("run status, error", (run["status"], (run["error"] or {}).get("message")))
    scenario.observe("'after' step requests (its effect)", scenario.mock.count("after-overlapped"))
    return {"run": run, "stale": stale}


@pytest.fixture(scope="module")
def overlapped(
    services: Services, mock: MockServer, tmp_path_factory: pytest.TempPathFactory, reports: Path
) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("overlapped")
    return played(
        services, mock, folder, name="c_lease_ran_out_mid_step_of_the_next", reports=reports, play=_play_overlapped
    )


@pytest.mark.xfail(strict=True, reason="IT-5 (FINDINGS.md): ADK session appends aren't fenced on the lease")
def test_an_attempt_whose_lease_ran_out_appends_nothing_to_the_session(overlapped: dict[str, Any]) -> None:
    assert overlapped["stale"] == []


@pytest.mark.xfail(
    strict=True,
    reason="IT-5 (FINDINGS.md): the old attempt's session append makes the new attempt's stale; the run fails",
)
def test_an_attempt_whose_lease_ran_out_cant_fail_the_attempt_that_took_its_run(overlapped: dict[str, Any]) -> None:
    run = overlapped["run"]
    assert run["status"] == "succeeded", run["error"]


# ----------------------------------------------------------------------------- a frozen worker


async def _play_frozen(scenario: Scenario) -> dict[str, Any]:
    await scenario.warm_sessions()
    await scenario.spawn_all("w1", "w2", concurrency=1)
    scenario.mock.hold(LLM)
    run_id = await scenario.start(_flow(scenario, "after-frozen"), {})
    await scenario.mock.arrived(LLM)
    holding = await scenario.run(run_id)
    holder = scenario.owner_of(holding)
    holder.signal(signal.SIGSTOP, f"SIGSTOP worker {holder.name} mid-step (its model call held)")

    await asyncio.sleep(float(holding["lease_until"].timestamp() - time.time()) + 1)
    done = await scenario.maintain()
    assert run_id in done["recovered"], done
    await scenario.until(run_id, "succeeded", seconds=30)
    before = await _snapshot(scenario, run_id, holding["session_id"])

    scenario.mock.release(LLM)  # its answer waits in the frozen worker's socket
    await asyncio.sleep(0.5)
    holder.signal(signal.SIGCONT, f"SIGCONT worker {holder.name}")
    resumed = time.time()
    try:
        code: int | None = await holder.exited(15)
    except AssertionError:
        code = None
    gone = round(time.time() - resumed, 2) if code is not None else None
    await asyncio.sleep(2)
    after = await _snapshot(scenario, run_id, holding["session_id"])
    written = _since(before, after)
    said = [line for line in holder.text().splitlines() if "watchdog" in line or "Fatal Python" in line][:3]
    scenario.observe("holder's exit status after SIGCONT", code)
    scenario.observe("seconds from SIGCONT to its end", gone)
    scenario.observe("what it said as it ended", said)
    scenario.observe("activity written after SIGCONT", written["activity"])
    scenario.observe("usage rows written after SIGCONT", written["usage"])
    scenario.observe("session events appended after SIGCONT", len(written["session"]))
    scenario.observe("run row changed after SIGCONT", written["run_changed"])
    return {"code": code, "gone": gone, "written": written}


@pytest.fixture(scope="module")
def frozen(
    services: Services, mock: MockServer, tmp_path_factory: pytest.TempPathFactory, reports: Path
) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("frozen")
    return played(services, mock, folder, name="c_lease_ran_out_while_frozen", reports=reports, play=_play_frozen)


def test_a_worker_frozen_past_its_lease_ends_as_it_goes_on(frozen: dict[str, Any]) -> None:
    # The watchdog's (70), or faulthandler's backstop (1).
    assert frozen["code"] in (70, 1) and frozen["gone"] is not None and frozen["gone"] < 5


def test_a_worker_frozen_past_its_lease_writes_nothing_as_it_goes_on(frozen: dict[str, Any]) -> None:
    written = frozen["written"]
    assert (written["activity"], written["usage"], written["session"], written["run_changed"]) == ([], [], [], {})
