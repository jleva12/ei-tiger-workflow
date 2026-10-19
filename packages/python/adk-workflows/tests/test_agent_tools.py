"""
LLM agents' tools and agents from the Agents page in a run, as the worker
runs them (the task's run job, its control in memory): the Agents builder's
tools built when they're first used, an agent used whole, a tool a person
confirms pausing the run, what a run opened closed when it stops, and calls
its worker's stop cut off answered before the run goes on (a workflow's with
the run it started).
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_agent_runtime import RuntimeServices, code_executor
from forge_agent_runtime.access import Caller, caller_json
from forge_common.adk.models import ProviderModels
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import DatabaseSessionService
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from forge_task_adk_workflows.graph import agent_tools
from forge_task_adk_workflows.graph.agent_tools import (
    AgentServices,
    DocumentsSearch,
    RunWorkflows,
    SnapshotKnowledgeBases,
    child_run_id,
    model_error,
)
from forge_task_adk_workflows.graph.errors import AgentBuildError
from forge_task_adk_workflows.graph.graph import build_agent
from forge_task_adk_workflows.runs import APP_NAME, CUT_OFF, CUT_OFF_KEY, NO_ANSWER, STATE_KEY, RunPayload
from forge_task_adk_workflows.steps import run_steps
from forge_tasks.control import AwaitingDecision, Decision
from forge_tasks.errors import TransientError
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm, blocked_for_safety
from .test_graph import end, llm_config, node
from .test_task import ORGANIZATION, PROVIDERS, Control, Worker, line, overloaded, payload, worker


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(value: str) -> types.Part:
    return types.Part(text=value)


def tool(tool_id: str, kind: str, name: str, **config: Any) -> dict[str, Any]:
    return {"id": tool_id, "kind": kind, "name": name, "config": config}


ORDERS = tool(
    "tool_orders",
    "http_tool",
    "Look up order",
    description="Finds an order by its number.",
    method="GET",
    url="https://api.example.com/orders/{order_id}",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
)


class Searches:
    """Knowledge base search standing in for the documents task's."""

    def __init__(self) -> None:
        self.asked: list[tuple[list[str], str, int]] = []

    async def __call__(self, knowledge_bases: Any, query: str, limit: int) -> list[dict[str, Any]]:
        self.asked.append((list(knowledge_bases), query, limit))
        self.names = dict(knowledge_bases)
        return [
            {"knowledge_base": "HR", "document": "Leave.pdf", "section": "Annual", "text": "25 days.", "score": 0.9}
        ]


class Docs(BaseToolset):
    """An MCP server's toolset, standing in for the organization's: one tool, and whether it was closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

        def search_docs(q: str) -> dict[str, Any]:
            """Searches the docs."""
            return {"found": f"Docs about {q}"}

        self.tool = FunctionTool(search_docs)

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        return [self.tool]

    async def close(self) -> None:
        self.closed = True


class Servers:
    """The organization's MCP servers: ``srv_docs`` only."""

    def __init__(self) -> None:
        self.made: list[Docs] = []
        self.asked: list[tuple[dict[str, Any], str | None]] = []

    async def __call__(self, config: Any, *, organization_id: str | None) -> BaseToolset:
        self.asked.append((dict(config), organization_id))
        if config.get("server") != "srv_docs":
            raise LookupError(f"The organization has no MCP server {config.get('server')!r}")
        self.made.append(Docs())
        return self.made[-1]


def with_tools(
    sessions: DatabaseSessionService,
    llm: ScriptedLlm,
    *,
    servers: Any = None,
    search: Searches | None = None,
    code: Any = None,
    answer: Callable[..., Any] | None = None,
    workflow_wait: float = 120.0,
    sandbox_mode: str = "off",
) -> Worker:
    """A worker whose LLM agents run on ``llm``, their tools on fakes."""
    models = ProviderModels(PROVIDERS, build=lambda _call: llm)
    w = worker(sessions, model=models, answer=answer)
    agents = AgentServices(
        models=models,
        runtime=RuntimeServices(
            environment={},
            allowed_hosts=("api.example.com",),
            mcp_servers=servers,
            code_executor=code_executor(sandbox_mode),
        ),
        http=w.task.services.http,
        search=search,
        code=code,
        workflow_wait=workflow_wait,
    )
    w.task.services = w.task.services.but(agents=agents)
    return w


def chat_agent(**config: Any) -> dict[str, Any]:
    """An agent from the Agents page, published as version 2: it reads the request and its state."""
    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_support",
        "name": "Support",
        "version": 2,
        "organization_id": ORGANIZATION,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "name": "Support desk",
                "config": {
                    "instruction": "Help {{ request.userId }}, a {{ state.tier }} customer.",
                    "state_schema": {
                        "type": "object",
                        "properties": {"tier": {"type": "string", "enum": ["free", "pro"]}},
                    },
                    **config,
                },
            }
        ],
        "edges": [],
    }


