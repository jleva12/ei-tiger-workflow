import asyncio
import contextlib
import contextvars
import copy
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types

from forge_agent_runtime.cli import main as cli
from forge_agent_runtime.document import load_document
from forge_agent_runtime.executor import (
    AgentExecutor,
    AgentNotFound,
    AgentRef,
    DirectoryAgentSource,
    FileAgentSource,
    RunRequest,
    StateRefused,
    check_state,
)
from forge_agent_runtime.leases import MemoryTurnLeases, TurnKey, TurnLease
from forge_agent_runtime.server import KEEPALIVE, _stream, create_app, create_router
from forge_agent_runtime.services import RuntimeServices
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from tests.conftest import EXAMPLE, EXAMPLE_HOSTS, PROVIDERS, ScriptedLlm, call, text, without

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


def events(response, *, lifecycle=False) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    got = [
        json.loads(m.removeprefix("data: ")) for m in response.text.split("\n\n") if m.startswith("data: ")
    ]
    return got if lifecycle else [e for e in got if "forge:turn" not in e.get("customMetadata", {})]


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


def test_a_turn_that_asks_for_follow_ups_ends_with_them(client, llm, calls):
    llm.turns = [
        [text("Your order A1 has shipped.")],
        [text('["Track it", "Change the address", "Cancel it"]')],
    ]
    sid = session(client)
    asked = {"forge:suggest_follow_ups": {"count": 2}, "model": "openai/gpt-other"}
    got = events(run(client, sid, "Where's A1?", stateDelta=asked))

    # The stream's last event carries them, written by the reply's model.
    assert got[-1]["actions"]["stateDelta"] == {
        "forge:follow_ups": {"suggestions": ["Track it", "Change the address"]}
    }
    assert [call.model.ref for call in calls] == ["openai/gpt-other", "openai/gpt-other"]
    kept = client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").json()
    assert kept["state"]["forge:follow_ups"]["suggestions"] == ["Track it", "Change the address"]

    # A turn that doesn't ask clears them, and costs no model call.
    llm.turns = [[text("Anything else?")]]
    got = events(run(client, sid, "Thanks"))
    assert len(llm.requests) == 3
    assert client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").json()["state"]["forge:follow_ups"] is None


def test_state_the_agent_doesnt_declare_is_refused(client):
    sid = session(client)
    refused = run(client, sid, "hi", stateDelta={"plan": "pro"})
    assert refused.status_code == 422 and "plan" in refused.text
    wrong = run(client, sid, "hi", stateDelta={"customer_tier": "gold"})
    assert wrong.status_code == 422 and "gold" in wrong.text
    assert client.post(f"/apps/{AGENT}/users/ada/sessions", json={"state": {"app:x": 1}}).status_code == 422


@pytest.mark.timeout(10)
def test_state_a_pattern_cant_check_in_time_is_refused(tmp_path, example, models, web):
    doc = without(example, "help")
    doc["nodes"][0]["config"]["state_schema"] = {
        "type": "object",
        "properties": {"code": {"type": "string", "pattern": "^(a|aa)+$"}},
    }
    agent = load_document(doc)
    runaway = "a" * 60 + "b"  # ^(a|aa)+$ backtracks on it for ages

    began = time.perf_counter()
    with pytest.raises(StateRefused) as refused:
        check_state(agent, {"code": runaway})
    assert time.perf_counter() - began < 2
    assert refused.value.problems == [
        "code: its regular expression /^(a|aa)+$/ took longer than the 500 ms "
        "a value's regular expressions get"
    ]
    assert check_state(agent, {"code": "aaaa"}) == {"code": "aaaa"}

    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    executor = AgentExecutor(
        FileAgentSource(path),
        models=models,
        services=RuntimeServices(allowed_hosts=("api.example.com",)),
        http=web(lambda request: httpx.Response(200)),
    )
    with TestClient(create_app(executor)) as client:
        began = time.perf_counter()
        created = client.post(f"/apps/{AGENT}/users/ada/sessions", json={"state": {"code": runaway}})
        assert created.status_code == 422 and "took longer than" in created.text
        assert time.perf_counter() - began < 2


