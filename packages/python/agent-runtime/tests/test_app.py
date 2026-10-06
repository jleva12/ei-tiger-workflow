import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.testclient import TestClient

from forge_agent_runtime import AgentServer, AgentServerSettings, BuildError
from forge_agent_runtime.document import parse_document
from tests.conftest import PLAIN, text, without

AGENT = "ca_1k2cuqsuev"


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    (web / "assets" / "app.js").write_text("console.log('app')")
    return web


def settings(**changes: Any) -> AgentServerSettings:
    return AgentServerSettings(_env_file=None, **changes)  # type: ignore[call-arg]


def session(client: TestClient, user: str = "ada") -> str:
    made = client.post(f"/api/apps/{AGENT}/users/{user}/sessions", json={})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def test_one_builder_chain_makes_the_whole_app(agent_file, web_dir, models, llm):
    server = AgentServer(settings()).with_agent(agent_file).with_models(models).with_web(web_dir)
    llm.turns = [[text("Hello Ada.")]]
    with TestClient(server.build()) as client:
        agent = client.get("/api/agent").json()
        assert (agent["id"], agent["name"], agent["entry"]) == (
            AGENT,
            "Support assistant",
            "support_assistant",
        )

        reply = client.post(
            "/api/run_sse",
            json={
                "appName": AGENT,
                "userId": "ada",
                "sessionId": session(client),
                "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
        assert reply.status_code == 200
        assert "Hello Ada." in reply.text
        assert llm.requests[0].config.system_instruction.startswith("Help ada.")

        assert client.get("/healthz").json() == {"status": "ok"}
        assert "root" in client.get("/").text
        assert "root" in client.get("/conversations/42").text  # the UI's own route
        assert client.get("/assets/app.js").text == "console.log('app')"
        assert client.get("/assets/missing.js").status_code == 404
    assert server.build() is server.build()


def test_settings_say_what_methods_dont(agent_file, models):
    server = AgentServer(settings(definition=agent_file, api_prefix="/v1", title="Support")).with_models(
        models
    )
    app = server.build()
    assert app.title == "Support"
    with TestClient(app) as client:
        assert client.get("/v1/agent").json()["id"] == AGENT
        assert client.get("/api/agent").status_code == 404


def test_auth_guards_the_run_api_only(agent_file, web_dir, models):
    def token(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer s3cret":
            raise HTTPException(401, "Sign in")

    server = (
        AgentServer(settings())
        .with_agent(agent_file)
        .with_models(models)
        .with_web(web_dir)
        .with_auth(Depends(token))
    )
    with TestClient(server.build()) as client:
        assert client.get("/api/agent").status_code == 401
        assert client.post(f"/api/apps/{AGENT}/users/ada/sessions", json={}).status_code == 401
        assert client.get("/api/agent", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200


def test_cors_only_when_asked(agent_file, models):
    preflight = {"Origin": "https://shop.example", "Access-Control-Request-Method": "POST"}
    plain = AgentServer(settings()).with_agent(agent_file).with_models(models)
    with TestClient(plain.build()) as client:
        assert "access-control-allow-origin" not in client.options("/api/run_sse", headers=preflight).headers
    allowed = (
        AgentServer(settings()).with_agent(agent_file).with_models(models).with_cors("https://shop.example")
    )
    with TestClient(allowed.build()) as client:
        answer = client.options("/api/run_sse", headers=preflight)
        assert answer.headers["access-control-allow-origin"] == "https://shop.example"


def test_your_own_routes_and_lifespans_come_along(agent_file, models):
    started: list[str] = []

    @asynccontextmanager
    async def warm(_app: Any):
        started.append("up")
        yield
        started.append("down")

    mine = APIRouter()

    @mine.get("/hello")
    async def hello() -> dict[str, str]:
        return {"hello": "world"}

    server = AgentServer(settings()).with_agent(agent_file).with_models(models)
    server.with_routes(mine).with_lifespan(warm)
    with TestClient(server.build()) as client:
        assert client.get("/hello").json() == {"hello": "world"}
        assert started == ["up"]
    assert started == ["up", "down"]
    with pytest.raises(TypeError, match="no telepathy"):
        server.with_services(telepathy=True)


def test_an_agent_given_as_a_document_is_served_too(example, models):
    doc = parse_document(without(example, *PLAIN))
    with TestClient(AgentServer(settings()).with_agent(doc).with_models(models).build()) as client:
        assert client.get("/api/agent").json()["id"] == AGENT


def test_an_agent_that_cant_be_built_stops_the_app_as_it_starts(tmp_path, example, models):
    doc = without(example, "help", "billing", "billing_api", "memory")
    orders = next(n for n in doc["nodes"] if n["id"] == "orders")
    orders["config"]["headers"] = [{"id": "h", "name": "Authorization", "value": "Bearer ${ORDERS_TOKEN}"}]
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    server = AgentServer(settings()).with_agent(path).with_models(models).with_services(environment={})
    with pytest.raises(BuildError, match="ORDERS_TOKEN"), TestClient(server.build()):
        pass


def a2a_send(client: TestClient, words: str, context: str, **headers: str) -> Any:
    return client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": f"m-{context}",
                    "role": "ROLE_USER",
                    "parts": [{"text": words}],
                    "contextId": context,
                }
            },
        },
        headers={"A2A-Version": "1.0", **headers},
    )


def test_a2a_serves_the_agent_beside_the_run_api(tmp_path, agent_file, web_dir, models, llm):
    server = (
        AgentServer(settings())
        .with_agent(agent_file)
        .with_models(models)
        .with_sessions(f"sqlite:///{tmp_path / 'sessions.db'}")
        .with_web(web_dir)
        .with_a2a(public_url="https://agent.example.com")
    )
    llm.turns = [[text("Hello over A2A.")]]
    with TestClient(server.build()) as client:
        card = client.get("/.well-known/agent-card.json").json()
        assert card["name"] == "Support assistant"
        assert card["url"] == "https://agent.example.com/a2a"
        assert "securitySchemes" not in card

        task = a2a_send(client, "hi", "c1").json()["result"]["task"]
        assert task["artifacts"][0]["parts"][0]["text"] == "Hello over A2A."
        # Kept with the conversations: the same session the run API would show.
        listed = client.get(f"/api/apps/{AGENT}/users/A2A_USER_c1/sessions").json()
        assert [s["id"] for s in listed] == ["c1"]
    assert "A2A tasks sqlite+aiosqlite" in server._kept()


def test_a2a_asks_for_the_api_key_but_shows_its_card(agent_file, models, llm):
    server = AgentServer(settings()).with_agent(agent_file).with_models(models).with_api_keys("k1").with_a2a()
    llm.turns = [[text("Hi.")]]
    with TestClient(server.build()) as client:
        card = client.get("/a2a/.well-known/agent-card.json").json()
        assert set(card["securitySchemes"]) == {"bearer", "apiKey"}
        assert a2a_send(client, "hi", "c2").status_code == 401
        sent = a2a_send(client, "hi", "c2", authorization="Bearer k1").json()
        assert sent["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        # Callers with a key are known, so they may list their tasks.
        listed = client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 2, "method": "ListTasks", "params": {}},
            headers={"A2A-Version": "1.0", "X-API-Key": "k1"},
        ).json()
        assert [t["contextId"] for t in listed["result"]["tasks"]] == ["c2"]


def test_a2a_is_off_unless_asked(agent_file, models):
    with TestClient(AgentServer(settings()).with_agent(agent_file).with_models(models).build()) as client:
        assert client.get("/.well-known/agent-card.json").status_code == 404
