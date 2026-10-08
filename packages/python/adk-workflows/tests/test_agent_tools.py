"""
LLM agents' tools and agents from the Agents page in a run, as the worker
runs them (the task's run job, its control in memory): the Agents builder's
tools built when they're first used, an agent used whole, a tool a person
confirms pausing the run, and what a run opened closed when it stops.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from forge_agent_runtime import RuntimeServices
from forge_common.adk.models import ProviderModels
from google.adk.sessions import DatabaseSessionService
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from forge_task_adk_workflows.graph.agent_tools import AgentServices, DocumentsSearch
from forge_task_adk_workflows.graph.errors import AgentBuildError
from forge_task_adk_workflows.graph.graph import build_agent
from forge_tasks.control import AwaitingDecision, Decision
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm
from .test_graph import end, llm_config, node
from .test_task import ORGANIZATION, PROVIDERS, Worker, line, payload, worker


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
    servers: Servers | None = None,
    search: Searches | None = None,
    code: Any = None,
) -> Worker:
    """A worker whose LLM agents run on ``llm``, their tools on fakes."""
    models = ProviderModels(PROVIDERS, build=lambda _call: llm)
    w = worker(sessions, model=models)
    agents = AgentServices(
        models=models,
        runtime=RuntimeServices(environment={}, allowed_hosts=("api.example.com",), mcp_servers=servers),
        http=w.task.services.http,
        search=search,
        code=code,
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


class CodeSearches:
    """The code graph worker's search: answers its hits in the repositories asked."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.asked: list[tuple[list[str], str, int]] = []

    async def search(self, repository_ids: list[str], query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        self.asked.append((repository_ids, query, limit))
        return [h for h in self.hits if h["repository_id"] in repository_ids]


async def test_a_knowledge_base_tool_searches_a_graph_knowledge_bases_code(sessions: DatabaseSessionService) -> None:
    from forge_codegraph import citation_ref as code_ref

    llm = ScriptedLlm(turns=[[call("code", query="How are tokens signed?")], [text("With HMAC.")]])
    code = CodeSearches(
        [
            {
                "repository_id": "repo:widgets",
                "id": "entity:signer",
                "kind": "class",
                "qualified_name": "widgets.Signer",
                "file": "src/sign.py",
                "line": 4,
                "score": 0.7,
                "snippet": "class Signer:",
            }
        ]
    )
    w = with_tools(sessions, llm, search=Searches(), code=code)
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
    assert code.asked == [(["repo:widgets"], "How are tokens signed?", 3)]
    declared = llm.requests[0].config.tools[0].function_declarations  # type: ignore[index, union-attr]
    # The tool tells the model the system: its applications and how they connect.
    assert "Widgets (code of acme/widgets; how they connect: acme/web calls the API of acme/widgets)" in (
        declared[0].description or ""
    )
    answered = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    passage = answered["payload"]["passages"][0]
    assert passage["ref"] == code_ref("repo:widgets:entity:signer")
    assert (passage["document"], passage["location"]) == ("acme/widgets: src/sign.py", "src/sign.py, line 4")

    # Without the code graph's API, the tool answers the model what to set.
    llm.turns = [[call("code", query="x")], [text("I can't search the code.")]]
    llm.requests.clear()
    without = await with_tools(sessions, llm, search=Searches()).run(
        payload(document, {}, knowledge_bases=carried).model_copy(update={"session_id": "other-session"})
    )
    assert without.detail["result"] == "I can't search the code."
    refused = llm.requests[1].contents[-1].parts[0].function_response.response  # type: ignore[index, union-attr]
    assert "CODEGRAPH_URL" in str(refused)


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
