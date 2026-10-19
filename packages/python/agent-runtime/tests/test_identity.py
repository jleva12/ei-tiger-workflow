"""Who a call is, and what it may reach: callers let in by an agent's ID and
organization before anything of it is read, conversations kept under who the
caller is rather than what the URL says, every turn checked against the chat
agent's rule, hand-offs holding the rules above them, saved agents in the
organization of the agent using them, published versions never running a
workflow's draft, and HTTP tools' hosts never the model's to pick."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from forge_agent_runtime import AgentServer, AgentServerSettings
from forge_agent_runtime.access import (
    AccessRefused,
    Caller,
    HeldAccess,
    all_of,
    any_of,
)
from forge_agent_runtime.build import BuildError, build_app
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    DocumentAgentSource,
    LocatedAgent,
    ResolvedAgent,
    RunRequest,
)
from forge_agent_runtime.http_templates import TemplateProblem, UrlTemplate
from forge_agent_runtime.server import CANT_BUILD, MAX_USER_ID, create_router, owned_by
from forge_agent_runtime.services import RuntimeServices
from tests.conftest import call, text
from tests.test_intake import Chat

READ, WRITE = 1, 2
NAMES = {READ: "finance:read", WRITE: "finance:write"}


class Holding:
    """What whoever the call is for holds, changed between turns: access taken away."""

    def __init__(self, *held: int) -> None:
        self.held = frozenset(held)

    async def __call__(self, wanted: frozenset[int], *, organization_id: str | None) -> Caller:
        return Caller(self.held, NAMES)


def agent(
    agent_id: str = "ca_desk",
    *,
    entry: Any = None,
    organization: str | None = "org_1",
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    version: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": "forge.chat_agent/v1",
        "id": agent_id,
        "name": "Desk",
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "name": "Front",
                "config": {"instruction": "Help.", "access": entry},
            },
            *(nodes or []),
        ],
        "edges": edges or [],
        **extra,
    }
    if organization is not None:
        doc["organization_id"] = organization
    if version is not None:
        doc["version"] = version
    return doc


def sub(node_id: str, name: str, access: Any = None) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "sub_agent",
        "name": name,
        "config": {"description": f"{name}.", "instruction": f"Be {name}.", "mode": "chat", "access": access},
    }


def edge(source: str, target: str, way: str = "agents") -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "source_output": way, "target": target}


# Rules combine without nesting deeper --------------------------------------------------


def test_rules_combine_flat() -> None:
    read, write = {"permission": READ}, {"permission": WRITE}
    assert all_of(None, None) is None
    assert all_of(read, None) == read
    assert all_of({"all": [read, write]}, read) == {"all": [read, write]}
    assert all_of({"all": [read]}, {"any": [write]}) == {"all": [read, {"any": [write]}]}
    # Anyone may when one of the ways in needs nothing.
    assert any_of(read, None) is None
    assert any_of() is None
    assert any_of({"any": [read]}, write, read) == {"any": [read, write]}


# Hand-offs hold the rules above them ------------------------------------------------------


async def test_a_conversation_handed_on_stops_when_access_above_it_is_taken_away(models, llm) -> None:
    # Front hands off to Coordinator (finance:read), which hands off to Billing (no rule of its own).
    doc = agent(
        nodes=[sub("sub_coordinator", "Coordinator", {"permission": READ}), sub("sub_billing", "Billing")],
        edges=[edge("agent", "sub_coordinator"), edge("sub_coordinator", "sub_billing")],
    )
    holding = Holding(READ)
    app = await build_app(parse_document(doc), models=models, services=RuntimeServices(access=holding))
    chat = Chat(app)
    llm.turns = [
        [call("transfer_to_agent", agent_name="coordinator")],
        [call("transfer_to_agent", agent_name="billing")],
        [text("Billing here.")],
    ]
    await chat.say(text("My invoice"))
    asked = len(llm.requests)

    # finance:read is taken away: the next turn goes straight to Billing, which says no.
    holding.held = frozenset()
    events = await chat.say(text("And the other one?"))
    said = [
        p.text for e in events for p in (e.content.parts if e.content and e.content.parts else []) if p.text
    ]
    assert said == [
        "You can't use Billing: it needs finance:read. Ask your organization's admins for access."
    ]
    assert len(llm.requests) == asked


# Every turn starts only when the chat agent's rule lets them in --------------------------


def executor_of(doc: dict[str, Any], models: Any, **services: Any) -> AgentExecutor:
    return AgentExecutor(
        DocumentAgentSource(parse_document(doc)),
        models=models,
        services=RuntimeServices(**services),
        http=httpx.AsyncClient(),
    )


async def test_the_executor_refuses_a_turn_its_chat_agent_doesnt_let_them_in_to(models, llm) -> None:
    executor = executor_of(agent(entry={"permission": READ}), models, access=HeldAccess(Caller(names=NAMES)))
    resolved = await executor.resolve("ca_desk")
    assert await executor.refusal(resolved) == (
        "You can't use Front: it needs finance:read. Ask your organization's admins for access."
    )
    session = await executor.sessions.create_session(app_name="ca_desk", user_id="ada")
    request = RunRequest(app_name="ca_desk", user_id="ada", session_id=session.id)
    with pytest.raises(AccessRefused, match="it needs finance:read"):
        async for _ in executor.run(request):
            pass
    assert llm.requests == []
    await executor.close()


async def test_rules_the_runtime_adds_hold_beside_the_nodes_own(models, llm) -> None:
    doc = parse_document(agent()).model_copy(update={"required_rules": {"agent": {"permission": WRITE}}})
    executor = AgentExecutor(
        DocumentAgentSource(doc),
        models=models,
        services=RuntimeServices(access=HeldAccess(Caller(frozenset({READ}), NAMES))),
        http=httpx.AsyncClient(),
    )
    resolved = await executor.resolve("ca_desk")
    assert "it needs finance:write" in str(await executor.refusal(resolved))
    # They aren't part of the document as it's exported.
    assert "required_rules" not in resolved.document.to_json()
    await executor.close()


# The run API: let in before anything is read, conversations the caller's own ------------


class Source:
    """Agents by organization; one of them can't be built, and says why only to the log."""

    def __init__(self, models: Any) -> None:
        self.read: list[str] = []
        self.docs = {
            "ca_desk": parse_document(agent()),
            "ca_theirs": parse_document(agent("ca_theirs", organization="org_2")),
        }

    async def locate(self, ref: AgentRef) -> LocatedAgent:
        if ref.agent_id == "ca_broken":
            return LocatedAgent(ref, "org_1")
        doc = self.docs.get(ref.agent_id)
        if doc is None:
            raise AgentNotFound(f"There's no agent {ref.agent_id}.")
        return LocatedAgent(ref, doc.organization_id)

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        self.read.append(ref.agent_id)
        if ref.agent_id == "ca_broken":
            raise BuildError("its MCP server https://secret.internal/mcp refused the token")
        doc = self.docs[ref.agent_id]
        return ResolvedAgent(doc, doc.version, ref.agent_id)