@pytest.mark.timeout(10)
def test_state_is_refused_for_a_reference_that_cant_be_resolved(tmp_path, example, models, web):
    elsewhere = "http://127.0.0.1:9/code.json"  # never fetched: only references within the schema are read
    doc = without(example, "help")
    doc["nodes"][0]["config"]["state_schema"] = {
        "type": "object",
        "properties": {"code": {"$ref": elsewhere}},
    }
    agent = load_document(doc)

    with pytest.raises(StateRefused) as refused:
        check_state(agent, {"code": "A1"})
    assert refused.value.problems == [
        f"code: its $ref {elsewhere!r} can't be resolved: only references within the schema are read"
    ]

    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    executor = AgentExecutor(
        FileAgentSource(path),
        models=models,
        services=RuntimeServices(allowed_hosts=("api.example.com",)),
        http=web(lambda request: httpx.Response(200)),
    )
    with TestClient(create_app(executor)) as client:
        created = client.post(f"/apps/{AGENT}/users/ada/sessions", json={"state": {"code": "A1"}})
        assert created.status_code == 422 and "can't be resolved" in created.text


def test_state_references_resolve_within_the_state_schema(example):
    doc = without(example, "help")
    doc["nodes"][0]["config"]["state_schema"] = {
        "$id": "https://example.com/state.json",
        "type": "object",
        "$defs": {"tier": {"enum": ["free", "pro"]}, "broken": "not a schema"},
        "definitions": {"code": {"type": "string", "maxLength": 2}},
        "properties": {
            "customer_tier": {"$ref": "#/$defs/tier"},
            "code": {"$ref": "https://example.com/state.json#/definitions/code"},
            "broken": {"$ref": "#/$defs/broken"},
        },
        "required": ["customer_tier", "code"],
    }
    agent = load_document(doc)

    fitting = {"customer_tier": "pro", "code": "A1"}
    assert check_state(agent, fitting) == fitting
    assert check_state(agent, {"customer_tier": "pro"}) == {"customer_tier": "pro"}  # needn't set all
    with pytest.raises(StateRefused) as refused:
        check_state(agent, {"customer_tier": "gold", "code": "A12"})
    assert refused.value.problems == [
        "customer_tier: 'gold' is not one of ['free', 'pro']",
        "code: 'A12' is too long",
    ]
    with pytest.raises(StateRefused) as refused:
        check_state(agent, {"broken": "A1"})
    assert refused.value.problems == [
        "broken: its $ref '#/$defs/broken' can't be resolved: only references within the schema are read"
    ]


def test_any_agent_takes_a_request_for_follow_ups(example):
    agent = load_document(without(example, "help"))
    for asked in (True, False, {"count": 2}, {}):
        assert check_state(agent, {"forge:suggest_follow_ups": asked}) == {"forge:suggest_follow_ups": asked}
    for wrong in ({"count": 9}, "yes"):
        with pytest.raises(StateRefused):
            check_state(agent, {"forge:suggest_follow_ups": wrong})


def test_unknown_agents_and_conversations_are_404s(client):
    assert run(client, "nope", "hi").status_code == 404
    assert run(client, "nope", "hi", app="ca_other").status_code == 404
    assert client.get("/apps/ca_other").status_code == 404
    sid = session(client)
    client.delete(f"/apps/{AGENT}/users/ada/sessions/{sid}")
    assert client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").status_code == 404


@pytest.mark.parametrize("delete_missing", [False, True])
def test_session_deletion_keeps_shared_and_other_sessions_artifacts(agent_file, models, delete_missing):
    executor = AgentExecutor(FileAgentSource(agent_file), models=models)

    async def seed_artifacts():
        for filename, sid in (("user:profile.txt", "first"), ("notes.txt", "first"), ("other.txt", "second")):
            await executor.artifacts.save_artifact(
                app_name=AGENT,
                user_id="ada",
                session_id=sid,
                filename=filename,
                artifact=types.Part(text=filename),
            )

    async def remaining(sid):
        return await executor.artifacts.list_artifact_keys(app_name=AGENT, user_id="ada", session_id=sid)

    with TestClient(create_app(executor)) as client:
        assert client.portal is not None
        client.portal.call(seed_artifacts)
        target = "missing" if delete_missing else "first"
        assert client.delete(f"/apps/{AGENT}/users/ada/sessions/{target}").status_code == 204
        assert client.portal.call(remaining, "second") == ["other.txt", "user:profile.txt"]
        assert client.portal.call(remaining, "first") == (
            ["notes.txt", "user:profile.txt"] if delete_missing else ["user:profile.txt"]
        )


def test_the_agent_says_what_it_takes(client):
    got = client.get(f"/apps/{AGENT}").json()
    assert got["entry"] == "support_assistant"
    assert got["state_schema"]["properties"] == STATE["properties"]


def test_a_failure_after_the_stream_starts_is_an_event(client, llm):
    llm.turns = [RuntimeError("boom")]
    got = events(run(client, session(client), "hi", stateDelta={"customer_tier": "free"}))
    assert got[-1]["errorCode"] == "INTERNAL_ERROR"


