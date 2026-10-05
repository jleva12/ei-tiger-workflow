import contextlib
import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types

from forge_agent_runtime.cli import main as cli
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    DirectoryAgentSource,
    FileAgentSource,
    RunRequest,
)
from forge_agent_runtime.server import create_app
from forge_agent_runtime.services import RuntimeServices
from tests.conftest import EXAMPLE, call, text, without

AGENT = "ca_1k2cuqsuev"
STATE = {
    "type": "object",
    "properties": {"customer_tier": {"type": "string", "enum": ["free", "pro"]}},
    "required": ["customer_tier"],
}


@pytest.fixture
def agent_file(tmp_path: Path, example) -> Path:
    # The Help center (MCP) would connect to a real server: left out.
    doc = without(example, "help")
    entry = doc["nodes"][0]
    entry["config"]["state_schema"] = STATE
    entry["config"]["instruction"] = "Help {{ request.userId }}, a {{ state.customer_tier }} customer."
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def orders() -> list[httpx.Request]:
    return []


@pytest.fixture
def client(agent_file, models, web, orders):
    def handler(request: httpx.Request) -> httpx.Response:
        orders.append(request)
        return httpx.Response(200, json={"order": "A1", "status": "shipped"})

    executor = AgentExecutor(
        FileAgentSource(agent_file),
        models=models,
        services=RuntimeServices(allowed_hosts=("api.example.com",)),
        http=web(handler),
    )
    with TestClient(create_app(executor)) as client:
        yield client


def events(response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        json.loads(m.removeprefix("data: ")) for m in response.text.split("\n\n") if m.startswith("data: ")
    ]


def session(client, user="ada", **state) -> str:
    created = client.post(f"/apps/{AGENT}/users/{user}/sessions", json={"state": state} if state else {})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def run(client, session_id, message: str, user="ada", app=AGENT, **extra):
    return client.post(
        "/run_sse",
        json={
            "appName": app,
            "userId": user,
            "sessionId": session_id,
            "newMessage": {"role": "user", "parts": [{"text": message}]},
            "streaming": True,
            **extra,
        },
    )


def test_a_turn_runs_with_its_instruction_filled_in_and_its_tools(client, llm, calls, orders):
    llm.turns = [[call("look_up_order", order_id="A1")], [text("Your order A1 has shipped.")]]
    sid = session(client)
    got = events(
        run(client, sid, "Where's A1?", stateDelta={"customer_tier": "pro", "model": "openai/gpt-other"})
    )

    assert got[-1]["content"]["parts"][0]["text"] == "Your order A1 has shipped."
    assert any(e.get("partial") for e in got)
    assert str(orders[0].url) == "https://api.example.com/orders/A1"
    first = llm.requests[0]
    assert first.config.system_instruction.startswith("Help ada, a pro customer.")
    assert calls[0].model.ref == "openai/gpt-other"
    tool_answer = next(
        p["functionResponse"]["response"]
        for e in got
        for p in e["content"]["parts"]
        if "functionResponse" in p
    )
    assert tool_answer["status"] == "success"
    assert tool_answer["payload"]["body"] == {"order": "A1", "status": "shipped"}

    kept = client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").json()
    assert kept["state"]["customer_tier"] == "pro"
    assert not any(key.startswith("temp:") for key in kept["state"])
    assert [s["id"] for s in client.get(f"/apps/{AGENT}/users/ada/sessions").json()] == [sid]


def test_state_the_agent_doesnt_declare_is_refused(client):
    sid = session(client)
    refused = run(client, sid, "hi", stateDelta={"plan": "pro"})
    assert refused.status_code == 422 and "plan" in refused.text
    wrong = run(client, sid, "hi", stateDelta={"customer_tier": "gold"})
    assert wrong.status_code == 422 and "gold" in wrong.text
    assert client.post(f"/apps/{AGENT}/users/ada/sessions", json={"state": {"app:x": 1}}).status_code == 422


