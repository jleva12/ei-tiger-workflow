"""
(f) A parent run whose LLM agent calls a workflow as a tool: the workflow
runs as a run of its own (a child), queued like any other, and the tool
waits for it (``RunWorkflows.attach``: polled every half second, for
``workflow_tool_wait``, here 8 seconds), holding the parent's worker slot
meanwhile. When every slot is a waiting parent's, no worker is left to run
their children: each waits out its whole wait, answers "queued", and the
child runs after its parent has gone on without it.

The agent here (the mock's model) calls the tool once, then answers with
what the tool said.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .harness import workflows as w
from .harness.mock_server import LLM
from .harness.scenario import Scenario, epoch

WAIT = 8.0


def _child(scenario: Scenario) -> dict[str, Any]:
    flow = w.line(w.http("work", scenario.mock.effect_url("child-work")), w.end("done", '{"child": "done"}'))
    return {**flow, "id": "ag_child", "name": "Child", "version": 1}


def _parent(n: int) -> dict[str, Any]:
    agent = w.llm(
        "orchestrate", "Call the Child workflow, then say what it returned.", tools=[w.workflow_tool("ag_child", 1)]
    )
    return w.line(agent, w.end("done", "steps.orchestrate.output"), name=f"Parent {n}", agent_id=f"ag_parent{n}")


async def _waited_on(scenario: Scenario, *, concurrency: int, parents: int) -> list[dict[str, Any]]:
    """Runs ``parents`` parents on one worker of ``concurrency``: what each one's tool answered, and when its child ran."""
    await scenario.warm_sessions()
    await scenario.spawn("w1", concurrency=concurrency, HYBRID_ADK_WORKFLOWS__WORKFLOW_TOOL_WAIT=WAIT)
    saved = {"ag_child@1": _child(scenario)}
    run_ids = [await scenario.start(_parent(n), {}, saved=saved) for n in range(parents)]
    finished = [await scenario.finished(run_id, seconds=WAIT * 3 + 30) for run_id in run_ids]
    answered = {}  # the tool's answer, by child: the model's second request quotes it
    for hit in scenario.mock.of(LLM):
        last = ((hit.body or {}).get("messages") or [{}])[-1]
        if last.get("role") == "tool":
            said = json.loads(last["content"]) if isinstance(last.get("content"), str) else last.get("content")
            said = said.get("payload", said) if isinstance(said, dict) else said
            answered[said["run_id"]] = (hit.at, said)
    found = []
    for parent in finished:
        [child] = await scenario.store.children(parent["id"])
        scenario.watched.append(child["id"])
        child = await scenario.finished(child["id"], seconds=WAIT * 3 + 30)
        claimed = next(e["at"] for e in await scenario.events(child["id"]) if e["kind"] == "started")
        at, said = answered[child["id"]]
        found.append(
            {
                "parent": parent["status"],
                "tool answered": said["status"],
                "seconds the tool waited": round(at - epoch(child["created_at"]), 1),
                "seconds the child was queued": round(epoch(claimed) - epoch(child["created_at"]), 1),
                "child ran while its parent waited": epoch(claimed) < at,
                "child": child["status"],
            }
        )
    for n, each in enumerate(found):
        scenario.observe(f"parent {n}", each)
    return found


async def test_one_parent_on_a_worker_with_two_slots_gets_its_childs_result(scenario: Scenario) -> None:
    found = await _waited_on(scenario, concurrency=2, parents=1)
    assert [(each["tool answered"], each["child ran while its parent waited"]) for each in found] == [
        ("succeeded", True)
    ]


@pytest.mark.xfail(
    strict=True, reason="IT-6 (FINDINGS.md): a parent waiting on its child holds the slot the child needs"
)
async def test_one_parent_on_a_worker_with_one_slot_gets_its_childs_result(scenario: Scenario) -> None:
    found = await _waited_on(scenario, concurrency=1, parents=1)
    assert [(each["tool answered"], each["child ran while its parent waited"]) for each in found] == [
        ("succeeded", True)
    ]


@pytest.mark.xfail(strict=True, reason="IT-6 (FINDINGS.md): parents waiting on their children hold every slot")
async def test_two_parents_on_a_worker_with_two_slots_get_their_childrens_results(scenario: Scenario) -> None:
    found = await _waited_on(scenario, concurrency=2, parents=2)
    assert [(each["tool answered"], each["child ran while its parent waited"]) for each in found] == [
        ("succeeded", True)
    ] * 2