@pytest.fixture
def source(models: Any) -> Source:
    return Source(models)


@pytest.fixture
def client(source: Source, models: Any) -> Any:
    executor = AgentExecutor(source, models=models, http=httpx.AsyncClient())  # type: ignore[arg-type]

    async def admit(request: Request, agent: LocatedAgent, *, action: str) -> None:
        # Org 1's callers only: anything else is no agent they know of.
        if agent.organization_id != "org_1":
            raise HTTPException(404, "There's no such agent.")

    async def owner(request: Request, user_id: str) -> str:
        return owned_by(request.headers["x-caller"], user_id)

    app = FastAPI()
    app.include_router(create_router(executor, admit=admit, owner=owner))
    with TestClient(app) as client:
        yield client


def test_another_organizations_agent_is_refused_before_it_is_read(client: TestClient, source: Source) -> None:
    refused = client.get("/apps/ca_theirs", headers={"x-caller": "apikey:k1"})
    assert refused.status_code == 404
    assert source.read == []
    assert client.get("/apps/ca_desk", headers={"x-caller": "apikey:k1"}).status_code == 200


def test_an_agent_that_cant_be_built_doesnt_say_why(client: TestClient) -> None:
    refused = client.get("/apps/ca_broken", headers={"x-caller": "apikey:k1"})
    assert refused.status_code == 422
    assert refused.json()["detail"] == CANT_BUILD
    assert "secret.internal" not in refused.text