async def test_an_llm_agent_calls_its_http_tool_and_answers(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("It shipped.")]])
    w = with_tools(sessions, llm)
    document = line(node("helper", "llm", llm_config(tools=[ORDERS])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "It shipped."
    assert [str(request.url) for request in w.sent] == ["https://api.example.com/orders/A1"]
    declared = llm.requests[0].config.tools[0].function_declarations  # type: ignore[index, union-attr]
    assert [d.name for d in declared] == ["look_up_order"]


async def test_an_http_tools_url_and_body_read_the_runs_data(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("create_order", sku="A1", count=2, other="x")], [text("Done.")]])
    w = with_tools(sessions, llm)
    create = tool(
        "tool_create",
        "http_tool",
        "Create order",
        description="Creates an order.",
        method="POST",
        url="https://api.example.com/{{ input.region }}/orders",
        body='{"sku": {sku}, "count": {count}, "customer": {{ input.customer }}, '
        '"note": "for {{ input.customer.name }}"}',
        parameters={"type": "object", "properties": {"count": {"type": "integer"}}},
    )
    document = line(node("helper", "llm", llm_config(tools=[create])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {"region": "eu", "customer": {"name": "Ada"}}))

    assert result.status is JobStatus.OK, result.detail
    [sent] = w.sent
    assert str(sent.url) == "https://api.example.com/eu/orders"
    # Only what its body names: the model's other argument goes nowhere.
    assert json.loads(sent.content) == {"sku": "A1", "count": 2, "customer": {"name": "Ada"}, "note": "for Ada"}
    [declared] = llm.requests[0].config.tools[0].function_declarations  # type: ignore[index, union-attr]
    assert declared.parameters_json_schema == {
        "type": "object",
        "properties": {"sku": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["sku"],
    }


async def test_an_llm_agent_runs_code_and_reads_what_it_printed(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("calculate", code="print(sum(range(1, 101)))")], [text("5050")]])
    w = with_tools(sessions, llm, sandbox_mode="local")
    code = tool("tool_code", "code_execution", "Calculate")
    document = line(node("helper", "llm", llm_config(tools=[code])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "5050"
    ran = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert ran == {"status": "success", "payload": {"stdout": "5050\n", "stderr": ""}}

    # Where code execution is off, a step whose agent runs code fails, saying so.
    llm.turns = [[text("unused")]]
    off = await with_tools(sessions, llm).run(payload(document, {}).model_copy(update={"session_id": "other"}))
    assert off.status is JobStatus.FAILED
    assert "tool 'Calculate'" in str(off.error) and "code execution is off" in str(off.error)


PLAYBOOK = {
    "name": "refund-playbook",
    "description": "How to handle a refund request. Use when someone asks for their money back.",
    "instructions": "1. Check the order is under 30 days old.\n2. Read references/policy.md.",
    "files": [{"path": "references/policy.md", "content": "Refunds within 30 days."}],
}


async def test_an_llm_agent_loads_a_skill_the_run_carries(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(
        turns=[
            [call("load_skill", skill_name="refund-playbook")],
            [call("load_skill_resource", skill_name="refund-playbook", file_path="references/policy.md")],
            [text("Refunds within 30 days.")],
        ]
    )
    w = with_tools(sessions, llm)
    skills = tool("tool_skills", "skills", "Skills", skills=["sk_refunds"])
    document = line(node("helper", "llm", llm_config(tools=[skills])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}, skills={"sk_refunds": PLAYBOOK}))

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunds within 30 days."
    # The model was told the skill from the start, by its name and description.
    instruction = str(llm.requests[0].config.system_instruction)
    assert "refund-playbook" in instruction and "money back" in instruction
    loaded = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert "Check the order is under 30 days old" in json.dumps(loaded)
    read = llm.requests[2].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert "Refunds within 30 days." in json.dumps(read)

    # A skill the run doesn't carry (deleted since) fails the step, saying so.
    llm.turns = [[text("unused")]]
    gone = await with_tools(sessions, llm).run(payload(document, {}).model_copy(update={"session_id": "other"}))
    assert gone.status is JobStatus.FAILED
    assert "tool 'Skills'" in str(gone.error) and "no skill sk_refunds" in str(gone.error)


def test_an_llm_agent_has_one_skills_tool_naming_its_skills() -> None:
    empty = tool("tool_skills", "skills", "Skills", skills=[])
    with pytest.raises(AgentBuildError, match="choose the skills it has"):
        build_agent(line(node("helper", "llm", llm_config(tools=[empty])), end("done", "'ok'")))
    one = tool("tool_a", "skills", "Skills", skills=["sk_a"])
    two = tool("tool_b", "skills", "More skills", skills=["sk_b"])
    with pytest.raises(AgentBuildError, match="one Skills tool"):
        build_agent(line(node("helper", "llm", llm_config(tools=[one, two])), end("done", "'ok'")))


async def test_a_knowledge_base_tool_searches_what_the_run_carries(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("policies", query="How much leave?")], [text("25 days.")]])
    search = Searches()
    w = with_tools(sessions, llm, search=search)
    kb = tool("tool_kb", "knowledge_base", "Policies", knowledge_bases=["kb_hr"], max_results=3)
    document = line(node("helper", "llm", llm_config(tools=[kb])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}, knowledge_bases={"kb_hr": {"name": "HR", "description": "Leave"}}))

    assert result.detail["result"] == "25 days."
    assert search.asked == [(["kb_hr"], "How much leave?", 3)]
    assert search.names == {"kb_hr": "HR"}  # the run's names for them, which each passage carries
    declared = llm.requests[0].config.tools[0].function_declarations  # type: ignore[index, union-attr]
    assert "HR (Leave)" in (declared[0].description or "")

    # One the run doesn't carry (it was checked as the run started) fails the step, saying which.
    llm.turns = [[call("policies", query="x")]]
    gone = await with_tools(sessions, llm, search=search).run(
        payload(document, {}).model_copy(update={"session_id": "other-session"})
    )
    assert gone.status is JobStatus.FAILED
    assert "tool 'Policies'" in str(gone.error) and "kb_hr" in str(gone.error)


def widgets_graph() -> Any:
    """The widgets' code graph in memory, whose search finds its Signer."""
    from forge_codegraph_query.testing import MemStore, span

    store = MemStore("repo:widgets")
    store.add_node(
        {
            "id": "entity:signer",
            "kind": "class",
            "name": "Signer",
            "qualified_name": "widgets.Signer",
            "properties": {"file_path": {"string": "src/sign.py"}, "signature": {"string": "class Signer:"}},
            # Its file isn't in the graph: the hit keeps its signature.
            "source": {"lineage": "file:sign", "content_sha256": "0" * 64, "span": span(0, 4, 10, 4)},
        }
    )
    store.hits = [store.hit("entity:signer", 0.7, False)]
    return store


async def test_a_knowledge_base_tool_searches_a_graph_knowledge_bases_code(sessions: DatabaseSessionService) -> None:
    from forge_codegraph import citation_ref as code_ref
    from forge_codegraph_query import CodeGraph

    llm = ScriptedLlm(turns=[[call("code", query="How are tokens signed?")], [text("With HMAC.")]])
    store = widgets_graph()
    w = with_tools(sessions, llm, search=Searches(), code=CodeGraph(store))
    kb = tool("tool_kb", "knowledge_base", "Code", knowledge_bases=["kb_code"], max_results=3)
    document = line(node("helper", "llm", llm_config(tools=[kb])), end("done", "steps.helper.output"))
    carried = {
        "kb_code": {
            "name": "Widgets",
            "description": "",
            "kind": "system",
            "repositories": [{"id": "r1", "graph_id": "repo:widgets", "name": "acme/widgets"}],
            "connections": [{"source": "acme/web", "kind": "calls", "target": "acme/widgets", "description": ""}],
        }
    }

    result = await w.run(payload(document, {}, knowledge_bases=carried))

    assert result.detail["result"] == "With HMAC."
    [asked] = store.searches
    assert (asked.repository_ids, asked.text, asked.limit) == (["repo:widgets"], "How are tokens signed?", 3)
    declared = llm.requests[0].config.tools[0].function_declarations  # type: ignore[index, union-attr]
    # The tool tells the model the system: its applications and how they connect.
    assert "Widgets (code of acme/widgets; how they connect: acme/web calls the API of acme/widgets)" in (
        declared[0].description or ""
    )
    answered = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    passage = answered["payload"]["passages"][0]
    assert passage["ref"] == code_ref("repo:widgets:entity:signer")
    assert (passage["document"], passage["location"]) == ("acme/widgets: src/sign.py", "src/sign.py, line 4")
    assert passage["text"] == "class Signer:"

    # Without the code graph, the tool answers the model what to set.
    llm.turns = [[call("code", query="x")], [text("I can't search the code.")]]
    llm.requests.clear()
    without = await with_tools(sessions, llm, search=Searches()).run(
        payload(document, {}, knowledge_bases=carried).model_copy(update={"session_id": "other-session"})
    )
    assert without.detail["result"] == "I can't search the code."
    refused = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert "CODEGRAPH__SPANNER__DATABASE" in str(refused)


async def test_a_carried_system_design_is_described_and_read() -> None:
    design = {
        "groups": [{"name": "Payments", "description": "Charging customers"}],
        "applications": [{"name": "acme/billing", "url": "https://github.com/acme/billing", "group": "Payments"}],
        "components": [
            {"name": "ledger", "kind": "database", "technology": "PostgreSQL", "description": "", "group": "Payments"}
        ],
        "connections": [{"source": "acme/billing", "kind": "writes_to", "target": "ledger", "description": ""}],
    }
    carried = {
        "kb_shop": {"name": "Shop", "description": "The shop", "kind": "system", "repositories": [], "design": design},
        # A run started before designs carries the connections alone.
        "kb_old": {
            "name": "Old",
            "description": "",
            "kind": "system",
            "connections": [{"source": "a/web", "kind": "calls", "target": "a/api", "description": ""}],
        },
        "kb_docs": {"name": "Docs", "description": "Policies", "kind": "rag"},
    }
    knowledge = SnapshotKnowledgeBases(carried, None)

    ((_, about),) = await knowledge.describe(["kb_shop"], organization_id=ORGANIZATION)
    assert about == (
        "The shop; code of acme/billing; other parts: ledger (database, PostgreSQL); groups: Payments "
        "(acme/billing, ledger): Charging customers; how they connect: acme/billing writes to ledger"
    )
    assert await knowledge.design(["kb_docs"], organization_id=ORGANIZATION) is None
    found = await knowledge.design(["kb_docs", "kb_shop", "kb_old"], organization_id=ORGANIZATION, focus="ledger")
    assert found is not None
    (shop,) = found["systems"]
    assert shop["knowledge_base"] == "Shop"
    assert shop["connections"] == [
        {"source": "acme/billing", "relation": "writes to", "target": "ledger", "description": ""}
    ]
    whole = await knowledge.design(["kb_old"], organization_id=ORGANIZATION)
    assert whole is not None
    assert whole["systems"][0]["connections"][0]["relation"] == "calls the API of"


