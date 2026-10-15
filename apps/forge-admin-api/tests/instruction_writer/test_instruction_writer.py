"""The instruction writer's route, without MySQL: the tables on SQLite, the
organization's access in memory, the agents' and workflows' stores faked,
and the writer on a scripted model. What it sends the model (the brief,
filled in from the organization's records, never another's), what it
answers, what it records, and what it refuses.
"""

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_common.adk.usage import UsageCall
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field, SecretStr
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.instruction_writer.writer import WriterError, parse
from forge_admin.models import KnowledgeBase, McpServer, Organization

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
MEMBER, VIEWER = "member-1", "viewer-1"
WRITE = f"/organizations/{ORG}/instruction-writer"
SERVER, OTHER_SERVER, BASE = "srv-orders", "srv-theirs", "kb-policies"

GRANTS = {
    "org:member": ("organizations:read", "agents:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {MEMBER: ("org:member", ORG), VIEWER: ("org:viewer", ORG)}

ANSWER = """<instruction>
You help Acme's customers with their orders.

## Tools
- Look an order up with `get_order` before answering about it.
</instruction>
<notes>
- Attach a refunds tool to let it refund orders.
- None
</notes>"""


class Grants(AsyncAdapter):
    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{org}*" for user, (role, org) in ROLES.items()]

    async def load_policy(self, model: Any) -> None:
        for line in self.lines:
            load_policy_line(line, model)

    async def save_policy(self, model: Any) -> bool:
        return True

    async def add_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: str
    ) -> None:
        pass


class Scripted(BaseLlm):
    """Answers each call with the next of ``turns`` (text, or an exception), with usage."""

    model: str = "scripted-model"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, LlmResponse):
            yield turn
            return
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=turn)]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1200,
                candidates_token_count=300,
                total_token_count=1500,
            ),
        )

    @property
    def sent(self) -> str:
        """What the last call was sent: its user message."""
        contents = self.requests[-1].contents
        return "".join(part.text or "" for c in contents for part in c.parts or ())

    @property
    def system(self) -> str:
        config = self.requests[-1].config
        return str(config.system_instruction if config else "")


@dataclass
class Store:
    """A chat agents' or workflows' store: its documents by (organization, ID)."""

    documents: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    async def get(
        self, organization_id: str, document_id: str
    ) -> dict[str, Any] | None:
        found = self.documents.get((organization_id, document_id))
        return {"_id": document_id, "document": found} if found else None

    async def aclose(self) -> None:
        pass


@dataclass
class Usage:
    calls: list[UsageCall] = field(default_factory=list)

    async def record(self, call: UsageCall) -> None:
        self.calls.append(call)


@dataclass
class Workspace:
    client: TestClient
    model: Scripted
    runtime: AgentRuntime
    usage: Usage
    tokens: dict[str, str]

    def write(self, body: dict[str, Any], user: str = MEMBER, path: str = WRITE) -> Any:
        return self.client.post(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
        )


def sqlite_tables() -> MetaData:
    tables = MetaData()
    for name in ("organizations", "users", "mcp_servers", "knowledge_bases"):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


def chat_agents() -> Store:
    return Store(
        {
            (ORG, "ca_billing"): {
                "name": "Billing desk",
                "nodes": [
                    {
                        "id": "agent",
                        "kind": "agent",
                        "name": "Billing Specialist",
                        "config": {"description": "Handles invoices and refunds."},
                    }
                ],
            },
            (OTHER_ORG, "ca_theirs"): {
                "name": "Their agent",
                "nodes": [
                    {
                        "id": "agent",
                        "kind": "agent",
                        "name": "Secret Agent",
                        "config": {"description": "Not yours."},
                    }
                ],
            },
        }
    )