def test_unknown_agents_and_conversations_are_404s(client):
    assert run(client, "nope", "hi").status_code == 404
    assert run(client, "nope", "hi", app="ca_other").status_code == 404
    assert client.get("/apps/ca_other").status_code == 404
    sid = session(client)
    client.delete(f"/apps/{AGENT}/users/ada/sessions/{sid}")
    assert client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").status_code == 404


def test_the_agent_says_what_it_takes(client):
    got = client.get(f"/apps/{AGENT}").json()
    assert got["entry"] == "support_assistant"
    assert got["state_schema"]["properties"] == STATE["properties"]


def test_a_failure_after_the_stream_starts_is_an_event(client, llm):
    llm.turns = [RuntimeError("boom")]
    got = events(run(client, session(client), "hi", stateDelta={"customer_tier": "free"}))
    assert got[-1]["errorCode"] == "INTERNAL_ERROR"


async def test_versions_are_pinned_and_built_agents_reused(tmp_path, example, models, web):
    for version in (1, 2):
        doc = copy.deepcopy(without(example, "help"))
        doc["version"] = version
        doc["nodes"][0]["config"]["instruction"] = f"Version {version}."
        (tmp_path / f"v{version}.json").write_text(json.dumps(doc))
    executor = AgentExecutor(DirectoryAgentSource(tmp_path), models=models, http=web())
    latest, resolved = await executor.runner(AGENT)
    assert resolved.version == 2
    again, _ = await executor.runner(AGENT)
    assert again is latest
    _, pinned = await executor.runner(f"{AGENT}@1")
    assert pinned.version == 1
    with pytest.raises(AgentNotFound):
        await executor.runner(f"{AGENT}@3")
    assert AgentRef.parse("ca_x@draft") == AgentRef("ca_x", "draft")
    with pytest.raises(AgentNotFound):
        AgentRef.parse("ca_x@latest")
    await executor.close()


class Watching:
    """An observer recording which agents were built and how each turn ended."""

    def __init__(self) -> None:
        self.built: list[Any] = []
        self.turns: list[tuple[str, str]] = []

    def plugins(self, agent: Any) -> tuple[()]:
        self.built.append(agent.version)
        return ()

    @contextlib.asynccontextmanager
    async def invocation(self, agent: Any, request: RunRequest) -> AsyncIterator[None]:
        try:
            yield
        except Exception as error:
            self.turns.append(("failed", str(error)))
            raise
        self.turns.append(("succeeded", request.session_id))


async def test_an_observer_sees_each_agent_built_and_each_turn(tmp_path, example, models, web, llm):
    (tmp_path / "agent.json").write_text(json.dumps(without(example, "help")))
    watching = Watching()
    executor = AgentExecutor(DirectoryAgentSource(tmp_path), models=models, http=web(), observer=watching)
    await executor.sessions.create_session(app_name=AGENT, user_id="ada", session_id="s-1")
    request = RunRequest(
        app_name=AGENT,
        user_id="ada",
        session_id="s-1",
        new_message=types.Content(role="user", parts=[text("hi")]),
    )
    llm.turns = [[text("Hello")], RuntimeError("boom")]

    _ = [event async for event in executor.run(request)]
    with pytest.raises(RuntimeError):
        _ = [event async for event in executor.run(request)]

    # Built once, then reused; each turn seen ending.
    assert len(watching.built) == 1
    assert watching.turns == [("succeeded", "s-1"), ("failed", "boom")]
    await executor.close()


def test_the_cli_validates_an_export(capsys, tmp_path, example):
    assert cli(["validate", str(EXAMPLE)]) == 0
    assert "Support assistant" in capsys.readouterr().out
    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps(
            {
                **example,
                "edges": [{"id": "x", "source": "agent", "source_output": "agents", "target": "memory"}],
            }
        )
    )
    assert cli(["validate", str(broken)]) == 1
    assert "isn't an agent" in capsys.readouterr().out