async def test_the_documents_search_ranks_a_tools_knowledge_bases_together() -> None:
    from dataclasses import replace

    from forge_task_documents.retrieval import Passage, citation_ref

    class Search:
        """The documents task's knowledge base search, recording what it was asked."""

        asked: list[tuple[list[str], str, int]] = []

        async def search(self, ids: Any, query: str, *, limit: int) -> list[Passage]:
            self.asked.append((list(ids), query, limit))
            appeals = Passage("c1", "d1", "Claims", ["Appeals"], "Appeal within 180 days.", 0.031)
            benefits = Passage("c2", "d2", "Benefits", [], "Two cleanings a year.", 0.016, "kb_members")
            appeals = replace(appeals, knowledge_base_id="kb_claims", filename="appeals.pdf", location="page 2")
            return [appeals, benefits]

    search = Search()
    knowledge_bases = {"kb_members": "Member knowledge", "kb_claims": "Claims knowledge"}
    found = await DocumentsSearch(search)(knowledge_bases, "appeal a denied claim", 5)

    # One search over both, not one each.
    assert search.asked == [(["kb_members", "kb_claims"], "appeal a denied claim", 5)]
    # Each passage names the knowledge base it's from, and carries what a
    # chat UI cites and links it by.
    assert found == [
        {
            "ref": citation_ref("c1"),
            "knowledge_base": "Claims knowledge",
            "knowledge_base_id": "kb_claims",
            "document": "appeals.pdf",
            "document_id": "d1",
            "section": "Appeals",
            "location": "page 2",
            "text": "Appeal within 180 days.",
            "score": 0.031,
        },
        {
            "ref": citation_ref("c2"),
            "knowledge_base": "Member knowledge",
            "knowledge_base_id": "kb_members",
            "document": "Benefits",
            "document_id": "d2",
            "section": "",
            "location": "",
            "text": "Two cleanings a year.",
            "score": 0.016,
        },
    ]


async def test_an_organizations_mcp_server_is_used_then_closed(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("search_docs", q="refunds")], [text("Found it.")]])
    servers = Servers()
    w = with_tools(sessions, llm, servers=servers)
    docs = tool("tool_docs", "mcp", "Docs", server="srv_docs", tools="")
    document = line(node("helper", "llm", llm_config(tools=[docs])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}))

    assert result.detail["result"] == "Found it."
    assert servers.asked[0][1] == ORGANIZATION
    # What the run opened is closed when it stops.
    assert [toolset.closed for toolset in servers.made] == [True]


class Down(Docs):
    """An MCP server's toolset whose server won't connect."""

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        raise ConnectionError("Failed to get tools from MCP server")


class DownServers(Servers):
    async def __call__(self, config: Any, *, organization_id: str | None) -> BaseToolset:
        self.made.append(Down())
        return self.made[-1]


async def test_an_mcp_server_that_wont_connect_fails_its_step(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[text("I answered without the docs.")]])
    servers = DownServers()
    w = with_tools(sessions, llm, servers=servers)
    docs = tool("tool_docs", "mcp", "Docs", server="srv_docs", tools="")
    document = line(node("helper", "llm", llm_config(tools=[docs])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}))

    # The step doesn't go on without a tool it was given, as a chat agent would.
    assert result.status is JobStatus.FAILED
    assert "tool 'Docs': ConnectionError: Failed to get tools from MCP server" in str(result.error)
    assert llm.requests == []
    assert [toolset.closed for toolset in servers.made] == [True]


class NeverCloses(Docs):
    """An MCP server's toolset whose session never finishes closing."""

    async def close(self) -> None:
        await asyncio.Event().wait()


class StuckServers(Servers):
    async def __call__(self, config: Any, *, organization_id: str | None) -> BaseToolset:
        self.made.append(NeverCloses())
        return self.made[-1]


async def test_a_tool_that_never_finishes_closing_doesnt_hold_the_run(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_tools, "CLOSE_SECONDS", 0.05)
    llm = ScriptedLlm(turns=[[call("search_docs", q="refunds")], [text("Found it.")]])
    w = with_tools(sessions, llm, servers=StuckServers())
    docs = tool("tool_docs", "mcp", "Docs", server="srv_docs", tools="")
    document = line(node("helper", "llm", llm_config(tools=[docs])), end("done", "steps.helper.output"))

    result = await asyncio.wait_for(w.run(payload(document, {})), 5)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == "Found it."


async def test_an_agent_from_the_agents_page_is_sent_its_message_and_inputs(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[text("Happy to help, pro.")]])
    w = with_tools(sessions, llm)
    config = llm_config(
        source="agent",
        agent="ca_support",
        version=None,
        message="{{ input.name }} asks: {{ input.question }}",
        inputs={"tier": "input.tier"},
    )
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    result = await w.run(
        payload(
            document,
            {"name": "Ada", "question": "Where is A1?", "tier": "pro"},
            chat_agents={"ca_support": chat_agent()},
        )
    )

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Happy to help, pro."
    asked = llm.requests[0]
    assert str(asked.config.system_instruction).startswith("Help member-1, a pro customer.")
    said = [part.text for content in asked.contents for part in content.parts or [] if part.text]
    assert said == ["Ada asks: Where is A1?"]
    # The run page shows what it asked its agent with.
    run = payload(document, {}, chat_agents={"ca_support": chat_agent()})
    session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
    support = next(step for step in run_steps(session, document) if step["id"] == "support")
    assert support["called_with"] == {"message": "Ada asks: Where is A1?", "inputs": {"tier": "pro"}}

    # Inputs its schema doesn't take fail the step, saying why.
    refused = line(
        node("support", "llm", {**config, "inputs": {"tier": "'gold'"}}),
        end("done", "steps.support.output"),
    )
    failed = await with_tools(sessions, llm).run(
        payload(refused, {}, chat_agents={"ca_support": chat_agent()}).model_copy(
            update={"session_id": "other-session"}
        )
    )
    assert failed.status is JobStatus.FAILED
    assert "Support (support) failed: its inputs don't fit the agent" in str(failed.error)


async def test_an_agent_from_the_agents_page_works_with_the_runs_files(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[text("Nothing to read.")]])
    w = with_tools(sessions, llm)
    w.task.artifacts = InMemoryArtifactService()
    config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    result = await w.run(payload(document, {}, chat_agents={"ca_support": chat_agent()}))

    assert result.status is JobStatus.OK, result.error
    # It reads and saves files as the run's own LLM agents do.
    assert {"load_artifacts", "save_file"} <= set(llm.requests[0].tools_dict)


class KeptDocs(Docs):
    """Docs, noting whether it was still open as each search ran."""

    def __init__(self) -> None:
        super().__init__()
        self.open_as_searched: list[bool] = []

        def search_docs(q: str) -> dict[str, Any]:
            """Searches the docs."""
            self.open_as_searched.append(not self.closed)
            return {"found": f"Docs about {q}"}

        self.tool = FunctionTool(search_docs)


