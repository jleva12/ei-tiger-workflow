"""
(i) Two maintenances recover one run: what one found (``interrupted``) goes
stale while it reads Redis for what the run's worker kept. Meanwhile the
other recovers the run, a worker takes it again, and that worker goes too,
having kept its failure. The first maintenance's writes are fenced on the
taking it found (``lease_epoch``): it neither queues the later taking again
nor fails it with what the earlier one kept, and the next maintenance fails
the run as its later worker meant.

No worker processes: the workers that go are claims of the run store's,
whose leases run out; the maintenances run here, on the suite's MySQL and
Redis, the stale one held between its read and its writes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from forge_async_worker.jobs import AdkRunJobs

from .harness import workflows as w
from .harness.scenario import Scenario

#: Seconds each gone worker's lease lasts.
LEASE = 1.0


async def _gone(scenario: Scenario, run_id: str, owner: str) -> dict[str, Any]:
    """A worker takes the run, and goes: its lease runs out."""
    taken = await scenario.store.claim(run_id, owner=owner, lease_seconds=LEASE)
    assert taken is not None, f"{owner} couldn't take run {run_id[:8]}"
    scenario.say(f"{owner} took run {run_id[:8]} (epoch {taken['lease_epoch']}) and went")
    await asyncio.sleep(LEASE + 0.5)
    return taken


async def test_a_stale_recovery_leaves_a_later_taking_to_its_own(scenario: Scenario) -> None:
    run_id = await scenario.start(w.line(w.end("done")), {}, queue=False)
    first = await _gone(scenario, run_id, "gone-1")
    failure = {"message": "Refund declined", "category": "failed"}
    jobs = AdkRunJobs(store=scenario.store, queue=scenario.queue, runner=None)  # type: ignore[arg-type]  # it runs no job
    reads = scenario.queue.kept_failure

    async def meanwhile(read_run: str, epoch: int) -> dict[str, Any] | None:
        kept = await reads(read_run, epoch)
        scenario.queue.kept_failure = reads  # type: ignore[method-assign]  # the other reads as it would
        # As the first maintenance reads Redis: the other recovers the run, a worker takes
        # it again and goes too, having failed it and kept the failure it couldn't write.
        assert (await scenario.maintain())["recovered"] == [run_id]
        second = await _gone(scenario, run_id, "gone-2")
        await scenario.queue.keep_failure(
            run_id, second["lease_epoch"], {"error": failure, "final": True, "epoch": second["lease_epoch"]}
        )
        scenario.say(f"gone-2 kept its failure for epoch {second['lease_epoch']}")
        return kept

    scenario.queue.kept_failure = meanwhile  # type: ignore[method-assign]
    try:
        stale = [run for run in await scenario.store.interrupted() if run["id"] == run_id]
        scenario.observe("the first maintenance found", f"epoch {stale[0]['lease_epoch']}" if stale else "nothing")
        assert [run["lease_epoch"] for run in stale] == [first["lease_epoch"]]
        done = {name: [] for name in ("recovered", "failed", "deferred", "abandoned", "queued", "timed_out")}
        await jobs._recover(stale[0], done)  # what one pass of its maintenance does with it
    finally:
        scenario.queue.kept_failure = reads  # type: ignore[method-assign]
    scenario.say(f"the first maintenance, from what it found: {({k: v for k, v in done.items() if v}) or 'nothing'}")

    after = await scenario.run(run_id)
    scenario.observe("the first maintenance did", {name: ids for name, ids in done.items() if ids} or "nothing")
    scenario.observe(
        "the run after it",
        f"{after['status']}, epoch {after['lease_epoch']}, {after['lease_owner']}, "
        f"{after['interruptions']} interruptions",
    )
    assert not any(done.values()), done
    assert (after["status"], after["lease_owner"], after["lease_epoch"]) == ("running", "gone-2", 2)
    assert after["interruptions"] == 1  # the other maintenance's recovery only

    # The next maintenance finds the later taking, and fails the run as its worker meant.
    assert (await scenario.maintain())["failed"] == [run_id]
    ended = await scenario.run(run_id)
    scenario.observe("it ended", f"{ended['status']}: {ended['error']['message']}")
    assert (ended["status"], ended["error"]["message"]) == ("failed", "Refund declined")
    assert await scenario.queue.kept_failure(run_id, 2) is None  # forgotten once written