def workflows() -> Store:
    return Store(
        {
            (ORG, "wf_returns"): {
                "name": "Start a return",
                "description": "Opens a return for an order.",
                "nodes": [
                    {
                        "id": "start",
                        "kind": "start",
                        "config": {
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "order_id": {
                                        "type": "string",
                                        "description": "The order to return.",
                                    }
                                },
                                "required": ["order_id"],
                            }
                        },
                    }
                ],
            }
        }
    )


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Grants())
    model = Scripted()
    runtime = AgentRuntime.of(
        [App(name="forge", root_agent=LlmAgent(name="forge", model=model))],
        sessions=InMemorySessionService(),
        artifacts=InMemoryArtifactService(),
    )
    usage = Usage()
    runtime.usage = usage  # type: ignore[assignment]
    app.state.agents = runtime
    app.state.chat_agents = chat_agents()
    app.state.organization_agents = workflows()

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add_all(
                [
                    Organization(id=ORG, name="Acme", description=""),
                    Organization(id=OTHER_ORG, name="Globex", description=""),
                    McpServer(
                        id=SERVER,
                        organization_id=ORG,
                        name="Orders API",
                        description="Acme's order system.",
                        url="https://orders.example/mcp",
                        tools=[
                            {
                                "name": "get_order",
                                "title": "",
                                "description": "Reads an order.",
                            },
                            {
                                "name": "cancel_order",
                                "description": "Cancels an order.",
                            },
                            {"name": "delete_everything", "description": "Not picked."},
                        ],
                    ),
                    McpServer(
                        id=OTHER_SERVER,
                        organization_id=OTHER_ORG,
                        name="Globex internals",
                        url="https://globex.example/mcp",
                        tools=[{"name": "globex_secret", "description": "Theirs."}],
                    ),
                    KnowledgeBase(
                        id=BASE,
                        organization_id=ORG,
                        name="Policies",
                        description="Returns and shipping policies.",
                        kind="system",
                    ),
                ]
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (MEMBER, VIEWER)
    }
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(create)
        yield Workspace(client, model, runtime, usage, tokens)
        client.portal.call(engine.dispose)


def brief(**changes: Any) -> dict[str, Any]:
    return {
        "where": "chat",
        "role": "chat_agent",
        "name": "Order helper",
        "description": "Answers customers' questions about their orders.",
        "container": "Support",
        "ask_person": True,
        "tools": [
            {
                "kind": "mcp",
                "name": "Orders",
                "config": {
                    "server": SERVER,
                    "tools": "get_order, cancel_order",
                    "confirm": True,
                },
            },
            {
                "kind": "knowledge_base",
                "name": "Policy search",
                "config": {"knowledge_bases": [BASE]},
            },
            {
                "kind": "http_tool",
                "name": "Track parcel",
                "config": {
                    "description": "Tracks a parcel.",
                    "method": "GET",
                    "url": "https://track.example/{code}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "The tracking code.",
                            }
                        },
                    },
                },
            },
            {"kind": "adk_workflow", "name": "", "config": {"workflow": "wf_returns"}},
            {"kind": "code_execution", "name": "", "config": {}},
            {
                "kind": "rich_answers",
                "name": "Rich answers",
                "config": {"components": ["Fact", "Form", "Input"]},
            },
            {"kind": "memory", "name": "Memory", "config": {"mode": "on_demand"}},
        ],
        "hand_offs": [
            {
                "name": "Billing",
                "description": "",
                "kind": "saved_agent",
                "agent": "ca_billing",
            },
            {
                "name": "Researcher",
                "description": "Digs into odd cases.",
                "kind": "sub_agent",
                "mode": "task",
            },
        ],
        "references": [
            {
                "path": "state.customer_name",
                "type": "string",
                "description": "Who's chatting.",
            },
        ],
    } | changes


# ---------------------------------------------------------------- writing