class KeptServers(Servers):
    async def __call__(self, config: Any, *, organization_id: str | None) -> BaseToolset:
        self.made.append(KeptDocs())
        return self.made[-1]


async def test_an_agent_called_as_a_tool_keeps_its_toolsets_until_the_run_stops(
    sessions: DatabaseSessionService,
) -> None:
    researcher = [call("researcher", request="Refunds?")], [call("search_docs", q="refunds")], [text("5 days.")]
    llm = ScriptedLlm(turns=[*researcher, *researcher, [text("Refunds take 5 days.")]])
    servers = KeptServers()
    w = with_tools(sessions, llm, servers=servers)
    agent = chat_agent()
    agent["nodes"] += [
        {"id": "res", "kind": "sub_agent", "name": "Researcher", "config": {"instruction": "Research."}},
        {"id": "docs", "kind": "mcp", "name": "Docs", "config": {"server": "srv_docs", "tools": ""}},
    ]
    agent["edges"] += [
        {"id": "e1", "source": "agent", "source_output": "tools", "target": "res"},
        {"id": "e2", "source": "res", "source_output": "tools", "target": "docs"},
    ]
    config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    result = await w.run(payload(document, {}, chat_agents={"ca_support": agent}))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == "Refunds take 5 days."
    # Each call's own runner left the researcher's MCP session open; the run closed it as it stopped.
    [docs] = servers.made
    assert docs.open_as_searched == [True, True]
    assert docs.closed


def with_docs(agent: dict[str, Any], *, called: bool) -> dict[str, Any]:
    """An Agents-page agent searching the docs server: itself, or through a researcher it calls as a tool."""
    docs = {"id": "docs", "kind": "mcp", "name": "Docs", "config": {"server": "srv_docs", "tools": ""}}
    if not called:
        agent["nodes"].append(docs)
        agent["edges"].append({"id": "e1", "source": "agent", "source_output": "tools", "target": "docs"})
        return agent
    agent["nodes"] += [
        {"id": "res", "kind": "sub_agent", "name": "Researcher", "config": {"instruction": "Research."}},
        docs,
    ]
    agent["edges"] += [
        {"id": "e1", "source": "agent", "source_output": "tools", "target": "res"},
        {"id": "e2", "source": "res", "source_output": "tools", "target": "docs"},
    ]
    return agent


@pytest.mark.parametrize("called", [False, True])
async def test_an_agent_from_the_agents_page_whose_mcp_server_wont_connect_fails_its_step(
    sessions: DatabaseSessionService, called: bool
) -> None:
    turns = [[call("researcher", request="Refunds?")]] if called else []
    llm = ScriptedLlm(turns=[*turns, [text("I answered without the docs.")]])
    servers = DownServers()
    w = with_tools(sessions, llm, servers=servers)
    config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    run = payload(document, {}, chat_agents={"ca_support": with_docs(chat_agent(), called=called)})
    result = await w.run(run)

    # The step doesn't go on without a tool its agent was given, as a chat would.
    assert result.status is JobStatus.FAILED
    agent = "researcher" if called else "support"
    assert str(result.error) == (
        f"Support (support) failed: tools of {agent} can't be used: "
        "ConnectionError: Failed to get tools from MCP server"
    )
    # Its own model was never asked; a researcher it called, never answered.
    assert len(llm.requests) == (1 if called else 0)
    assert [toolset.closed for toolset in servers.made] == [True]


async def test_an_agent_from_the_agents_page_whose_model_answers_with_an_error_fails_its_step(
    sessions: DatabaseSessionService,
) -> None:
    w = with_tools(sessions, ScriptedLlm(turns=[blocked_for_safety()]))
    config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    result = await w.run(payload(document, {}, chat_agents={"ca_support": chat_agent()}))

    assert result.status is JobStatus.FAILED
    assert result.error == "Support (support) failed: its model answered with an error (SAFETY): Blocked for safety"


@pytest.mark.parametrize("source", ["model", "agent"])
async def test_a_native_gemini_truncation_fails_before_downstream_steps(
    sessions: DatabaseSessionService, source: str,
) -> None:
    response = LlmResponse.create(types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[text("Partial answer cut off")]),
        finish_reason=types.FinishReason.MAX_TOKENS,
    )]))
    assert response.error_code is None
    w = with_tools(sessions, ScriptedLlm(turns=[response]))
    config = llm_config()
    agents = {}
    if source == "agent":
        config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
        agents = {"ca_support": chat_agent()}
    document = line(node("support", "llm", config), end("done", "steps.support.output"))

    run = payload(document, {}, chat_agents=agents)
    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert "MAX_TOKENS" in str(result.error)
    steps = await w.steps(run)
    assert next(step for step in steps if step["id"] == "support")["status"] == "failed"
    assert next(step for step in steps if step["id"] == "done")["status"] == "not_reached"


@pytest.mark.parametrize("source", ["model", "agent"])
@pytest.mark.parametrize("failure", ["partial_error", "partial_message", "interrupted", "partial_interrupted"])
async def test_an_unfinished_model_response_fails_before_downstream_steps(
    sessions: DatabaseSessionService, source: str, failure: str,
) -> None:
    response = LlmResponse(
        content=types.Content(role="model", parts=[text("Incomplete answer")]),
        partial=failure.startswith("partial"),
        error_code="PROVIDER_UNAVAILABLE" if failure == "partial_error" else None,
        error_message="The model's service became unavailable. Please retry." if "interrupted" not in failure else None,
        interrupted="interrupted" in failure,
    )
    w = with_tools(sessions, ScriptedLlm(turns=[response]))
    config = llm_config()
    agents = {}
    if source == "agent":
        config = llm_config(source="agent", agent="ca_support", version=None, message="Hi", inputs={"tier": "'pro'"})
        agents = {"ca_support": chat_agent()}
    run = payload(line(node("support", "llm", config), end("done", "steps.support.output")), {}, chat_agents=agents)

    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    explanation = "stopped before finishing its response" if "interrupted" in failure else response.error_message
    assert explanation in str(result.error)
    steps = await w.steps(run)
    assert next(step for step in steps if step["id"] == "support")["status"] == "failed"
    assert next(step for step in steps if step["id"] == "done")["status"] == "not_reached"


async def test_a_tool_a_person_confirms_pauses_the_run_until_they_do(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("It shipped.")]])
    w = with_tools(sessions, llm)
    confirmed = {**ORDERS, "config": {**ORDERS["config"], "confirm": True}}
    document = line(node("helper", "llm", llm_config(tools=[confirmed])), end("done", "steps.helper.output"))
    run = payload(document, {})

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [gate] = w.control.gates
    assert gate["details"]["kind"] == "approval"
    assert gate["details"]["tool"] == "look_up_order"
    assert gate["details"]["args"] == {"order_id": "A1"}
    assert gate["details"]["step"] == "helper"
    assert w.sent == []

    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "It shipped."
    assert [str(request.url) for request in w.sent] == ["https://api.example.com/orders/A1"]
    assert "Helper (helper): calling look_up_order allowed by Lead" in w.control.said()


async def test_a_steps_tool_calls_show_on_the_run_page(sessions: DatabaseSessionService) -> None:
    from forge_task_adk_workflows.runs import APP_NAME
    from forge_task_adk_workflows.steps import run_steps

    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("It shipped.")]])
    w = with_tools(sessions, llm)
    confirmed = {**ORDERS, "config": {**ORDERS["config"], "confirm": True}}
    document = line(node("helper", "llm", llm_config(tools=[confirmed])), end("done", "steps.helper.output"))
    run = payload(document, {})

    async def helper() -> dict[str, Any]:
        session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
        return next(step for step in run_steps(session, document) if step["id"] == "helper")

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    paused = await helper()
    assert paused["status"] == "waiting"
    [asked] = paused["calls"]
    assert (asked["name"], asked["args"], asked["status"]) == ("look_up_order", {"order_id": "A1"}, "waiting")

    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))
    await w.run(run)
    done = await helper()
    [made] = done["calls"]
    assert made["status"] == "done"
    assert made["response"]["payload"]["body"] == {"ok": True}