def test_conversations_are_kept_under_the_caller(client: TestClient, source: Source) -> None:
    sessions = "/apps/ca_desk/users/alice/sessions"
    made = client.post(sessions, json={}, headers={"x-caller": "apikey:k1"})
    assert made.status_code == 201
    # Shown as the call named its user...
    assert made.json()["userId"] == "alice"
    mine = client.get(sessions, headers={"x-caller": "apikey:k1"}).json()
    assert [s["id"] for s in mine] == [made.json()["id"]]
    # ...but another caller naming the same user reaches none of them.
    assert client.get(sessions, headers={"x-caller": "apikey:k2"}).json() == []
    one = f"{sessions}/{made.json()['id']}"
    assert client.get(one, headers={"x-caller": "apikey:k2"}).status_code == 404
    assert client.get(one, headers={"x-caller": "apikey:k1"}).json()["userId"] == "alice"
    # Nor runs a turn in it.
    turn = client.post(
        "/run_sse",
        json={"appName": "ca_desk", "userId": "alice", "sessionId": made.json()["id"], "newMessage": None},
        headers={"x-caller": "apikey:k2"},
    )
    assert turn.status_code == 404
    client.delete(one, headers={"x-caller": "apikey:k2"})
    assert client.get(one, headers={"x-caller": "apikey:k1"}).status_code == 200


