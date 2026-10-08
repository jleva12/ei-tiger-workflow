"""Starting ADK workflow runs (``adk_workflows.runs``), without MySQL, MongoDB or Redis
(the run store on SQLite): the input and build checks, the saved ADK
workflows a run carries, what's kept and queued, and answers to a run's
questions."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from forge_task_adk_workflows.run_store import RunStore, metadata
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.queue import EmbeddingError
from forge_admin.adk_workflows.runs import (
    MAX_ANSWER,
    AdkRunError,
    PreparedRun,
    check_input,
    checked_answer,
    prepare_run,
    saved_documents,
    start_adk_run,
)

EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/adk-workflows/tests/fixtures/example.agent.json"
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
        kept = records.get(agent_id)
        return kept["document"] if kept is not None else None

    find.found = found  # type: ignore[attr-defined]
    return find


class FakeQueue:
    def __init__(self, refuse: bool = False) -> None:
        self.queued: list[str] = []
        self.refuse = refuse

    async def run_adk(self, run_id: str) -> None:
        if self.refuse:
            raise EmbeddingError("Connection refused")
        self.queued.append(run_id)


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


async def started(queue: FakeQueue, *runs: dict[str, Any]) -> list[dict[str, Any]]:
    """Start a run of each record given, on a run store of its own; return
    each run as the store keeps it."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    store = RunStore(engine)
    kept = []
    try:
        for run in runs:
            made = await start_adk_run(
                store,
                queue,  # type: ignore[arg-type]
                run["record"],
                prepared=PreparedRun(run["saved"]),
                input=run["input"],
                run_as="member-1",
                run_as_name="Ada Lovelace",
                trigger={"type": "manual", "by": "member-1"},
            )
            kept.append(await store.get(made["id"]))
    finally:
        await engine.dispose()
    return kept


def test_a_run_is_kept_with_its_documents_session_and_member_and_queued() -> None:
    leaf = agent(
        "ag_leaf000001", node("done", "end", {"outcome": "succeeded", "result": ""})
    )
    top = agent("ag_top0000001", saved("leaf", "ag_leaf000001"))
    carried = asyncio.run(prepare_run(record(top), {"n": 1}, finder(leaf, top)))
    queue = FakeQueue()
    one = {"record": record(top), "saved": carried.saved, "input": {"n": 1}}
    run, again = asyncio.run(started(queue, one, {**one, "input": None}))
    session_id = run["session_id"]
    assert (run["status"], run["attempt"]) == ("queued", 1)
    assert (run["organization_id"], run["agent_id"]) == (ORG, "ag_top0000001")
    assert (run["agent_name"], run["revision"]) == ("Agent ag_top0000001", 3)
    assert (run["requested_by"], run["requested_by_name"]) == (
        "member-1",
        "Ada Lovelace",
    )
    assert run["payload"] == {
        "tenant_id": ORG,
        "agent_id": "ag_top0000001",
        "revision": 3,
        "version": None,
        "name": "Agent ag_top0000001",
        "document": top,
        "saved": {"ag_leaf000001": leaf},
        "chat_agents": {},
        "knowledge_bases": {},
        "input": {"n": 1},
        "files": [],
        "session_id": session_id,
        "run_as": "member-1",
        "run_as_name": "Ada Lovelace",
        "trigger": {"type": "manual", "by": "member-1"},
    }
    # A job takes each; every run has a session of its own.
    assert queue.queued == [run["id"], again["id"]]
    assert again["session_id"] != session_id


def test_a_run_whose_job_cant_be_queued_is_kept_queued() -> None:
    made = agent(
        "ag_top0000001", node("done", "end", {"outcome": "succeeded", "result": ""})
    )
    queue = FakeQueue(refuse=True)
    [run] = asyncio.run(
        started(queue, {"record": record(made), "saved": {}, "input": None})
    )
    assert run["status"] == "queued"
    assert queue.queued == []


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


def llm_node(node_id: str, **config: Any) -> dict[str, Any]:
    return node(
        node_id,
        "llm",
        {"instruction": "Help.", "model": {"provider": "", "name": ""}, **config},
    )