async def test_a_tool_call_a_person_refuses_isnt_made(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("I wasn't allowed to look.")]])
    w = with_tools(sessions, llm)
    confirmed = {**ORDERS, "config": {**ORDERS["config"], "confirm": True}}
    document = line(node("helper", "llm", llm_config(tools=[confirmed])), end("done", "steps.helper.output"))
    run = payload(document, {})

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    w.control.decide(waiting.value.key, Decision(approved=False, actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.detail["result"] == "I wasn't allowed to look."
    assert w.sent == []


def test_a_document_naming_tools_badly_doesnt_build() -> None:
    def refused(tools: list[dict[str, Any]], match: str) -> None:
        document = line(node("helper", "llm", llm_config(tools=tools)), end("done"))
        with pytest.raises(AgentBuildError, match=match):
            build_agent(document)

    refused([tool("t", "calculator", "Sums")], "Forge has no kind of tool 'calculator'")
    refused([tool("t", "knowledge_base", "Policies", knowledge_bases=[])], "choose the knowledge bases")
    refused([tool("t", "mcp", "Docs", server="", url="")], "MCP servers, or give its URL")
    refused([ORDERS, ORDERS], "an ID of its own")
    with pytest.raises(AgentBuildError, match="choose the agent from the Agents page"):
        build_agent(line(node("helper", "llm", llm_config(source="agent", agent="")), end("done")))


async def test_without_tool_services_a_tool_fails_its_step(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[text("unused")]])
    w = worker(sessions, model=ProviderModels(PROVIDERS, build=lambda _call: llm))
    document = line(node("helper", "llm", llm_config(tools=[ORDERS])), end("done", "steps.helper.output"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert "tool 'Look up order': tools can't be used on this worker" in str(result.error)


# ---------------------------------------------------------------------- calls cut off by a stop


class Refunds(BaseToolset):
    """An MCP server's toolset whose one tool does something (a refund), taking ``seconds``."""

    def __init__(self, seconds: float = 30) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.started = asyncio.Event()

        async def refund(order: str, tool_context: ToolContext) -> dict[str, Any]:
            """Refunds an order."""
            self.calls.append(str(tool_context.function_call_id))
            self.started.set()
            await asyncio.sleep(seconds)
            return {"refunded": order}

        self.tool = FunctionTool(refund)

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        return [self.tool]


class OneServer:
    """The organization's MCP servers: always ``toolset``."""

    def __init__(self, toolset: BaseToolset) -> None:
        self.toolset = toolset

    async def __call__(self, config: Any, *, organization_id: str | None) -> BaseToolset:
        return self.toolset


async def stopped_while(started: asyncio.Event, running: Any) -> None:
    """The worker stops while the run waits on ``started``'s tool: SAQ cancels its job."""
    job = asyncio.create_task(running)
    await started.wait()
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job


def responses(request: Any) -> list[dict[str, Any]]:
    """The tools' answers a model request carries."""
    return [
        dict(part.function_response.response or {})
        for content in request.contents
        for part in content.parts or []
        if part.function_response
    ]


async def answered_by_the_run(sessions: DatabaseSessionService, run: Any) -> list[Any]:
    session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
    assert session is not None
    return [event for event in session.events if (event.custom_metadata or {}).get(CUT_OFF_KEY)]


async def test_a_call_cut_off_by_a_stop_is_answered_before_the_model_is_asked_again(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refund", order="A1")], [text("It may be refunded; I'll check.")]])
    refunds = Refunds()
    w = with_tools(sessions, llm, servers=OneServer(refunds))
    document = line(
        node("helper", "llm", llm_config(tools=[tool("t", "mcp", "Refunds", server="srv", tools="")])),
        end("done", "steps.helper.output"),
    )
    run = payload(document, {})

    await stopped_while(refunds.started, w.run(run))

    # Noted, durably, before it ran: it may have taken effect.
    [cut_off] = refunds.calls
    assert list(w.control.load(STATE_KEY)["calls"]) == [cut_off]

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert refunds.calls == [cut_off]  # not again
    # The model sees what became of its call, instead of calling again blind.
    assert responses(llm.requests[1]) == [
        {
            "status": "failed",
            "reason": CUT_OFF,
            "suggested_fixes": ["Check whether it took effect before calling it again."],
        }
    ]
    [answer] = await answered_by_the_run(sessions, run)
    assert [response.id for response in answer.get_function_responses()] == [cut_off]
    assert "Helper (helper): its call to refund was cut off as its worker stopped: it may have taken effect" in (
        w.control.said()
    )
    assert w.control.load(STATE_KEY)["calls"] == {}
    session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
    helper = next(step for step in run_steps(session, document) if step["id"] == "helper")
    assert [(made["id"], made["status"], made["error"]) for made in helper["calls"]] == [(cut_off, "failed", CUT_OFF)]


async def test_a_call_cut_off_in_an_agent_seeing_only_its_turn_is_answered_when_made_again(
    sessions: DatabaseSessionService,
) -> None:
    # An agent from the Agents page sees only its own turn: not the answer its cut-off call got.
    llm = ScriptedLlm(
        turns=[[call("refund", order="A1")], [call("refund", order="A1")], [text("It may be refunded; I'll check.")]]
    )
    refunds = Refunds()
    w = with_tools(sessions, llm, servers=OneServer(refunds))
    agent = chat_agent()
    agent["nodes"].append({"id": "refunds", "kind": "mcp", "name": "Refunds", "config": {"server": "srv"}})
    agent["edges"].append({"id": "t", "source": "agent", "source_output": "tools", "target": "refunds"})
    config = llm_config(source="agent", agent="ca_support", version=None, message="Refund A1", inputs={})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))
    run = payload(document, {}, chat_agents={"ca_support": agent})

    await stopped_while(refunds.started, w.run(run))
    [cut_off] = refunds.calls
    assert list(w.control.load(STATE_KEY)["calls"]) == [cut_off]  # noted there too

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert responses(llm.requests[1]) == []  # it asks afresh, as ADK has it...
    assert refunds.calls == [cut_off]  # ...but its call again doesn't run: it's answered as the cut-off one was
    assert [said["reason"] for said in responses(llm.requests[2])] == [CUT_OFF]
    assert (
        "Support (support): its call to refund again, as it carried on, is answered as the one its worker's "
        "stop cut off"
    ) in w.control.said()
    # The cut-off call is answered where it was, in the agent's branch.
    [answer] = await answered_by_the_run(sessions, run)
    session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
    asked = next(event for event in session.events if cut_off in [c.id for c in event.get_function_calls()])
    assert (answer.branch, answer.author) == (asked.branch, asked.author)


def refunding_support() -> tuple[dict[str, Any], Any]:
    """A workflow whose step is an agent from the Agents page (it sees only its
    own turn) with a refunds tool; and its run."""
    agent = chat_agent()
    agent["nodes"].append({"id": "refunds", "kind": "mcp", "name": "Refunds", "config": {"server": "srv"}})
    agent["edges"].append({"id": "t", "source": "agent", "source_output": "tools", "target": "refunds"})
    config = llm_config(source="agent", agent="ca_support", version=None, message="Refund A1", inputs={})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))
    return document, payload(document, {}, chat_agents={"ca_support": agent})