class SlowLlm(ScriptedLlm):
    """Thinks for a while before each answer, as a turn waiting on a slow tool would be quiet."""

    delay: float = 0.3

    async def generate_content_async(self, llm_request, stream=False):
        await asyncio.sleep(self.delay)
        async for response in super().generate_content_async(llm_request, stream):
            yield response


def test_a_quiet_turn_keeps_its_stream_alive(agent_file, web):
    llm = SlowLlm(turns=[[text("Here now.")]])
    models = ProviderModels(parse_model_provider_yaml(PROVIDERS), build=lambda _: llm)
    executor = AgentExecutor(
        FileAgentSource(agent_file),
        models=models,
        services=RuntimeServices(allowed_hosts=("api.example.com",)),
        http=web(),
    )
    app = FastAPI()
    app.include_router(create_router(executor, keepalive=0.05))
    with TestClient(app) as client:
        response = run(client, session(client), "hi", stateDelta={"customer_tier": "free"})

    # Said while the model thought, before its answer; skipped as events.
    assert response.text.startswith(":ok\n\n")
    assert KEEPALIVE in response.text
    assert response.text.index(KEEPALIVE) < response.text.index('"Here now."')
    assert events(response)[-1]["content"]["parts"][0]["text"] == "Here now."


step: contextvars.ContextVar[str] = contextvars.ContextVar("step", default="none")


class QuietExecutor:
    """A turn that keeps a context variable from one step to the next, then
    goes quiet until it's stopped."""

    def __init__(self) -> None:
        self.stopped = asyncio.Event()

    async def run(self, body: Any, *, resolved: Any = None, lease: Any = None) -> AsyncIterator[Any]:
        step.set("first")
        yield Event(author="agent", content=types.Content(role="model", parts=[text("one")]))
        yield Event(author="agent", content=types.Content(role="model", parts=[text(step.get())]))
        try:
            await asyncio.sleep(60)
        finally:
            self.stopped.set()


async def test_a_turn_runs_in_one_task_and_stops_with_its_stream() -> None:
    executor = QuietExecutor()
    body: Any = SimpleNamespace(app_name=AGENT, session_id="s1")
    leases = MemoryTurnLeases()
    key = TurnKey(AGENT, "ada", "s1")
    lease = await TurnLease.take(leases, key)
    stream = _stream(executor, body, "agent", lease=lease, keepalive=0.05)  # type: ignore[arg-type]

    said = [await anext(stream) for _ in range(5)]
    assert said[0] == ":ok\n\n"
    # What a step set, the next step reads: the turn's steps share one context.
    assert json.loads(said[1].removeprefix("data: "))["customMetadata"]["forge:turn"]["status"] == "running"
    assert json.loads(said[3].removeprefix("data: "))["content"]["parts"][0]["text"] == "first"
    assert said[4] == KEEPALIVE

    # The person goes away: the turn stops with the stream, and the conversation is free.
    await stream.aclose()
    await asyncio.wait_for(executor.stopped.wait(), 1)
    assert await leases.acquire(key, owner="next", seconds=30)


async def test_versions_are_pinned_and_built_agents_reused(tmp_path, example, models, web):
    for version in (1, 2):
        doc = copy.deepcopy(without(example, "help"))
        doc["version"] = version
        doc["nodes"][0]["config"]["instruction"] = f"Version {version}."
        (tmp_path / f"v{version}.json").write_text(json.dumps(doc))
    executor = AgentExecutor(
        DirectoryAgentSource(tmp_path),
        models=models,
        http=web(),
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
    )
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
    executor = AgentExecutor(
        DirectoryAgentSource(tmp_path),
        models=models,
        http=web(),
        observer=watching,
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
    )
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