def test_it_writes_the_instruction_from_the_brief(workspace: Workspace) -> None:
    workspace.model.turns.append(ANSWER)
    written = workspace.write(
        {
            "ask": "Help customers with orders; billing goes to Billing.",
            "brief": brief(),
        }
    )
    assert written.status_code == 200, written.json()
    draft = written.json()
    assert draft["instruction"].startswith("You help Acme's customers")
    assert "</instruction>" not in draft["instruction"]
    assert draft["notes"] == ["Attach a refunds tool to let it refund orders."]
    assert draft["model"] == "scripted-model"

    assert "You write the system instruction" in workspace.model.system
    # Its own instruction is a provider: ADK leaves its {{ }} examples alone.
    assert "The customer wrote: {{ input.message }}" in workspace.model.system
    sent = workspace.model.sent
    # Its tools, by the names the model calls them, filled in from the records.
    assert 'MCP server "Orders API" (Acme\'s order system.)' in sent
    assert "`get_order`: Reads an order." in sent
    assert "`cancel_order`" in sent
    assert "delete_everything" not in sent
    assert "A person confirms each call" in sent
    assert (
        "`policy_search(query)`" in sent and '"Policies" (Returns and shipping' in sent
    )
    assert "`policy_search_system_design(focus?)`" in sent
    assert "`track_parcel`: Tracks a parcel. (GET https://track.example/{code})" in sent
    assert "`code` (string): The tracking code." in sent
    assert "`start_a_return`: Opens a return for an order." in sent
    assert "`order_id` (string, required)" in sent
    assert "`run_code(code)`" in sent
    assert (
        "`present(...)`: shows the person a composition the chat draws, of the components its "
        "Rich answers node picks (Fact, Input, Form)" in sent
    )
    assert "`load_memory(query)`" in sent
    # Who it hands off to, as ADK names them.
    assert '`billing_specialist` (the Agents page\'s "Billing desk")' in sent
    assert 'transfer_to_agent(agent_name="billing_specialist")' in sent
    assert "Handles invoices and refunds." in sent
    assert "the tool `researcher(request)` gives it a task" in sent
    assert "`{{ state.customer_name }}` (string): Who's chatting." in sent
    assert "It can stop to ask the person" in sent
    assert "None yet: write a new one." in sent
    assert sent.rstrip().endswith(
        "Help customers with orders; billing goes to Billing."
    )


def test_the_current_instruction_is_reworked(workspace: Workspace) -> None:
    workspace.model.turns.append("<instruction>Shorter.</instruction>")
    written = workspace.write(
        {"ask": "Make it shorter.", "current": "You are long-winded.", "brief": brief()}
    )
    assert written.status_code == 200
    assert written.json() == {
        "instruction": "Shorter.",
        "notes": [],
        "model": "scripted-model",
    }
    assert "<current>\nYou are long-winded.\n</current>" in workspace.model.sent


def test_a_workflow_agent_is_described_as_one(workspace: Workspace) -> None:
    workspace.model.turns.append(ANSWER)
    workflow_brief = brief(
        where="workflow",
        role="workflow_agent",
        mode="single_turn",
        ask_person=False,
        files=True,
        output_schema={"type": "object", "properties": {"reply": {"type": "string"}}},
        tools=[],
        hand_offs=[
            {
                "id": "triage",
                "name": "Triage",
                "description": "Sorts it.",
                "kind": "llm",
            }
        ],
        references=[{"path": "input.order_id", "type": "string"}],
    )
    assert (
        workspace.write({"ask": "Draft a reply.", "brief": workflow_brief}).status_code
        == 200
    )
    sent = workspace.model.sent
    assert 'an LLM agent step in a Forge workflow, part of "Support"' in sent
    assert "it runs once with what it's given" in sent
    assert "No person is there to answer questions" in sent
    assert '{"type":"object","properties":{"reply":{"type":"string"}}}' in sent
    assert "`load_artifacts`" in sent and "`save_file(name, content)`" in sent
    assert "its answer is kept as `state.triage`" in sent
    assert "`{{ input.order_id }}` (string)" in sent


def test_another_organizations_records_are_never_read(workspace: Workspace) -> None:
    workspace.model.turns.append(ANSWER)
    theirs = brief(
        tools=[
            {"kind": "mcp", "name": "Theirs", "config": {"server": OTHER_SERVER}},
            {"kind": "saved_agent", "name": "Spy", "config": {"agent": "ca_theirs"}},
        ],
        hand_offs=[],
    )
    assert workspace.write({"ask": "Anything.", "brief": theirs}).status_code == 200
    sent = workspace.model.sent
    assert "globex" not in sent.lower()
    assert "Secret" not in sent and "secret_agent" not in sent
    # They're described from what the brief said, by the names it gave.
    assert "`spy(request)`" in sent