async def test_a_call_that_went_through_isnt_made_twice_when_its_step_runs_again_after_a_hiccup(
    sessions: DatabaseSessionService,
) -> None:
    # The refund goes through, then the model can't be reached: the attempt ends, and its step runs again.
    llm = ScriptedLlm(
        turns=[[call("refund", order="A1")], overloaded(), [call("refund", order="A1")], [text("Refunded.")]]
    )
    refunds = Refunds(seconds=0)
    w = with_tools(sessions, llm, servers=OneServer(refunds))
    _, run = refunding_support()

    with pytest.raises(TransientError):
        await w.run(run)
    [made] = refunds.calls
    assert list(w.control.load(STATE_KEY)["calls"]) == [made]  # noted, durably, before it ran

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert refunds.calls == [made]  # not twice
    assert responses(llm.requests[2]) == []  # asked afresh, as ADK has it...
    assert responses(llm.requests[3]) == responses(llm.requests[1])  # ...its call again answered as it was
    assert (
        "Support (support): its call to refund again, as it carried on, is answered as it was before: "
        "it isn't made twice"
    ) in w.control.said()
    assert w.control.load(STATE_KEY)["replays"] == []  # its step ended: nothing to answer again


async def test_a_call_cut_off_stays_answered_though_the_attempt_that_answered_it_failed_too(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(
        turns=[
            [call("refund", order="A1")],  # cut off as its worker stops
            [call("refund", order="A1")],  # asked afresh: answered as the cut-off call...
            overloaded(),  # ...then the model can't be reached: that attempt ends too
            [call("refund", order="A1")],  # asked afresh again
            [text("It may be refunded; I'll check.")],
        ]
    )
    refunds = Refunds()
    w = with_tools(sessions, llm, servers=OneServer(refunds))
    _, run = refunding_support()
    await stopped_while(refunds.started, w.run(run))
    [cut_off] = refunds.calls

    with pytest.raises(TransientError):
        await w.run(run)
    # Journaled, durably: the next attempt doesn't take it for a call answered and done with.
    assert [entry["call"] for entry in w.control.load(STATE_KEY)["replays"]] == [cut_off]

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert refunds.calls == [cut_off]  # never made again
    assert [said["reason"] for said in responses(llm.requests[4])] == [CUT_OFF]


async def test_a_call_that_only_reads_cut_off_is_answered_unanswered(sessions: DatabaseSessionService) -> None:
    started = asyncio.Event()

    async def slow(request: Any) -> Any:
        started.set()
        await asyncio.sleep(30)

    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("I'll look again.")]])
    w = with_tools(sessions, llm, answer=slow)
    document = line(node("helper", "llm", llm_config(tools=[ORDERS])), end("done", "steps.helper.output"))
    run = payload(document, {})

    await stopped_while(started, w.run(run))
    assert w.control.load(STATE_KEY)["calls"] == {}  # a GET only reads: nothing to note

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    [said] = responses(llm.requests[1])
    assert (said["status"], said["reason"]) == ("failed", NO_ANSWER)
    assert "Helper (helper): its call to look_up_order got no answer: its worker stopped" in w.control.said()


async def test_a_call_a_person_is_still_to_confirm_isnt_answered_as_cut_off(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[call("look_up_order", order_id="A1")], [text("It shipped.")]])
    w = with_tools(sessions, llm)
    confirmed = {**ORDERS, "config": {**ORDERS["config"], "confirm": True}}
    document = line(node("helper", "llm", llm_config(tools=[confirmed])), end("done", "steps.helper.output"))
    run = payload(document, {})

    with pytest.raises(AwaitingDecision):
        await w.run(run)
    with pytest.raises(AwaitingDecision):  # its job again, still undecided
        await w.run(run)

    assert await answered_by_the_run(sessions, run) == []


@dataclass
class Children:
    """The runs a run starts, standing in for the worker's (``ChildRuns``):
    each queued when it's started, at ``clock``'s time; ``hold`` keeps a start
    from returning (its worker stops meanwhile)."""

    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    asked: asyncio.Event = field(default_factory=asyncio.Event)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    hold: asyncio.Event | None = None

    async def start(self, payload: dict[str, Any], *, run_id: str | None = None) -> str:
        assert run_id is not None
        if run_id not in self.runs:
            self.runs[run_id] = {
                "id": run_id,
                "status": "running",
                "agent_id": payload["agent_id"],
                "agent_name": payload["name"],
                "payload": payload,
                "created_at": self.clock(),
            }
            self.started.append(run_id)
        self.asked.set()
        if self.hold is not None:
            await self.hold.wait()
        return run_id

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    def end(self, run_id: str, status: str = "succeeded", **values: Any) -> None:
        self.runs[run_id].update(status=status, **values)


@dataclass
class Starting(Control):
    """The test control, starting runs on ``children``."""

    children: Children = field(default_factory=Children)


REFUNDS = {
    "format": "forge.agent/v1",
    "id": "ag_refunds",
    "name": "Refunds",
    "version": 1,
    "nodes": [{"id": "start", "kind": "start", "name": "Start", "config": {"input_schema": {}}, "outputs": ["next"]}],
    "edges": [],
}


def calling_refunds(
    sessions: DatabaseSessionService, llm: ScriptedLlm, *, wait: float = 30, **config: Any
) -> tuple[Worker, Children, RunPayload]:
    """A run whose Helper step calls the Refunds workflow (``config``: its
    tool's), on a worker whose runs start runs on the children returned."""
    w = with_tools(sessions, llm)
    children = Children(clock=w.clock)
    w.control = Starting(instance="task-1", children=children)
    workflow = tool("w", "adk_workflow", "Refunds", workflow="ag_refunds", version=1, wait_seconds=wait, **config)
    document = line(node("helper", "llm", llm_config(tools=[workflow])), end("done", "steps.helper.output"))
    return w, children, payload(document, {}, saved={"ag_refunds@1": REFUNDS})


def answered(child: str, status: str, **more: Any) -> dict[str, Any]:
    """What a workflow's call is answered, as its guarded tool answers: the run it started, as it is."""
    return {"status": "success", "payload": {"run_id": child, "status": status, **more}}


async def test_a_workflow_called_as_a_tool_pauses_its_run_until_the_run_it_started_ends(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("Refunded.")]])
    w, children, run = calling_refunds(sessions, llm, wait=30)

    # The call starts its run and gives the worker back: the run waits for it at a gate.
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    session = await sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
    [asked] = [called for event in session.events for called in event.get_function_calls()]
    assert child == child_run_id("task-1", str(asked.id))  # its call's run
    assert children.runs[child]["payload"]["trigger"]["wait_seconds"] == 30
    [gate] = w.control.gates
    assert gate["key"] == waiting.value.key == asked.id
    assert gate["reason"] == f"Waits for Refunds (run {child[:8]}) to end"
    details = gate["details"]
    assert (details["kind"], details["child_run_id"], details["tool"]) == ("child_run", child, "refunds")
    assert (details["step"], details["step_name"], details["child_workflow_id"]) == ("helper", "Helper", "ag_refunds")
    assert gate["timeout_seconds"] == 31  # the call's wait, and a second's margin
    assert len(llm.requests) == 1  # the model hasn't been asked again
    helper = next(step for step in await w.steps(run) if step["id"] == "helper")
    assert helper["status"] == "waiting"
    assert [(made["name"], made["status"]) for made in helper["calls"]] == [("refunds", "waiting")]

    # Its job again before the run it started ended (a stray one): it still waits.
    with pytest.raises(AwaitingDecision):
        await w.run(run)
    assert len(llm.requests) == 1

    # The run ended: its end decided the gate (the run store, as it ended).
    children.end(child, result={"refunded": "A1"})
    w.control.decide(waiting.value.key, Decision(approved=True, comment="succeeded"))
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunded."
    assert children.started == [child]  # no other run for that call
    assert responses(llm.requests[1]) == [answered(child, "succeeded", result={"refunded": "A1"})]
    assert f"Helper (helper): its call to refunds is answered: run {child[:8]} succeeded" in w.control.said()
    helper = next(step for step in await w.steps(run) if step["id"] == "helper")
    assert [(made["name"], made["status"]) for made in helper["calls"]] == [("refunds", "done")]


