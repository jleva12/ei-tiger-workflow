"""Who may call the runtime's agents: an API key or a sign-in holding
agents:run in the agent's organization (agents:manage for its draft), let in
by the agent's ID and organization before anything of it is read, into
conversations of their own: a person's are theirs, a key's are its end
users', kept apart from every other key's."""

import json
from contextlib import nullcontext
from typing import Any

from agent_access_world import BUILDER, add_builder
from forge_agent_runtime import RunRequest
from key_world import (
    ADMIN,
    AGENT,
    MEMBER,
    NEIGHBOUR,
    ORG,
    OTHER_AGENT,
    RUNTIME,
    VIEWER,
    World,
    agent_document,
)
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import set_caller
from forge_admin.models import Organization
from forge_admin.overview.recording import AgentUsage

CARD = f"/a2a/{AGENT}/.well-known/agent-card.json"
SESSIONS = f"/apps/{AGENT}/users/{{}}/sessions"


def runtime(
    world: World,
    method: str,
    path: str,
    user: str | None = None,
    body: Any = None,
    **kw: Any,
) -> Any:
    return world.call(method, path, user, body, prefix=RUNTIME, **kw)


def list_tasks(world: World, **kw: Any) -> dict[str, Any]:
    response = runtime(
        world,
        "POST",
        f"/a2a/{AGENT}",
        body={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}},
        headers={"A2A-Version": "1.0"},
        **kw,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def turn(user_id: str, session_id: str, app: str = AGENT) -> dict[str, Any]:
    return {
        "appName": app,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
    }


# ------------------------------------------------------------ callers


def test_the_runtime_needs_a_caller(world: World) -> None:
    refused = runtime(world, "GET", f"/apps/{AGENT}")
    assert refused.status_code == 401
    assert refused.headers["WWW-Authenticate"] == "Bearer"
    assert runtime(world, "GET", CARD).status_code == 401
    # Refused before the agent is built (its model would fail to).
    assert runtime(world, "POST", "/run_sse", body=turn("x", "s")).status_code == 401
    made = runtime(world, "POST", SESSIONS.format("customer-1"), body={})
    assert made.status_code == 401
    # There's no switch letting anyone in.
    assert "agent_runtime_public" not in world.call("GET", "/info", MEMBER).json()


def test_orphaned_agents_answer_nobody(world: World) -> None:
    # SQL deletion may succeed while external document cleanup fails. The
    # remaining source record must not authorize any runtime entry point.
    async def delete_organization(session: AsyncSession) -> None:
        organization = await session.get(Organization, ORG)
        assert organization is not None
        await session.delete(organization)
        await session.commit()

    secret = world.make_key(name="CI")["secret"]
    world.run(delete_organization)
    assert runtime(world, "GET", f"/apps/{AGENT}", MEMBER).status_code == 404
    assert runtime(world, "GET", CARD, MEMBER).status_code == 404
    assert runtime(world, "GET", f"/apps/{AGENT}", key=secret).status_code == 404
    assert runtime(world, "GET", f"/apps/{OTHER_AGENT}", NEIGHBOUR).status_code == 200


def test_people_need_agents_run_in_the_agents_organization(world: World) -> None:
    assert runtime(world, "GET", f"/apps/{AGENT}", MEMBER).status_code == 200
    assert runtime(world, "GET", f"/apps/{AGENT}", ADMIN).status_code == 200
    # In the organization without agents:run: refused.
    assert runtime(world, "GET", f"/apps/{AGENT}", VIEWER).status_code == 403
    # Not in it at all: there's no such agent, as for one there isn't.
    other = runtime(world, "GET", f"/apps/{AGENT}", NEIGHBOUR)
    missing = runtime(world, "GET", "/apps/ca_nosuchagent", NEIGHBOUR)
    assert other.status_code == missing.status_code == 404
    assert runtime(world, "GET", f"/apps/{OTHER_AGENT}", NEIGHBOUR).status_code == 200