def tool(tool_id: str, kind: str, **config: Any) -> dict[str, Any]:
    return {"id": tool_id, "kind": kind, "name": tool_id.title(), "config": config}


class FakeResources:
    """The organization's agents (one published at v2, with a draft),
    knowledge bases and MCP servers, as a run would find them."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, Any]] = []

    async def chat_agent(self, agent_id: str, version: Any) -> dict[str, Any]:
        self.asked.append((agent_id, version))
        if agent_id != "ca_support":
            raise LookupError(f"the organization has no agent {agent_id}")
        if version not in (None, 2, "draft"):
            raise LookupError(f"Support has no version {version}")
        return {"id": agent_id, "version": version or 2}

    async def knowledge_bases(self, ids: list[str]) -> dict[str, dict[str, str]]:
        known = {"kb_hr": {"name": "HR", "description": "Policies"}}
        return {kb: known[kb] for kb in ids if kb in known}

    async def mcp_servers(self, ids: list[str]) -> set[str]:
        return {server for server in ids if server == "srv_docs"}


def test_a_run_carries_the_agents_and_knowledge_bases_its_llm_agents_use() -> None:
    called = agent("ag_called0001", node("done", "end", {"outcome": "succeeded"}))
    top = agent(
        "ag_top0000001",
        llm_node("support", source="agent", agent="ca_support", version=None),
        llm_node(
            "triage",
            tools=[
                tool("kb", "knowledge_base", knowledge_bases=["kb_hr"]),
                tool("docs", "mcp", server="srv_docs"),
                tool("helper", "saved_agent", agent="ca_support"),
                tool("flow", "adk_workflow", workflow="ag_called0001"),
            ],
            sub_agents=[
                {
                    "id": "drafter",
                    "kind": "llm",
                    "name": "Drafter",
                    "config": {
                        "tools": [
                            tool("pinned", "knowledge_base", knowledge_bases=["kb_hr"])
                        ]
                    },
                }
            ],
        ),
    )
    resources = FakeResources()
    prepared = asyncio.run(
        prepare_run(record(top), None, finder(called, top), resources)  # type: ignore[arg-type]
    )
    # The workflow called as a tool comes along like a saved one.
    assert list(prepared.saved) == ["ag_called0001"]
    assert prepared.chat_agents == {"ca_support": {"id": "ca_support", "version": 2}}
    assert resources.asked == [("ca_support", None)]
    assert prepared.knowledge_bases == {
        "kb_hr": {"name": "HR", "description": "Policies"}
    }

    def refused(document: dict[str, Any], match: str) -> None:
        with pytest.raises(AdkRunError, match=match):
            asyncio.run(
                prepare_run(record(document), None, finder(document), FakeResources())
            )  # type: ignore[arg-type]

    refused(
        agent("ag_a000000001", llm_node("one", source="agent", agent="ca_gone")),
        "Node 'One': the organization has no agent ca_gone",
    )
    refused(
        agent(
            "ag_a000000001",
            llm_node("one", source="agent", agent="ca_support", version=7),
        ),
        "Node 'One': Support has no version 7",
    )
    refused(
        agent("ag_a000000001", llm_node("one", source="agent", agent="")),
        "Node 'One': choose the agent from the Agents page it uses",
    )
    refused(
        agent(
            "ag_a000000001",
            llm_node(
                "one", tools=[tool("kb", "knowledge_base", knowledge_bases=["kb_x"])]
            ),
        ),
        "Node 'One', tool 'Kb': the organization has no knowledge base kb_x",
    )
    refused(
        agent(
            "ag_a000000001", llm_node("one", tools=[tool("m", "mcp", server="srv_x")])
        ),
        "Node 'One', tool 'M': the organization has no MCP server srv_x",
    )
    # Without the organization's resources, a run that uses them can't start.
    with pytest.raises(AdkRunError, match="isn't set up here"):
        asyncio.run(prepare_run(record(top), None, finder(called, top)))