@pytest.mark.parametrize("status", ["failed", "abandoned"])
async def test_a_workflow_call_is_answered_with_how_its_run_ended(
    sessions: DatabaseSessionService, status: str
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("It didn't go through.")]])
    w, children, run = calling_refunds(sessions, llm)
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    error = {"message": "The refund was declined", "category": "failed"} if status == "failed" else None
    children.end(child, status, error=error)
    w.control.decide(waiting.value.key, Decision(approved=True, comment=status))

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert responses(llm.requests[1]) == [answered(child, status, **({"error": error} if error else {}))]


async def test_a_workflow_call_whose_run_ended_before_its_run_paused_is_answered_at_once(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("Refunded.")]])
    w, children, run = calling_refunds(sessions, llm)
    children.hold = asyncio.Event()

    # The worker stops as the call starts its run; the run ends before the next attempt.
    await stopped_while(children.asked, w.run(run))
    [child] = children.started
    children.end(child, result={"refunded": "A1"})
    children.hold = None

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert w.control.gates == []  # never waited
    assert children.started == [child]
    assert responses(llm.requests[1]) == [answered(child, "succeeded", result={"refunded": "A1"})]
    assert await answered_by_the_run(sessions, run) == []  # its answer isn't a cut-off call's


async def test_a_workflow_call_whose_wait_is_up_is_answered_with_where_its_run_got_to(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("It's still going.")]])
    w, children, run = calling_refunds(sessions, llm, wait=5)
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    assert w.control.gates[0]["timeout_seconds"] == 6
    # Its deadline: the run store times the gate out, a rejection by nobody.
    w.control.decide(waiting.value.key, Decision(approved=False, comment="Nobody decided by its deadline"))
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "It's still going."
    assert responses(llm.requests[1]) == [answered(child, "running")]
    assert f"Helper (helper): its call to refunds is answered: run {child[:8]} is running still" in w.control.said()


async def test_a_workflow_call_cut_off_before_it_started_its_run_is_answered_unanswered(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("I'll try again later.")]])
    w, children, run = calling_refunds(sessions, llm)
    children.hold = asyncio.Event()
    await stopped_while(children.asked, w.run(run))
    children.runs.clear()  # as though the worker stopped before the run was started
    children.started.clear()
    children.hold = None

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert children.started == []  # the call isn't made again: the model decides
    assert responses(llm.requests[1]) == [
        {"status": "failed", "reason": NO_ANSWER, "suggested_fixes": ["Call it again if it's still needed."]}
    ]
    assert "Helper (helper): its call to refunds got no answer: its worker stopped" in w.control.said()


async def test_a_workflow_call_that_fails_is_answered_with_why_and_its_agent_goes_on(
    sessions: DatabaseSessionService,
) -> None:
    # Its input doesn't fit the workflow's start: no run is started, and the model is told why.
    llm = ScriptedLlm(turns=[[call("refunds", order=7)], [text("The order must be text.")]])
    w, children, run = calling_refunds(sessions, llm)
    start = {"type": "object", "properties": {"order": {"type": "string"}}}
    refunds = {**REFUNDS, "nodes": [{**REFUNDS["nodes"][0], "config": {"input_schema": start}}]}
    run = run.model_copy(update={"saved": {"ag_refunds@1": refunds}})

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "The order must be text."
    assert children.started == [] and w.control.gates == []
    [answer] = responses(llm.requests[1])
    assert answer["status"] == "failed"
    assert "doesn't fit the workflow's start" in answer["reason"]


def agents_page_refunds(sessions: DatabaseSessionService, llm: ScriptedLlm) -> tuple[Worker, Children, RunPayload]:
    """A run whose Support step is an agent from the Agents page that sees
    only its own turn, and calls the Refunds workflow."""
    w = with_tools(sessions, llm, workflow_wait=30)
    children = Children(clock=w.clock)
    w.control = Starting(instance="task-1", children=children)
    agent = chat_agent()
    agent["nodes"].append(
        {"id": "refunds", "kind": "adk_workflow", "name": "Refunds", "config": {"workflow": "ag_refunds", "version": 1}}
    )
    agent["edges"].append({"id": "t", "source": "agent", "source_output": "tools", "target": "refunds"})
    config = llm_config(source="agent", agent="ca_support", version=None, message="Refund A1", inputs={})
    document = line(node("support", "llm", config), end("done", "steps.support.output"))
    return w, children, payload(document, {}, chat_agents={"ca_support": agent}, saved={"ag_refunds@1": REFUNDS})


async def test_an_agent_from_the_agents_page_calling_a_workflow_waits_for_its_run_too(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[[call("refunds", order="A1")], [text("Refunded.")]])
    w, children, run = agents_page_refunds(sessions, llm)

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    [gate] = w.control.gates
    assert (gate["details"]["kind"], gate["details"]["step"]) == ("child_run", "support")
    children.end(child, result={"refunded": "A1"})
    w.control.decide(waiting.value.key, Decision(approved=True, comment="succeeded"))
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunded."
    assert responses(llm.requests[1]) == [answered(child, "succeeded", result={"refunded": "A1"})]


async def test_a_workflow_call_an_agent_seeing_only_its_turn_makes_again_is_answered_with_the_run_that_ended(
    sessions: DatabaseSessionService,
) -> None:
    # Its call was answered with how its run ended; its step then runs again,
    # and the agent, which doesn't see that answer, calls again: it's
    # answered with the same run, at once, and starts none.
    llm = ScriptedLlm(
        turns=[[call("refunds", order="A1")], overloaded(), [call("refunds", order="A1")], [text("Refunded.")]]
    )
    w, children, run = agents_page_refunds(sessions, llm)
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    children.end(child, result={"refunded": "A1"})
    w.control.decide(waiting.value.key, Decision(approved=True, comment="succeeded"))
    with pytest.raises(TransientError):  # the model's hiccup: its step runs again
        await w.run(run)

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunded."
    assert children.started == [child]
    assert len(w.control.gates) == 1  # the call again didn't wait: its run had ended
    assert responses(llm.requests[-1])[-1] == answered(child, "succeeded", result={"refunded": "A1"})


async def test_a_workflow_call_an_agent_seeing_only_its_turn_makes_again_waits_for_the_run_it_started(
    sessions: DatabaseSessionService,
) -> None:
    # Its call was answered (its wait was up); its step then runs again, and
    # the agent, which doesn't see that answer, calls again: it waits for the
    # same run, which still goes, and starts none.
    llm = ScriptedLlm(
        turns=[[call("refunds", order="A1")], overloaded(), [call("refunds", order="A1")], [text("Refunded.")]]
    )
    w, children, run = agents_page_refunds(sessions, llm)
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [child] = children.started
    [first] = w.control.gates
    w.clock.advance(seconds=first["timeout_seconds"])  # its wait is up
    w.control.decide(waiting.value.key, Decision(approved=False, comment="Nobody decided by its deadline"))
    with pytest.raises(TransientError):  # the model's hiccup: its step runs again
        await w.run(run)

    with pytest.raises(AwaitingDecision) as again:
        await w.run(run)

    assert children.started == [child]  # its call again waits for that run, not another
    gate = next(gate for gate in w.control.gates if gate["key"] == again.value.key)
    assert gate["details"]["child_run_id"] == child
    assert gate["timeout_seconds"] == first["timeout_seconds"]  # as long as its own wait
    assert any("waits for the run its first call started" in said for said in w.control.said())
    children.end(child, result={"refunded": "A1"})
    w.control.decide(again.value.key, Decision(approved=True, comment="succeeded"))
    result = await w.run(run)
    assert result.status is JobStatus.OK, result.detail
    assert responses(llm.requests[-1])[-1] == answered(child, "succeeded", result={"refunded": "A1"})
    assert children.started == [child]