def test_keys_call_only_their_organizations_agents_as_their_role_allows(
    world: World,
) -> None:
    caller = world.make_key(name="Caller")["secret"]
    viewer = world.make_key(name="Viewer", role="org:viewer")["secret"]
    assert runtime(world, "GET", f"/apps/{AGENT}", key=caller).status_code == 200
    assert (
        runtime(
            world, "GET", f"/apps/{AGENT}", key=caller, header="x-api-key"
        ).status_code
        == 200
    )
    assert runtime(world, "GET", f"/apps/{AGENT}", key=viewer).status_code == 403
    # Another organization's agent is one there isn't, over A2A too.
    other = runtime(world, "GET", f"/apps/{OTHER_AGENT}", key=caller)
    assert other.status_code == 404
    assert "Globex" not in other.text
    a2a = f"/a2a/{OTHER_AGENT}/.well-known/agent-card.json"
    assert runtime(world, "GET", a2a, key=caller).status_code == 404
    card = runtime(world, "GET", CARD, key=caller).json()
    assert set(card["securitySchemes"]) == {"bearer", "apiKey"}
    assert "result" in list_tasks(world, key=caller)
    # A credential that isn't one is refused, not ignored.
    assert (
        runtime(world, "GET", f"/apps/{AGENT}", key="fk_" + "x" * 43).status_code == 401
    )


def unbuildable(agent_id: str, organization_id: str) -> dict[str, Any]:
    """An agent that parses but can't be built: the model picks its tool's host."""
    doc = agent_document(agent_id, organization_id, "Broken helper")
    doc["nodes"].append(
        {
            "id": "leak",
            "kind": "http_tool",
            "name": "Leak",
            "config": {
                "url": "https://{host}/secret-internal-path",
                "headers": [{"id": "h", "name": "Authorization", "value": "Bearer x"}],
            },
        }
    )
    doc["edges"] = [
        {"id": "e", "source": "agent", "source_output": "tools", "target": "leak"}
    ]
    return doc


def test_an_agent_that_cant_be_built_doesnt_say_why(
    world: World, tmp_path: Any
) -> None:
    folder = tmp_path / "agents"
    (folder / f"{AGENT}.json").write_text(json.dumps(unbuildable(AGENT, ORG)))
    (folder / f"{OTHER_AGENT}.json").write_text(
        json.dumps(unbuildable(OTHER_AGENT, "3f6c0000-0000-4000-8000-000000000002"))
    )
    secret = world.make_key(name="CI")["secret"]
    made = runtime(world, "POST", SESSIONS.format("u1"), body={}, key=secret)
    assert made.status_code == 201
    refused = runtime(
        world, "POST", "/run_sse", body=turn("u1", made.json()["id"]), key=secret
    )
    assert refused.status_code == 422
    assert "secret-internal-path" not in refused.text
    assert "can't be built" in refused.json()["detail"]
    # Another organization's broken agent is one there isn't: never read.
    other = runtime(world, "GET", f"/apps/{OTHER_AGENT}", key=secret)
    assert other.status_code == 404
    assert "secret-internal-path" not in other.text


def test_a_draft_is_for_those_who_edit_the_organizations_agents(
    world: World, tmp_path: Any
) -> None:
    draft = {**agent_document(AGENT, ORG, "Acme helper"), "version": "draft"}
    (tmp_path / "agents" / f"{AGENT}@draft.json").write_text(json.dumps(draft))
    add_builder(world)
    app = f"/apps/{AGENT}@draft"
    # agents:run isn't enough: drafts are tried by whoever edits them.
    assert runtime(world, "GET", app, MEMBER).status_code == 403
    caller = world.make_key(name="Caller")["secret"]
    assert runtime(world, "GET", app, key=caller).status_code == 403
    assert (
        runtime(
            world, "GET", f"/a2a/{AGENT}@draft{CARD[len(f'/a2a/{AGENT}') :]}", MEMBER
        ).status_code
        == 403
    )
    assert runtime(world, "GET", app, BUILDER).status_code == 200
    # Its published version is anyone's with agents:run.
    assert runtime(world, "GET", f"/apps/{AGENT}", MEMBER).status_code == 200


# ------------------------------------------------------------ conversations


