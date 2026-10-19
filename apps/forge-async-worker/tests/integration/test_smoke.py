"""The harness itself: one worker process runs an HTTP step's workflow to its end."""

from __future__ import annotations

from .harness import workflows as w
from .harness.scenario import Scenario


async def test_a_worker_runs_a_workflow_to_its_end(scenario: Scenario) -> None:
    await scenario.spawn("w1")
    flow = w.line(
        w.http("call", scenario.mock.effect_url("smoke"), body='{"hello": "there"}'),
        w.end("done", "steps.call.output.body"),
    )
    run_id = await scenario.start(flow, {})

    run = await scenario.finished(run_id, seconds=60)

    assert run["status"] == "succeeded", run["error"]
    assert scenario.mock.count("smoke") == 1
    assert run["result"]["name"] == "smoke"
    assert (await scenario.kinds(run_id))[:2] == ["created", "started"]