async def test_a_child_workflow_inherits_skills_and_executes_its_inline_skills_tool(
    sessions: DatabaseSessionService,
) -> None:
    skills = tool("tool_skills", "skills", "Skills", skills=["sk_refunds"])
    document = line(node("helper", "llm", llm_config(tools=[skills])), end("done", "steps.helper.output"))
    parent = payload(document, {}, saved={"ag_refunds@1": document}, skills={"sk_refunds": PLAYBOOK})
    children = Children()
    workflows = RunWorkflows(parent.model_dump(), run_id="parent", children=children, wait=0)
    context = SimpleNamespace(function_call_id="call_1", agent_name="helper")

    await workflows.run("ag_refunds@1", {}, organization_id=ORGANIZATION, context=context)
    [child] = children.started
    inherited = RunPayload.model_validate(children.runs[child]["payload"])
    assert inherited.skills == parent.skills

    llm = ScriptedLlm(turns=[[call("load_skill", skill_name="refund-playbook")], [text("Refunds within 30 days.")]])
    result = await with_tools(sessions, llm).run(inherited)
    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunds within 30 days."
    loaded = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert "Check the order is under 30 days old" in json.dumps(loaded)


async def grandchild_of(held: frozenset[int]) -> RunPayload:
    """
    The run a run's run starts, for someone holding ``held`` of permission 7:
    Refunds, whose Support step is an agent from the Agents page only those
    holding it may use. The first run's call waits apart (made in its own
    invocation); its child's, made in a sub-agent's runner, in place.
    """
    support = chat_agent(access={"permission": 7})
    config = llm_config(source="agent", agent="ca_support", version=None, message="Refund A1", inputs={})
    refunds = line(node("support", "llm", config), end("done", "steps.support.output"))
    access = caller_json(Caller(held=held, names={7: "refunds:approve"}))
    first = payload(
        refunds, {}, saved={"ag_refunds@1": refunds}, chat_agents={"ca_support": support}, agent_access=access
    )
    children = Children()

    apart = SimpleNamespace(function_call_id="call_1", agent_name="helper", invocation_id="inv-first")
    workflows = RunWorkflows(
        first.model_dump(mode="json"), run_id="first", children=children, wait=0, invocation_id="inv-first"
    )
    assert await workflows.run("ag_refunds@1", {}, organization_id=ORGANIZATION, context=apart) is None
    [child_id] = children.started
    child = RunPayload.model_validate(children.runs[child_id]["payload"])
    assert child.agent_access == access

    in_place = SimpleNamespace(function_call_id="call_2", agent_name="support", invocation_id="inv-sub-agent")
    workflows = RunWorkflows(
        child.model_dump(mode="json"), run_id=child_id, children=children, wait=0, invocation_id="inv-child"
    )
    answer = await workflows.run("ag_refunds@1", {}, organization_id=ORGANIZATION, context=in_place)
    assert answer["status"] == "running"
    grandchild = RunPayload.model_validate(children.runs[children.started[1]]["payload"])
    assert grandchild.agent_access == access
    return grandchild


async def test_a_called_workflows_runs_use_the_agents_their_first_runs_person_may_use(
    sessions: DatabaseSessionService,
) -> None:
    grandchild = await grandchild_of(frozenset({7}))
    llm = ScriptedLlm(turns=[[text("Refunded.")]])

    result = await with_tools(sessions, llm).run(grandchild)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Refunded."


async def test_a_called_workflows_runs_refuse_the_agents_their_first_runs_person_may_not_use(
    sessions: DatabaseSessionService,
) -> None:
    grandchild = await grandchild_of(frozenset())
    llm = ScriptedLlm(turns=[[text("Refunded.")]])

    result = await with_tools(sessions, llm).run(grandchild)

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == (
        "You can't use Support desk: it needs refunds:approve. Ask your organization's admins for access."
    )
    assert llm.requests == []


@pytest.mark.timeout(10)
async def test_a_called_workflow_is_waited_for_as_long_as_its_tool_says() -> None:
    children = Children()
    # The worker's default would wait a minute; the tool says a moment.
    workflows = RunWorkflows({"saved": {"ag_refunds@1": REFUNDS}}, run_id="r1", children=children, wait=60)
    context = SimpleNamespace(function_call_id="call_1", agent_name="helper")

    began = time.perf_counter()
    answer = await workflows.run("ag_refunds@1", {}, organization_id=None, context=context, wait=0.2)

    assert time.perf_counter() - began < 5
    assert answer == {"run_id": children.started[0], "status": "running"}


@pytest.mark.timeout(10)
async def test_a_called_workflows_input_its_start_cant_check_in_time_is_refused() -> None:
    start = {"type": "object", "properties": {"order": {"type": "string", "pattern": "^(a|aa)+$"}}}
    refunds = {**REFUNDS, "nodes": [{**REFUNDS["nodes"][0], "config": {"input_schema": start}}]}
    children = Children()
    workflows = RunWorkflows({"saved": {"ag_refunds@1": refunds}}, run_id="r1", children=children, wait=1)

    began = time.perf_counter()
    with pytest.raises(ValueError, match=r"^The input doesn't fit the workflow's start: its regular expression"):
        await workflows.run("ag_refunds@1", {"order": "a" * 60 + "b"}, organization_id=None, context=None)

    assert time.perf_counter() - began < 2
    assert children.started == []


async def test_a_called_workflows_input_its_start_cant_resolve_a_reference_for_is_refused() -> None:
    start = {"type": "object", "properties": {"order": {"$ref": "#/$defs/order"}}}
    refunds = {**REFUNDS, "nodes": [{**REFUNDS["nodes"][0], "config": {"input_schema": start}}]}
    children = Children()
    workflows = RunWorkflows({"saved": {"ag_refunds@1": refunds}}, run_id="r1", children=children, wait=1)

    with pytest.raises(ValueError) as refused:
        await workflows.run("ag_refunds@1", {"order": "A1"}, organization_id=None, context=None)

    assert str(refused.value) == (
        "The input doesn't fit the workflow's start: its $ref '#/$defs/order' can't be resolved: "
        "only references within the schema are read"
    )
    assert children.started == []


def test_a_model_error_says_what_its_code_means_and_how_far_it_got() -> None:
    malformed = LlmResponse(
        error_code=types.FinishReason.MALFORMED_FUNCTION_CALL,
        error_message="Arguments for the following function calls were not a valid JSON object: run_orders",
        usage_metadata=types.GenerateContentResponseUsageMetadata(candidates_token_count=4096),
    )
    said = model_error(malformed)
    assert said.startswith(
        "its model answered with an error (MALFORMED_FUNCTION_CALL) after 4,096 output tokens: "
        "Arguments for the following function calls were not a valid JSON object: run_orders. "
        "That means it called a tool with arguments that aren't a JSON object."
    )
    assert "output token limit" in said
    # A code that says what it is reads as it is.
    assert model_error(LlmResponse(error_code="SAFETY", error_message="Blocked")) == (
        "its model answered with an error (SAFETY): Blocked"
    )
