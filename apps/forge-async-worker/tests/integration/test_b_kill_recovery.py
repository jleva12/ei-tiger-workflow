"""
(b) ``kill -9`` of the worker process mid-step: the run recovers once its
lease runs out, through the maintenance (here the workers' own, every two
seconds), on the other worker.

What the run's docs promise (runs.py, "Interrupted"): the invocation carries
on where it stopped. A step that was running runs again when that's safe (a
GET); one whose effect may not be (a POST, a Bash command) isn't run again:
what it left running is stopped, and it takes its Error way, or with none the
run fails, saying it may have run. The tests hold the run to that: going on
to its end, or failing closed (failing, saying where it was cut off), its
effect had once; never ending as if the step had answered when it didn't
(CURRENT_STATE.md IT-2), nor the killed attempt's command going on beside the
run's next attempt (IT-3).
"""

from __future__ import annotations

import asyncio
import time

from .harness import workflows as w
from .harness.scenario import LEASE_SECONDS, Scenario

#: Seconds a recovery may take: the lease running out, a maintenance, a claim.
RECOVERY = LEASE_SECONDS + 15


def _went_on_to_its_end(run: dict) -> bool:
    """Succeeded with what its End hands on, or failed saying so: never succeeded with nothing."""
    if run["status"] == "succeeded":
        return run["result"] is not None
    return run["status"] == "failed" and bool((run["error"] or {}).get("message"))


async def test_a_run_killed_mid_post_recovers_and_fails_closed_without_sending_it_again(scenario: Scenario) -> None:
    await scenario.spawn_all("w1", "w2", maintenance=2)
    flow = w.line(
        w.http("charge", scenario.mock.effect_url("charge"), body='{"amount": 42}'),
        w.end("done", "steps.charge.output.body"),
    )
    scenario.mock.hold("charge")
    run_id = await scenario.start(flow, {})
    await scenario.mock.arrived("charge")
    victim = scenario.owner_of(await scenario.run(run_id))
    victim.kill()
    killed = time.time()
    await victim.exited(10)
    scenario.mock.release("charge")  # answered to nobody: the charge happened all the same

    run = await scenario.finished(run_id, seconds=RECOVERY + 30)

    kinds = await scenario.kinds(run_id)
    scenario.observe("status, attempt, lease epoch", (run["status"], run["attempt"], run["lease_epoch"]))
    scenario.observe("charge requests (the effect)", scenario.mock.count("charge"))
    scenario.observe("result", run["result"])
    scenario.observe("activity", kinds)
    recovered = next(e for e in await scenario.events(run_id) if e["kind"] == "recovered")
    scenario.observe("seconds from the kill to its recovery", round(recovered["at"].timestamp() - killed, 1))
    assert "recovered" in kinds and run["lease_epoch"] == 2
    assert _went_on_to_its_end(run), f"it {run['status']} with result {run['result']!r}"
    # A POST cut off may have taken effect (it did: the charge went through), so it isn't sent again.
    assert run["status"] == "failed" and "may have taken effect" in run["error"]["message"]
    assert scenario.mock.count("charge") == 1


async def test_a_run_killed_mid_bash_step_recovers_and_its_command_dies_with_its_worker(scenario: Scenario) -> None:
    await scenario.spawn_all("w1", "w2", maintenance=2)
    url = scenario.mock.url
    command = (
        f"curl -s -X POST '{url}/effect/bash-began?pid='$$ >/dev/null; sleep 4; "
        f"curl -s -X POST '{url}/effect/bash-ended?pid='$$ >/dev/null; echo finished"
    )
    flow = w.line(w.bash("work", command), w.end("done", "steps.work.output.stdout"), allow_file_system=True)
    run_id = await scenario.start(flow, {})
    [began] = await scenario.mock.arrived("bash-began")
    victim = scenario.owner_of(await scenario.run(run_id))
    victim.kill()
    killed = time.time()
    await victim.exited(10)

    run = await scenario.finished(run_id, seconds=RECOVERY + 30)
    await asyncio.sleep(6)  # whatever the killed attempt left running has finished by now

    first_pid = began.path.partition("pid=")[2]
    ended = scenario.mock.of("bash-ended")
    after_death = [hit for hit in ended if hit.path.endswith(f"pid={first_pid}") and hit.at > killed]
    scenario.observe("status, attempt, lease epoch", (run["status"], run["attempt"], run["lease_epoch"]))
    scenario.observe("commands begun", scenario.mock.count("bash-began"))
    scenario.observe("commands ended", len(ended))
    scenario.observe("effects of the killed attempt's command after its worker died", len(after_death))
    scenario.observe("result", run["result"])
    assert _went_on_to_its_end(run), f"it {run['status']} with result {run['result']!r}"
    assert after_death == []
    # Cut off, it may have run: it isn't run again.
    assert run["status"] == "failed" and "may have run" in run["error"]["message"]
    assert scenario.mock.count("bash-began") == 1
