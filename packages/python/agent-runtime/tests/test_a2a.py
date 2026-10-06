import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from forge_agent_runtime.a2a import api_key_security, create_a2a_router, owner_of, task_store
from forge_agent_runtime.executor import AgentExecutor, DirectoryAgentSource
from tests.conftest import call, text, without

AGENT = "ca_1k2cuqsuev"
STATE = {"type": "object", "properties": {"customer_tier": {"type": "string", "enum": ["free", "pro"]}}}
V1 = {"A2A-Version": "1.0"}


@pytest.fixture
def agents(tmp_path: Path, example) -> Path:
    """The example (without its MCP server, which would connect), and a second agent beside it."""
    doc = without(example, "help")
    entry = doc["nodes"][0]
    entry["config"]["state_schema"] = STATE
    entry["config"]["instruction"] = "Help {{ request.userId }} ({{ state.customer_tier }})."
    (tmp_path / "support.json").write_text(json.dumps(doc))
    other = {**without(example, "help", "memory", "orders", "billing", "billing_api"), "id": "ca_other"}
    (tmp_path / "other.json").write_text(json.dumps(other))
    return tmp_path


@pytest.fixture
def executor(agents, models, web) -> AgentExecutor:
    return AgentExecutor(DirectoryAgentSource(agents), models=models, http=web())


def app_of(executor: AgentExecutor, **options: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(create_a2a_router(executor, path="/a2a", **options))
    return app


@pytest.fixture
def client(executor):
    with TestClient(app_of(executor)) as client:
        yield client


def rpc(client, method: str, params: dict[str, Any], *, app: str = AGENT, headers=None) -> dict[str, Any]:
    response = client.post(
        f"/a2a/{app}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def message_v1(words: str, context: str | None = None, **extra: Any) -> dict[str, Any]:
    message = {"messageId": f"m-{words[:8]}", "role": "ROLE_USER", "parts": [{"text": words}], **extra}
    if context:
        message["contextId"] = context
    return {"message": message}


def test_the_card_says_what_the_agent_is_and_where_both_protocols_are(client):
    card = client.get(f"/a2a/{AGENT}/.well-known/agent-card.json").json()

    assert card["name"] == "Support assistant"
    assert card["capabilities"]["streaming"] is True
    url = f"http://testserver/a2a/{AGENT}"
    versions = {(i["url"], i["protocolBinding"], i["protocolVersion"]) for i in card["supportedInterfaces"]}
    assert versions == {(url, "JSONRPC", "1.0"), (url, "JSONRPC", "0.3")}
    # 0.3 clients read the same card.
    assert card["url"] == url
    assert card["preferredTransport"] == "JSONRPC"
    assert card["skills"][0]["name"] == "Support assistant"
    assert "Billing specialist" in {skill["name"] for skill in card["skills"]}


def test_a_pinned_version_or_the_draft_has_a_card_of_its_own(client):
    assert client.get(f"/a2a/{AGENT}@7/.well-known/agent-card.json").status_code == 404
    assert client.get("/a2a/ca_nobody/.well-known/agent-card.json").status_code == 404
    assert client.post("/a2a/ca_nobody", json={}).status_code == 404


def test_a_1_0_message_runs_a_turn_and_completes_with_the_reply(client, llm):
    llm.turns = [[text("Hello! How can I help?")]]
    got = rpc(client, "SendMessage", message_v1("Hi", "conv-1"), headers=V1)

    task = got["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["contextId"] == "conv-1"
    assert task["artifacts"][0]["parts"][0]["text"] == "Hello! How can I help?"
    # The conversation is the agent's session, with ADK's A2A user for an unknown caller.
    assert llm.requests[0].config.system_instruction.startswith("Help A2A_USER_conv-1 ().")

    fetched = rpc(client, "GetTask", {"id": task["id"]}, headers=V1)["result"]
    assert fetched["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_the_conversation_carries_on_in_its_context(client, executor, llm):
    llm.turns = [[text("Noted.")], [text("You said hi.")]]
    rpc(client, "SendMessage", message_v1("Hi", "conv-2"), headers=V1)
    rpc(client, "SendMessage", message_v1("What did I say?", "conv-2"), headers=V1)

    second = llm.requests[1]
    said = [part.text for content in second.contents for part in content.parts or [] if part.text]
    assert said[:3] == ["Hi", "Noted.", "What did I say?"]
    session = await executor.sessions.get_session(
        app_name=AGENT, user_id="A2A_USER_conv-2", session_id="conv-2"
    )
    assert session is not None


def test_a_0_3_message_is_answered_as_0_3(client, llm):
    llm.turns = [[text("Hello from 0.3.")]]
    got = rpc(
        client,
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "text", "text": "Hi"}],
                "contextId": "conv-3",
            }
        },
    )

    task = got["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0] == {"kind": "text", "text": "Hello from 0.3."}
    assert rpc(client, "tasks/get", {"id": task["id"]})["result"]["status"]["state"] == "completed"


def test_streaming_sends_the_task_as_it_goes(client, llm):
    llm.turns = [[call("look_up_order", order_id="A1")], [text("It shipped.")]]
    with client.stream(
        "POST",
        f"/a2a/{AGENT}",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": message_v1("A1?", "c4")},
        headers=V1,
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]

    results = [event["result"] for event in events]
    assert "task" in results[0]
    states = [r["statusUpdate"]["status"]["state"] for r in results if "statusUpdate" in r]
    assert states[0] == "TASK_STATE_WORKING"
    assert states[-1] == "TASK_STATE_COMPLETED"
    assert any("artifactUpdate" in r for r in results)


def test_state_comes_in_the_message_metadata_and_is_checked(client, llm):
    llm.turns = [[text("Hi, pro.")]]
    rpc(
        client,
        "SendMessage",
        message_v1("Hi", "c5", metadata={"state": {"customer_tier": "pro"}}),
        headers=V1,
    )
    assert llm.requests[0].config.system_instruction.startswith("Help A2A_USER_c5 (pro).")

    refused = rpc(
        client, "SendMessage", message_v1("Hi", "c6", metadata={"state": {"plan": "gold"}}), headers=V1
    )
    task = refused["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "plan" in task["status"]["message"]["parts"][0]["text"]


def test_a_context_id_a_conversation_cant_have_is_refused_before_a_task_is_kept(client, llm):
    got = rpc(client, "SendMessage", message_v1("Hi", "x" * 40), headers=V1)
    assert got["error"]["code"] == -32602 and "36 at most" in got["error"]["message"]
    old = rpc(
        client,
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m",
                "role": "user",
                "parts": [{"kind": "text", "text": "Hi"}],
                "contextId": "a b",
            }
        },
    )
    # a2a-sdk's 0.3 adapter answers A2A's own errors as internal ones, saying why.
    assert "36 at most" in old["error"]["message"]
    assert llm.requests == []