def test_authorize_decides_each_call_before_the_agent_is_built(agent_file, models, web):
    from fastapi import FastAPI, HTTPException

    seen: list[tuple[str, str, str | None]] = []
    built: list[str] = []

    class Watching:
        def plugins(self, agent):
            built.append(agent.document.id)
            return ()

        def invocation(self, agent, request):
            return contextlib.nullcontext()

    async def authorize(request, agent, *, action, user_id):
        seen.append((agent.document.id, action, user_id))
        if user_id == "mallory":
            raise HTTPException(403, "Not yours")

    executor = AgentExecutor(
        FileAgentSource(agent_file),
        models=models,
        http=web(),
        observer=Watching(),
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
    )
    app = FastAPI()
    app.include_router(create_router(executor, authorize=authorize))
    with TestClient(app) as client:
        assert client.get(f"/apps/{AGENT}").status_code == 200
        assert client.get(f"/apps/{AGENT}/users/ada/sessions").status_code == 200
        refused = client.post(
            "/run_sse",
            json={
                "appName": AGENT,
                "userId": "mallory",
                "sessionId": "s1",
                "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
        assert refused.status_code == 403
    assert seen == [(AGENT, "agent", None), (AGENT, "sessions", "ada"), (AGENT, "run", "mallory")]
    # Refused before anything was built.
    assert built == []


@pytest.mark.parametrize("decided", [False, True, None])
def test_streaming_decides_whether_a_turns_replies_stream(agent_file, models, web, llm, decided):
    seen: list[str] = []

    async def streaming(request, agent):
        seen.append(agent.document.id)
        return decided

    executor = AgentExecutor(
        FileAgentSource(agent_file),
        models=models,
        http=web(),
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
    )
    app = FastAPI()
    app.include_router(create_router(executor, streaming=streaming))
    with TestClient(app) as client:
        llm.turns = [[text("Your order A1 has shipped.")], [text("Still shipped.")]]
        sid = session(client, customer_tier="pro")
        # Asked to stream, and asked not to: what it decides holds either way.
        for asked in (True, False):
            got = events(run(client, sid, "Where's A1?", streaming=asked))
            assert got[-1]["content"]["parts"][0]["text"] in {
                "Your order A1 has shipped.",
                "Still shipped.",
            }
            streamed = any(e.get("partial") for e in got)
            assert streamed is (asked if decided is None else decided)
    assert seen == [AGENT, AGENT]


def test_a_conversation_follows_the_workflow_runs_its_calls_started(agent_file, models, web):
    from fastapi import FastAPI, HTTPException

    asked: list[dict[str, Any]] = []
    going = {"run_id": "r1", "workflow": "Triage", "status": "running", "steps": []}

    class Workflows:
        async def describe(self, workflow_id, *, organization_id):
            raise LookupError(workflow_id)

        async def run(self, workflow_id, workflow_input, *, organization_id, context, wait=None):
            raise AssertionError("not run here")

        async def intend(self, starts, *, organization_id, context, source):
            raise AssertionError("not run here")

        async def dispatch(self, run_id, *, organization_id, wait):
            raise AssertionError("not run here")

        async def progress(self, **asking):
            asked.append(asking)
            return going if asking["call_id"] == "call_1" else None

    async def authorize(request, agent, *, action, user_id):
        if user_id == "mallory":
            raise HTTPException(403, "Not yours")

    def app_with(services: RuntimeServices) -> FastAPI:
        executor = AgentExecutor(FileAgentSource(agent_file), models=models, http=web(), services=services)
        app = FastAPI()
        app.include_router(create_router(executor, authorize=authorize))
        return app

    path = "/apps/{app}/users/{user}/sessions/s1/workflow-runs/{call}"
    with TestClient(app_with(RuntimeServices(workflows=Workflows()))) as client:
        # Whichever version the chat runs, the run is the agent's.
        got = client.get(path.format(app=f"{AGENT}@1", user="ada", call="call_1"))
        assert got.status_code == 200, got.text
        assert got.json() == going
        assert asked == [
            {
                "agent_id": AGENT,
                "user_id": "ada",
                "session_id": "s1",
                "call_id": "call_1",
                "organization_id": load_document(agent_file).organization_id,
            }
        ]
        # A call that started none, and someone else's conversation.
        assert client.get(path.format(app=AGENT, user="ada", call="call_2")).status_code == 404
        assert client.get(path.format(app=AGENT, user="mallory", call="call_1")).status_code == 403
        assert len(asked) == 2
    # A runtime without workflows has no runs to follow.
    with TestClient(app_with(RuntimeServices())) as client:
        assert client.get(path.format(app=AGENT, user="ada", call="call_1")).status_code == 404


def test_a_conversation_is_named_after_its_first_message_with_that_turn(client, llm):
    llm.turns = [[text("It shipped.")], [text("Also shipped.")]]
    sid = session(client)
    first = "Where's my order A-1042?\nI ordered it Monday."
    events(run(client, sid, first))
    listed = client.get(f"/apps/{AGENT}/users/ada/sessions").json()
    # The chat lists it with its title, from its state.
    assert listed[0]["state"]["forge:title"] == "Where's my order A-1042?"
    # Set with the turn: no event of its own in the conversation.
    kept = client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").json()
    assert all(e.get("content") for e in kept["events"])
    # Later turns keep it.
    events(run(client, sid, "And B-7731?"))
    again = client.get(f"/apps/{AGENT}/users/ada/sessions/{sid}").json()
    assert again["state"]["forge:title"] == "Where's my order A-1042?"
