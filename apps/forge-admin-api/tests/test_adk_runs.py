"""Starting ADK workflow runs (``adk_runs``), without MySQL, MongoDB or Redis:
the input and build checks, the saved ADK workflows a run carries, what's
submitted, and answers to a run's questions."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from forge_admin.adk_runs import (
    MAX_ANSWER,
    AdkRunError,
    check_input,
    checked_answer,
    prepare_run,
    saved_documents,
    start_adk_run,
)

EXAMPLE = (
    Path(__file__).parents[3]
    / "packages/python/tasks/adk-workflows/tests/fixtures/example.agent.json"
)
ORG = "3f6c0000-0000-4000-8000-000000000001"


def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def node(
    node_id: str, kind: str, config: dict[str, Any], name: str = ""
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": name or node_id.title(),
        "config": config,
        "outputs": ["next"],
    }


def agent(agent_id: str, *nodes: dict[str, Any]) -> dict[str, Any]:
    """An ADK workflow of a start and the nodes given, one after the other."""
    chain = [node("start", "start", {"input_schema": {}}), *nodes]
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": f"Agent {agent_id}",
        "nodes": chain,
        "edges": [
            {
                "id": f"{a['id']}->{b['id']}",
                "source": a["id"],
                "source_output": "next",
                "target": b["id"],
            }
            for a, b in zip(chain, chain[1:], strict=False)
        ],
    }


def saved(node_id: str, agent_id: str) -> dict[str, Any]:
    return node(node_id, "saved", {"agent": agent_id})


def record(document: dict[str, Any], revision: int = 3) -> dict[str, Any]:
    return {
        "_id": document["id"],
        "organization_id": ORG,
        "revision": revision,
        "document": document,
    }


def finder(*documents: dict[str, Any]) -> Any:
    found: list[str] = []
    records = {document["id"]: record(document) for document in documents}

    async def find(agent_id: str) -> dict[str, Any] | None:
        found.append(agent_id)
        return records.get(agent_id)

    find.found = found  # type: ignore[attr-defined]
    return find


class FakeQueue:
    def __init__(self) -> None:
        self.adk_runs: list[dict[str, Any]] = []

    async def run_adk_workflow(self, **submission: Any) -> None:
        self.adk_runs.append(submission)


def test_the_input_must_fit_the_starts_schema() -> None:
    document = example()
    check_input(document, {"message": "It crashes", "customer_id": "c1"})
    with pytest.raises(AdkRunError) as refused:
        check_input(document, {"message": 7})
    assert str(refused.value) == (
        "The input doesn't fit the start: input: 'customer_id' is a required "
        "property; message: 7 is not of type 'string'"
    )
    # A start that declares nothing takes anything.
    check_input(agent("ag_any0000001"), None)


def test_an_adk_workflow_without_a_start_or_with_a_broken_schema_is_refused() -> None:
    with pytest.raises(AdkRunError, match="The ADK workflow has no start"):
        check_input({"nodes": []}, {})
    broken = agent("ag_broken00001")
    broken["nodes"][0]["config"]["input_schema"] = {"type": "nothing"}
    with pytest.raises(
        AdkRunError, match="The start's input schema isn't a JSON Schema"
    ):
        check_input(broken, {})


def test_a_run_carries_every_saved_adk_workflow_it_runs() -> None:
    leaf = agent(
        "ag_leaf000001",
        node("add", "transform", {"expression": "1", "output_schema": {}}),
    )
    middle = agent("ag_middle0001", saved("leaf", "ag_leaf000001"))
    top = agent(
        "ag_top0000001",
        saved("middle", "ag_middle0001"),
        saved("leaf_again", "ag_leaf000001"),
    )
    find = finder(leaf, middle, top)
    found = asyncio.run(saved_documents(top, find))
    assert list(found) == ["ag_middle0001", "ag_leaf000001"]
    assert found["ag_leaf000001"]["nodes"] == leaf["nodes"]
    # Each is read once.
    assert find.found == ["ag_middle0001", "ag_leaf000001"]


def test_a_document_that_doesnt_build_is_refused_with_the_reason() -> None:
    missing = agent("ag_top0000001", saved("gone", "ag_nothing001"))
    with pytest.raises(AdkRunError, match="Node 'Gone': the organization has no agent"):
        asyncio.run(prepare_run(record(missing), None, finder(missing)))
    looping = agent("ag_top0000001", saved("again", "ag_top0000001"))
    with pytest.raises(AdkRunError, match="would run itself"):
        asyncio.run(prepare_run(record(looping), None, finder(looping)))
    # And the input first.
    with pytest.raises(AdkRunError, match="doesn't fit the start"):
        asyncio.run(prepare_run(record(example()), {}, finder()))


def test_a_run_is_submitted_with_its_documents_session_and_member() -> None:
    leaf = agent(
        "ag_leaf000001", node("done", "end", {"outcome": "succeeded", "result": ""})
    )
    top = agent("ag_top0000001", saved("leaf", "ag_leaf000001"))
    carried = asyncio.run(prepare_run(record(top), {"n": 1}, finder(leaf, top)))
    queue = FakeQueue()
    started = asyncio.run(
        start_adk_run(
            queue,  # type: ignore[arg-type]
            record(top),
            saved=carried,
            input={"n": 1},
            run_as="member-1",
            run_as_name="Ada Lovelace",
            trigger={"type": "manual", "by": "member-1"},
        )
    )
    [submitted] = queue.adk_runs
    session_id = started["session_id"]
    assert started["queue"] == "adk_workflows"
    assert started["key"].startswith("adk_workflows.run:ag_top0000001:")
    assert submitted["key"] == started["key"]
    assert submitted["tenant_id"] == ORG
    assert submitted["labels"] == {
        "adk_workflow": "ag_top0000001",
        "adk_session": session_id,
    }
    assert submitted["requested_by"] == {
        "id": "member-1",
        "display_name": "Ada Lovelace",
    }
    assert submitted["payload"] == {
        "tenant_id": ORG,
        "agent_id": "ag_top0000001",
        "revision": 3,
        "name": "Agent ag_top0000001",
        "document": top,
        "saved": {"ag_leaf000001": leaf},
        "input": {"n": 1},
        "session_id": session_id,
        "run_as": "member-1",
        "run_as_name": "Ada Lovelace",
        "trigger": {"type": "manual", "by": "member-1"},
    }
    # Every run has a session of its own.
    again = asyncio.run(
        start_adk_run(
            queue,  # type: ignore[arg-type]
            record(top),
            saved=carried,
            input=None,
            run_as="member-1",
            run_as_name="Ada Lovelace",
            trigger={"type": "manual", "by": "member-1"},
        )
    )
    assert again["session_id"] != session_id and again["key"] != started["key"]


QUESTION = {
    "kind": "human_input",
    "step": "check_fix",
    "response_schema": {
        "type": "object",
        "required": ["send"],
        "properties": {"send": {"type": "boolean"}, "changes": {"type": "string"}},
    },
}


def test_an_answer_must_fit_what_the_question_asks() -> None:
    assert checked_answer(QUESTION, {"send": True, "changes": "é"}) == (
        '{"send":true,"changes":"é"}'
    )
    with pytest.raises(AdkRunError) as refused:
        checked_answer(QUESTION, {"send": "yes", "changes": 3})
    assert str(refused.value) == (
        "The answer doesn't fit what the question asks: changes: 3 is not of type "
        "'string'; send: 'yes' is not of type 'boolean'"
    )
    with pytest.raises(AdkRunError, match="answer: 'send' is a required property"):
        checked_answer(QUESTION, {})
    # A question without a schema takes any answer.
    assert checked_answer({"kind": "human_input"}, "fine") == '"fine"'
    assert checked_answer({**QUESTION, "response_schema": {}}, None) == "null"


def test_an_answer_fits_a_decisions_comment() -> None:
    fits = "x" * (MAX_ANSWER - 2)
    assert checked_answer({}, fits) == json.dumps(fits)
    with pytest.raises(AdkRunError, match="the most it can be is 4,000"):
        checked_answer({}, fits + "x")