def test_its_calls_count_as_the_assistants_for_the_organization(
    workspace: Workspace,
) -> None:
    workspace.model.turns.append(ANSWER)
    assert workspace.write({"ask": "Help.", "brief": brief()}).status_code == 200
    [call] = workspace.usage.calls
    assert call.call == "model"
    assert call.author == "instruction_writer"
    assert (call.attribution.organization_id, call.attribution.kind) == (
        ORG,
        "assistant",
    )
    assert call.attribution.subject_id == "assistant"
    assert call.attribution.user_id == MEMBER
    assert call.input_tokens == 1200


# ---------------------------------------------------------------- refusals


def test_it_needs_agents_manage(workspace: Workspace) -> None:
    refused = workspace.write({"ask": "Help.", "brief": brief()}, user=VIEWER)
    assert refused.status_code == 403
    assert workspace.model.requests == []


def test_it_refuses_an_empty_ask_and_unknown_fields(workspace: Workspace) -> None:
    assert workspace.write({"ask": "   ", "brief": brief()}).status_code == 422
    assert (
        workspace.write({"ask": "Hi", "brief": brief(), "model": "x"}).status_code
        == 422
    )
    assert (
        workspace.write({"ask": "Hi", "brief": brief(role="boss")}).status_code == 422
    )


def test_it_isnt_there_when_the_assistant_isnt_set_up(workspace: Workspace) -> None:
    workspace.runtime.unavailable = "The assistant needs a key."
    refused = workspace.write({"ask": "Help.", "brief": brief()})
    assert refused.status_code == 503
    assert refused.json()["detail"] == "The assistant needs a key."


def test_a_failing_or_silent_model_is_a_bad_gateway(workspace: Workspace) -> None:
    workspace.model.turns += [RuntimeError("boom"), "<notes>- only notes</notes>"]
    failed = workspace.write({"ask": "Help.", "brief": brief()})
    assert failed.status_code == 502
    assert failed.json()["detail"] == "The writer ran into a problem. Try again."
    silent = workspace.write({"ask": "Help.", "brief": brief()})
    assert silent.status_code == 502
    assert "without an instruction" in silent.json()["detail"]


def test_a_model_that_stops_says_why(workspace: Workspace) -> None:
    workspace.model.turns.append(
        LlmResponse(error_code="MAX_TOKENS", error_message="Too long.")
    )
    stopped = workspace.write({"ask": "Help.", "brief": brief()})
    assert stopped.status_code == 502
    assert stopped.json()["detail"] == (
        "The model stopped without writing it (MAX_TOKENS); try again."
    )


# ---------------------------------------------------------------- reading the answer


def test_parse_reads_tags_fences_and_bare_answers() -> None:
    assert parse("<instruction>\n```markdown\nYou do X.\n```\n</instruction>") == (
        "You do X.",
        [],
    )
    assert parse("You do Y.") == ("You do Y.", [])
    assert parse("<instruction>Cut off mid") == ("Cut off mid", [])
    instruction, notes = parse(
        "<instruction>Z</instruction><notes>\n* one\n- two\n• three\n- four\n- five\n</notes>"
    )
    assert (instruction, notes) == ("Z", ["one", "two", "three", "four"])
    with pytest.raises(WriterError):
        parse("<instruction>  </instruction>")


def test_a_server_set_up_by_url_goes_by_its_name_not_its_url(
    workspace: Workspace,
) -> None:
    workspace.model.turns += [ANSWER, ANSWER]
    by_url = {
        "kind": "mcp",
        "name": "Help center",
        "config": {"url": "https://help.example/mcp?key=s3cret"},
    }
    assert (
        workspace.write({"ask": "Help.", "brief": brief(tools=[by_url])}).status_code
        == 200
    )
    sent = workspace.model.sent
    assert "help.example" not in sent and "s3cret" not in sent
    assert 'MCP server "Help center"; their names aren\'t known here' in sent
    picked = by_url | {
        "config": {"url": "https://help.example/mcp", "tools": "search, read"}
    }
    assert (
        workspace.write({"ask": "Help.", "brief": brief(tools=[picked])}).status_code
        == 200
    )
    assert 'MCP server "Help center": `read`, `search`.' in workspace.model.sent