def test_a_model_failure_fails_the_task_saying_why(client, llm):
    llm.turns = [ValueError("The prompt is too long.")]
    task = rpc(client, "SendMessage", message_v1("Hi", "c7"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"] == "The prompt is too long."


def test_a_task_is_found_only_through_its_own_agent(client, llm):
    llm.turns = [[text("Hi.")]]
    task = rpc(client, "SendMessage", message_v1("Hi", "c8"), headers=V1)["result"]["task"]

    other = rpc(client, "GetTask", {"id": task["id"]}, app="ca_other", headers=V1)
    assert other["error"]["code"] == -32001  # TaskNotFound


def test_listing_tasks_needs_a_known_caller(executor, llm):
    llm.turns = [[text("Hi.")], [text("Hi.")]]

    def who(request: Request) -> str | None:
        return request.headers.get("x-user")

    with TestClient(app_of(executor, caller=who)) as client:
        assert "error" in rpc(client, "ListTasks", {}, headers=V1)
        rpc(client, "SendMessage", message_v1("Hi", "c9"), headers={**V1, "x-user": "ada"})
        rpc(client, "SendMessage", message_v1("Hi", "c10"), headers={**V1, "x-user": "bob"})
        listed = rpc(client, "ListTasks", {}, headers={**V1, "x-user": "ada"})["result"]["tasks"]

    assert [task["contextId"] for task in listed] == ["c9"]
    # A known caller's conversations are theirs.
    assert llm.requests[0].config.system_instruction.startswith("Help ada ().")


def test_one_agent_is_served_at_a_path_of_its_own_and_its_card_at_the_root(executor, llm):
    async def key(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer k1":
            from fastapi import HTTPException

            raise HTTPException(401)

    app = FastAPI()
    app.include_router(
        create_a2a_router(
            executor,
            path="/a2a",
            agent=AGENT,
            dependencies=[Depends(key)],
            card_dependencies=(),
            security=api_key_security(),
        )
    )
    llm.turns = [[text("Hi.")]]
    with TestClient(app) as client:
        root = client.get("/.well-known/agent-card.json").json()
        assert root == client.get("/a2a/.well-known/agent-card.json").json()
        assert root["supportedInterfaces"][0]["url"] == "http://testserver/a2a"
        assert set(root["securitySchemes"]) == {"bearer", "apiKey"}
        sent = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": message_v1("Hi", "c11")}
        assert client.post("/a2a", json=sent, headers=V1).status_code == 401
        answered = client.post("/a2a", json=sent, headers={**V1, "authorization": "Bearer k1"}).json()
    assert answered["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_tasks_are_kept_in_a_database(tmp_path, executor, llm):
    store = task_store(f"sqlite:///{tmp_path / 'tasks.db'}")
    llm.turns = [[text("Kept.")]]
    with TestClient(app_of(executor, tasks=store)) as client:
        task = rpc(client, "SendMessage", message_v1("Hi", "c12"), headers=V1)["result"]["task"]
        assert rpc(client, "GetTask", {"id": task["id"]}, headers=V1)["result"]["artifacts"]
    await store.engine.dispose()  # type: ignore[attr-defined]


def test_an_owner_is_the_agent_and_the_caller():
    from a2a.server.context import ServerCallContext

    assert owner_of(ServerCallContext(state={"forge_agent": "ca_x"})) == "ca_x/"


def test_authorize_decides_cards_and_calls(executor):
    from fastapi import HTTPException

    seen: list[tuple[str, str]] = []

    async def authorize(request, agent, *, action, user_id):
        seen.append((agent.document.id, action))
        if request.headers.get("x-who") != "friend":
            raise HTTPException(403, "Not you")

    with TestClient(app_of(executor, authorize=authorize)) as client:
        assert client.get(f"/a2a/{AGENT}/.well-known/agent-card.json").status_code == 403
        card = client.get(f"/a2a/{AGENT}/.well-known/agent-card.json", headers={"x-who": "friend"})
        assert card.status_code == 200
        refused = client.post(
            f"/a2a/{AGENT}",
            json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}},
            headers=V1,
        )
        assert refused.status_code == 403
    assert seen == [(AGENT, "card"), (AGENT, "card"), (AGENT, "a2a")]


def test_api_key_security_can_say_what_to_send():
    schemes = api_key_security(bearer="An org key", header="The org key")
    assert schemes["bearer"].http_auth_security_scheme.description == "An org key"
    assert schemes["apiKey"].api_key_security_scheme.description == "The org key"
