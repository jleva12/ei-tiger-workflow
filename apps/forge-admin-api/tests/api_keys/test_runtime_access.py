"""Who may call the runtime's agents: anyone when it's public (credentials
sent still say who); otherwise an API key or a sign-in holding agents:run in
the agent's organization, into conversations of their own."""

from contextlib import nullcontext
from typing import Any

import pytest
from forge_agent_runtime import RunRequest
from key_world import (
    ADMIN,
    AGENT,
    MEMBER,
    NEIGHBOUR,
    OTHER_AGENT,
    RUNTIME,
    VIEWER,
    World,
)

from forge_admin.auth.access import set_caller
from forge_admin.overview.recording import AgentUsage

CARD = f"/a2a/{AGENT}/.well-known/agent-card.json"


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


# ------------------------------------------------------------ public


def test_a_public_runtime_takes_anyones_calls(world: World) -> None:
    assert runtime(world, "GET", f"/apps/{AGENT}").status_code == 200
    card = runtime(world, "GET", CARD).json()
    assert not card.get("securitySchemes")
    made = runtime(world, "POST", f"/apps/{AGENT}/users/customer-1/sessions", body={})
    assert made.status_code == 201


def test_credentials_sent_to_a_public_runtime_say_whos_calling(world: World) -> None:
    secret = world.make_key(name="CI")["secret"]
    # Anonymous callers can't list tasks: they'd see everyone's.
    assert "error" in list_tasks(world, user=None)
    assert "result" in list_tasks(world, user=None, key=secret)
    assert "result" in list_tasks(world, user=MEMBER)
    # A credential that isn't one is refused, not ignored.
    assert (
        runtime(world, "GET", f"/apps/{AGENT}", key="fk_" + "x" * 43).status_code == 401
    )


# ------------------------------------------------------------ private


@pytest.fixture
def private(make_world: Any) -> World:
    world: World = make_world(public=False)
    return world


def test_a_private_runtime_needs_a_caller(private: World) -> None:
    refused = runtime(private, "GET", f"/apps/{AGENT}")
    assert refused.status_code == 401
    assert refused.headers["WWW-Authenticate"] == "Bearer"
    assert runtime(private, "GET", CARD).status_code == 401
    # Refused before the agent is built (its model would fail to).
    body = {
        "appName": AGENT,
        "userId": "x",
        "sessionId": "s",
        "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
    }
    assert runtime(private, "POST", "/run_sse", body=body).status_code == 401


def test_people_need_agents_run_in_the_agents_organization(private: World) -> None:
    assert runtime(private, "GET", f"/apps/{AGENT}", MEMBER).status_code == 200
    assert runtime(private, "GET", f"/apps/{AGENT}", ADMIN).status_code == 200
    assert runtime(private, "GET", f"/apps/{AGENT}", VIEWER).status_code == 403
    assert runtime(private, "GET", f"/apps/{AGENT}", NEIGHBOUR).status_code == 403
    assert runtime(private, "GET", f"/apps/{OTHER_AGENT}", NEIGHBOUR).status_code == 200


def test_keys_call_only_their_organizations_agents_as_their_role_allows(
    private: World,
) -> None:
    caller = private.make_key(name="Caller")["secret"]
    viewer = private.make_key(name="Viewer", role="org:viewer")["secret"]
    assert runtime(private, "GET", f"/apps/{AGENT}", key=caller).status_code == 200
    assert (
        runtime(
            private, "GET", f"/apps/{AGENT}", key=caller, header="x-api-key"
        ).status_code
        == 200
    )
    assert runtime(private, "GET", f"/apps/{AGENT}", key=viewer).status_code == 403
    other = runtime(private, "GET", f"/apps/{OTHER_AGENT}", key=caller)
    assert other.status_code == 403
    assert "another organization" in other.json()["detail"]
    card = runtime(private, "GET", CARD, key=caller).json()
    assert set(card["securitySchemes"]) == {"bearer", "apiKey"}
    assert "result" in list_tasks(private, key=caller)


def test_conversations_are_the_callers_own(private: World) -> None:
    sessions = f"/apps/{AGENT}/users/{{}}/sessions"
    assert (
        runtime(private, "POST", sessions.format(MEMBER), MEMBER, {}).status_code == 201
    )
    assert runtime(private, "GET", sessions.format(ADMIN), MEMBER).status_code == 403
    secret = private.make_key(name="Website")["secret"]
    # A key's end users are anyone but Forge's users and other keys.
    assert (
        runtime(
            private, "POST", sessions.format("customer-42"), body={}, key=secret
        ).status_code
        == 201
    )
    assert (
        runtime(private, "GET", sessions.format(MEMBER), key=secret).status_code == 403
    )
    assert (
        runtime(
            private, "GET", sessions.format("apikey:someone"), key=secret
        ).status_code
        == 403
    )


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