def test_a_turn_its_chat_agent_doesnt_let_them_in_to_is_refused_before_it_streams(models, llm) -> None:
    executor = executor_of(agent(entry={"permission": READ}), models, access=HeldAccess(Caller(names=NAMES)))
    app = FastAPI()
    app.include_router(create_router(executor))
    with TestClient(app) as client:
        made = client.post("/apps/ca_desk/users/ada/sessions", json={}).json()
        refused = client.post(
            "/run_sse",
            json={
                "appName": "ca_desk",
                "userId": "ada",
                "sessionId": made["id"],
                "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
    assert refused.status_code == 403
    assert "it needs finance:read" in refused.json()["detail"]
    assert llm.requests == []


def test_an_end_users_id_too_long_to_keep_is_kept_as_a_hash() -> None:
    caller = "apikey:" + "a" * 36
    assert owned_by(caller, caller) == caller
    assert owned_by(caller, "alice") == f"{caller}/alice"
    long = owned_by(caller, "x" * 200)
    assert len(long) <= MAX_USER_ID
    assert long != owned_by(caller, "y" * 200)
    assert long.startswith(f"{caller}/#")


# A2A: let in before anything is read too ----------------------------------------------


def test_a2a_lets_callers_in_before_reading_the_agent(source: Source, models: Any) -> None:
    from forge_agent_runtime.a2a import create_a2a_router

    executor = AgentExecutor(source, models=models, http=httpx.AsyncClient())  # type: ignore[arg-type]

    async def admit(request: Request, agent: LocatedAgent, *, action: str) -> None:
        if agent.organization_id != "org_1":
            raise HTTPException(404, "There's no such agent.")

    app = FastAPI()
    app.include_router(create_a2a_router(executor, path="/a2a", admit=admit))
    with TestClient(app) as client:
        assert client.get("/a2a/ca_theirs/.well-known/agent-card.json").status_code == 404
        assert source.read == []
        broken = client.get("/a2a/ca_broken/.well-known/agent-card.json")
        assert broken.status_code == 422
        assert "secret.internal" not in broken.text


# Saved agents run in the organization of the agent using them ----------------------------


class Workflows:
    """Says what any workflow is, noting whose it was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    async def describe(
        self, workflow_id: str, *, organization_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        self.asked.append((workflow_id, organization_id))
        return "Refund", "Refunds an order.", {"type": "object"}

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("not run here")

    async def progress(self, **kwargs: Any) -> dict[str, Any] | None:
        return None


def workflow_node(version: Any = None) -> dict[str, Any]:
    return {
        "id": "refund",
        "kind": "adk_workflow",
        "name": "Refund",
        "config": {"workflow": "ag_refund", "version": version},
    }


def using_saved(saved_organization: str | None) -> dict[str, Any]:
    billing = agent(
        "ca_billing",
        organization=saved_organization,
        nodes=[workflow_node()],
        edges=[edge("agent", "refund", "tools")],
    )
    return agent(
        nodes=[{"id": "saved", "kind": "saved_agent", "name": "Billing", "config": {"agent": "ca_billing"}}],
        edges=[edge("agent", "saved")],
        dependencies={"ca_billing@1": billing},
    )


async def test_a_saved_agent_runs_in_the_organization_of_the_agent_using_it(models) -> None:
    workflows = Workflows()
    await build_app(
        parse_document(using_saved(None)), models=models, services=RuntimeServices(workflows=workflows)
    )
    assert workflows.asked == [("ag_refund", "org_1")]


async def test_another_organizations_saved_agent_is_refused(models) -> None:
    with pytest.raises(BuildError, match="isn't one of this organization's agents"):
        await build_app(
            parse_document(using_saved("org_2")),
            models=models,
            services=RuntimeServices(workflows=Workflows()),
        )


# A published version never runs a workflow's draft ---------------------------------------


@pytest.mark.parametrize(("version", "builds"), [(3, False), ("draft", True), (None, True)])
async def test_a_published_version_never_runs_a_workflows_draft(models, version, builds) -> None:
    doc = agent(nodes=[workflow_node("draft")], edges=[edge("agent", "refund", "tools")], version=version)
    services = RuntimeServices(workflows=Workflows())
    if builds:
        await build_app(parse_document(doc), models=models, services=services)
    else:
        with pytest.raises(BuildError, match="runs ag_refund's draft, which a published agent can't"):
            await build_app(parse_document(doc), models=models, services=services)


# HTTP tools: where they call is their author's to say ------------------------------------


@pytest.mark.parametrize(
    ("url", "headers", "problem"),
    [
        ("https://api.example.com/orders/{id}", True, None),
        ("https://api.example.com?q={q}", True, None),
        ("https://api.example.com", True, None),
        ("https://${HOST}/orders/{id}", True, None),
        ("https://{host}/orders", False, "lets the model pick where it calls"),
        ("https://api.example.com{suffix}/x", False, "lets the model pick where it calls"),
        ("{scheme}://api.example.com/", False, "lets the model pick where it calls"),
        ("https://{user}@api.example.com/", False, "lets the model pick where it calls"),
        ("{{ state.base }}/orders/{id}", False, None),
        ("{{ state.base }}/orders/{id}", True, "takes its host from a run's value"),
    ],
)
def test_an_http_tools_host_is_never_the_models_to_pick(url: str, headers: bool, problem: str | None) -> None:
    template = UrlTemplate.parse(url, lambda name: "api.example.com")
    if problem is None:
        template.check_origin(headers=headers)
    else:
        with pytest.raises(TemplateProblem, match=problem):
            template.check_origin(headers=headers)


async def test_a_tool_whose_host_the_model_picks_isnt_built(models) -> None:
    tool = {
        "id": "orders",
        "kind": "http_tool",
        "name": "Orders",
        "config": {
            "method": "GET",
            "url": "https://{host}/orders",
            "headers": [{"id": "h1", "name": "Authorization", "value": "Bearer ${TOKEN}"}],
        },
    }
    doc = agent(nodes=[tool], edges=[edge("agent", "orders", "tools")])
    with pytest.raises(BuildError, match="lets the model pick where it calls"):
        await build_app(
            parse_document(doc), models=models, services=RuntimeServices(environment={"TOKEN": "secret"})
        )


# The standalone server: each key a caller of its own ---------------------------------------


def test_each_key_keeps_its_own_conversations(tmp_path: Path, example: dict[str, Any], models: Any) -> None:
    from tests.conftest import PLAIN, without

    doc = without(example, *PLAIN)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    server = (
        AgentServer(AgentServerSettings(_env_file=None))  # type: ignore[call-arg]
        .with_agent(path)
        .with_models(models)
        .with_api_keys("k1", "k2")
    )
    sessions = f"/api/apps/{doc['id']}/users/alice/sessions"
    with TestClient(server.build()) as client:
        made = client.post(sessions, json={}, headers={"Authorization": "Bearer k1"})
        assert made.status_code == 201
        assert made.json()["userId"] == "alice"
        assert len(client.get(sessions, headers={"X-API-Key": "k1"}).json()) == 1
        assert client.get(sessions, headers={"X-API-Key": "k2"}).json() == []