def test_a_persons_conversations_are_their_own(world: World) -> None:
    made = runtime(world, "POST", SESSIONS.format(MEMBER), MEMBER, {})
    assert made.status_code == 201
    assert made.json()["userId"] == MEMBER
    # Naming anyone else: refused, whoever's they'd be.
    for user in (ADMIN, "customer-1", f"apikey:{'x' * 36}"):
        assert runtime(world, "GET", SESSIONS.format(user), MEMBER).status_code == 403
        assert (
            runtime(world, "POST", "/run_sse", MEMBER, turn(user, "s")).status_code
            == 403
        )
    # Someone else can't reach theirs by naming them.
    one = f"{SESSIONS.format(MEMBER)}/{made.json()['id']}"
    assert runtime(world, "GET", one, ADMIN).status_code == 403
    assert runtime(world, "DELETE", one, ADMIN).status_code == 403
    assert runtime(world, "GET", one, MEMBER).status_code == 200


def test_a_keys_end_users_conversations_are_the_keys_alone(world: World) -> None:
    website = world.make_key(name="Website")["secret"]
    kiosk = world.make_key(name="Kiosk")["secret"]
    made = runtime(world, "POST", SESSIONS.format("customer-42"), body={}, key=website)
    assert made.status_code == 201
    # Shown as the key named its end user.
    assert made.json()["userId"] == "customer-42"
    listed = runtime(world, "GET", SESSIONS.format("customer-42"), key=website)
    assert [s["id"] for s in listed.json()] == [made.json()["id"]]
    # Another key of the same organization naming the same end user reaches none of them.
    assert runtime(world, "GET", SESSIONS.format("customer-42"), key=kiosk).json() == []
    one = f"{SESSIONS.format('customer-42')}/{made.json()['id']}"
    assert runtime(world, "GET", one, key=kiosk).status_code == 404
    assert (
        runtime(
            world,
            "POST",
            "/run_sse",
            body=turn("customer-42", made.json()["id"]),
            key=kiosk,
        ).status_code
        == 404
    )
    assert runtime(world, "DELETE", one, key=kiosk).status_code == 204
    assert runtime(world, "GET", one, key=website).status_code == 200
    # Nor does a person naming the key's end user, or the key's own ID.
    assert (
        runtime(world, "GET", SESSIONS.format("customer-42"), MEMBER).status_code == 403
    )
    # A key naming a Forge person reaches only its own end user of that name.
    assert runtime(world, "GET", SESSIONS.format(MEMBER), key=website).json() == []
    runtime(world, "POST", SESSIONS.format(MEMBER), MEMBER, {})
    assert runtime(world, "GET", SESSIONS.format(MEMBER), key=website).json() == []


def test_workflow_runs_are_followed_only_in_their_own_conversation(
    world: World,
) -> None:
    secret = world.make_key(name="Website")["secret"]
    made = runtime(world, "POST", SESSIONS.format("customer-1"), body={}, key=secret)
    follow = f"{SESSIONS.format('customer-1')}/{made.json()['id']}/workflow-runs/call-1"
    # No workflows here: none to follow, whoever asks; and nobody else's to ask about.
    assert runtime(world, "GET", follow, key=secret).status_code == 404
    other = world.make_key(name="Other")["secret"]
    assert runtime(world, "GET", follow, key=other).status_code == 404
    assert runtime(world, "GET", follow, MEMBER).status_code == 403


# ------------------------------------------------------------ usage


class Recorded:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invocation(self, attribution: Any, **fields: Any) -> Any:
        self.calls.append(fields)
        return nullcontext()


def test_a_known_callers_turns_are_theirs_in_the_usage(world: World) -> None:
    executor = world.client.app.state.agent_executor  # type: ignore[attr-defined]
    store = Recorded()
    usage = AgentUsage(store)  # type: ignore[arg-type]
    request = RunRequest.model_validate(
        {
            "appName": AGENT,
            "userId": "customer-1",
            "sessionId": "s",
            "newMessage": {"role": "user", "parts": []},
        }
    )
    resolved = world.client.portal.call(executor.resolve, AGENT)  # type: ignore[union-attr]

    set_caller(None, ())
    usage.invocation(resolved, request)
    set_caller("apikey:abc", ())
    usage.invocation(resolved, request)
    set_caller(None, ())

    assert [call["user_id"] for call in store.calls] == ["customer-1", "apikey:abc"]
