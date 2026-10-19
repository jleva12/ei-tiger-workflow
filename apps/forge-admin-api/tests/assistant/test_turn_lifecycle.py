"""The same terminal outcome reaches the HTTP client and conversation audit."""

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi.responses import StreamingResponse
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.executor import AgentExecutor, DocumentAgentSource, RunRequest
from forge_agent_runtime.server import KEEPALIVE
from forge_agent_runtime.server import _stream as agent_stream
from forge_common.adk.models import ProviderModels
from forge_common.adk.turns import TURN_KEY
from forge_common.adk.usage import Attribution
from forge_common.model_provider import parse_model_provider_yaml
from forge_task_adk_workflows.usage_store import UsageStore, invocations, metadata
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from sqlalchemy.ext.asyncio import create_async_engine

from forge_admin.api.routes.assistant import RunRequest as AssistantRequest
from forge_admin.api.routes.assistant import _refuse as assistant_refuse
from forge_admin.api.routes.assistant import _stream as assistant_stream
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.overview.recording import AgentUsage

pytestmark = pytest.mark.asyncio
SUPPORT_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "packages/python/agent-runtime/tests/fixtures/support-assistant.chat-agent.json"
)


class FailedModel(BaseLlm):
    model: str = "test-model"
    raises_after_error: bool = False

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(error_code="SAFETY", error_message="The reply was blocked.")
        if self.raises_after_error:
            raise RuntimeError("Cleanup failed after native error")


@pytest_asyncio.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield UsageStore(engine)
    await engine.dispose()


@pytest.fixture
def document():
    raw = json.loads(SUPPORT_FIXTURE.read_text())
    raw["nodes"] = [raw["nodes"][0]]
    raw["edges"] = []
    raw["organizationId"] = "org-1"
    raw["nodes"][0]["config"]["instruction"] = "Help the person."
    return parse_document(raw)


def outcomes(chunks):
    events = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: ")]
    return [
        event["customMetadata"][TURN_KEY]
        for event in events
        if TURN_KEY in event.get("customMetadata", {})
    ]


@pytest.mark.parametrize("route", ["assistant", "agent"])
@pytest.mark.parametrize("raises_after_error", [False, True])
async def test_native_error_terminal_matches_audit_and_is_persisted(
    store, route, document, raises_after_error
):
    model = FailedModel(raises_after_error=raises_after_error)
    sessions = InMemorySessionService()
    message = types.Content(role="user", parts=[types.Part(text="Hello")])
    if route == "assistant":
        app_name = "forge"
        runner = Runner(
            app=App(name=app_name, root_agent=LlmAgent(name="forge", model=model)),
            session_service=sessions,
        )
        await sessions.create_session(app_name=app_name, user_id="u", session_id="s")
        stream = assistant_stream(
            runner,
            AssistantRequest(
                app_name=app_name, user_id="u", session_id="s", new_message=message
            ),
            None,
            usage=store,
            attribution=Attribution("org-1", "assistant", "assistant", "Assistant"),
        )
        close = runner.close
    else:
        config = parse_model_provider_yaml("""
version: 1
default: {provider: openai, model: test-model}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: test}
    models: [{id: test-model}]
""")
        app_name = document.id
        executor = AgentExecutor(
            DocumentAgentSource(document),
            models=ProviderModels(config, build=lambda _: model),
            sessions=sessions,
            observer=AgentUsage(store),
        )
        await sessions.create_session(app_name=app_name, user_id="u", session_id="s")
        body = RunRequest(
            app_name=app_name, user_id="u", session_id="s", new_message=message
        )
        # The conversation's lease, as the run API takes it before it streams.
        stream = agent_stream(
            executor, body, app_name, lease=await executor.take_turn(body)
        )
        close = executor.close
    try:
        chunks = [chunk async for chunk in stream]
        started, terminal = outcomes(chunks)
        assert (started["status"], terminal["status"]) == ("running", "failed")
        assert terminal["runId"] == started["runId"]
        assert terminal["errorCode"] == "SAFETY"
        async with store.engine.begin() as connection:
            row = (await connection.execute(sa.select(invocations))).mappings().one()
        assert row["id"] == terminal["runId"]
        assert row["status"] == terminal["status"]
        assert row["finished_at"] is not None
        assert "SAFETY" in row["error"]
        session = await sessions.get_session(
            app_name=app_name, user_id="u", session_id="s"
        )
        assert any(event.error_code == "SAFETY" for event in session.events)
        [marker] = [
            event
            for event in session.events
            if TURN_KEY in (event.custom_metadata or {})
        ]
        assert marker.custom_metadata[TURN_KEY] == terminal
    finally:
        await close()


async def test_raw_assistant_heartbeats_and_disconnect_finishes_audit(store):
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="forge", user_id="u", session_id="s")

    class QuietModel(BaseLlm):
        model: str = "quiet"

        async def generate_content_async(self, llm_request, stream=False):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                await asyncio.sleep(0)
                cleaned.set()

    runtime = AgentRuntime.of(
        [App(name="forge", root_agent=LlmAgent(name="forge", model=QuietModel()))],
        sessions=sessions,
        artifacts=InMemoryArtifactService(),
    )
    runner = runtime.runners["forge"]

    chunks = []
    disconnect = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message["body"].decode())
            if chunks[-1] == KEEPALIVE:
                disconnect.set()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    body = AssistantRequest(
        app_name="forge",
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="Hello")]),
    )
    response = StreamingResponse(
        assistant_stream(
            runner,
            body,
            None,
            usage=store,
            attribution=Attribution("org-1", "assistant", "assistant", "Assistant"),
            keepalive=0.01,
        )
    )
    await asyncio.wait_for(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 2
    )
    assert entered.is_set() and cleaned.is_set()
    assert KEEPALIVE in chunks
    [started] = outcomes(
        chunks
    )  # A disconnected client cannot receive a terminal frame.
    async with store.engine.begin() as connection:
        row = (await connection.execute(sa.select(invocations))).mappings().one()
    assert row["id"] == started["runId"]
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    saved = await sessions.get_session(app_name="forge", user_id="u", session_id="s")
    user, terminal = saved.events
    assert terminal.custom_metadata[TURN_KEY]["status"] == "cancelled"
    assert terminal.custom_metadata[TURN_KEY]["runId"] == started["runId"]
    assert terminal.invocation_id == user.invocation_id
    assert terminal.invocation_id != started["runId"]
    await runner.close()


@pytest.mark.parametrize("route", ["assistant", "agent"])
@pytest.mark.parametrize("failure", ["after_run", "serialization"])
async def test_post_run_and_transport_failures_are_saved_before_terminal(
    store, document, route, failure, monkeypatch
):
    import forge_agent_runtime.server as server

    class FinalModel(BaseLlm):
        model: str = "test-model"

        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="Saved answer")]
                )
            )

    class FailingCleanup(BasePlugin):
        def __init__(self):
            super().__init__(name="failing_cleanup")

        async def after_run_callback(self, *, invocation_context):
            raise RuntimeError("Post-run cleanup failed")

    class ObservedUsage(AgentUsage):
        def plugins(self, agent):
            return [*super().plugins(agent), *plugins]

    plugins = [FailingCleanup()] if failure == "after_run" else []
    sessions = InMemorySessionService()
    message = types.Content(role="user", parts=[types.Part(text="Hello")])
    model = FinalModel()
    if route == "assistant":
        app_name = "forge"
        runner = Runner(
            app=App(
                name=app_name,
                root_agent=LlmAgent(name="forge", model=model),
                plugins=plugins,
            ),
            session_service=sessions,
        )
        stream = assistant_stream(
            runner,
            AssistantRequest(
                app_name=app_name, user_id="u", session_id="s", new_message=message
            ),
            None,
            usage=store,
            attribution=Attribution("org-1", "assistant", "assistant", "Assistant"),
        )
        close = runner.close
    else:
        app_name = document.id
        executor = AgentExecutor(
            DocumentAgentSource(document),
            models=ProviderModels(
                parse_model_provider_yaml("""
version: 1
default: {provider: openai, model: test-model}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: test}
    models: [{id: test-model}]
"""),
                build=lambda _: model,
            ),
            sessions=sessions,
            observer=ObservedUsage(store),
        )
        body = RunRequest(
            app_name=app_name, user_id="u", session_id="s", new_message=message
        )
        # The conversation's lease, as the run API takes it before it streams.
        stream = agent_stream(
            executor, body, app_name, lease=await executor.take_turn(body)
        )
        close = executor.close
    await sessions.create_session(app_name=app_name, user_id="u", session_id="s")
    original_sse = server.sse

    def serialize(event):
        if (
            failure == "serialization"
            and event.content
            and any(part.text == "Saved answer" for part in event.content.parts or ())
        ):
            raise RuntimeError("Transport encoding failed")
        return original_sse(event)

    monkeypatch.setattr(server, "sse", serialize)
    try:
        chunks = [chunk async for chunk in stream]
        terminal = outcomes(chunks)[-1]
        saved = await sessions.get_session(
            app_name=app_name, user_id="u", session_id="s"
        )
        [marker] = [
            event for event in saved.events if TURN_KEY in (event.custom_metadata or {})
        ]
        assert marker.custom_metadata[TURN_KEY] == terminal
        assert terminal["status"] == "failed"
        assert marker.invocation_id == saved.events[0].invocation_id
        assert marker.content is None
        assert any(
            part.text == "Saved answer"
            for event in saved.events
            if event.content
            for part in event.content.parts or ()
        )
        async with store.engine.begin() as connection:
            row = (await connection.execute(sa.select(invocations))).mappings().one()
        assert row["id"] == terminal["runId"]
        assert row["status"] == "failed"
    finally:
        await close()


async def test_unconfigured_assistant_saves_refused_turn_and_failed_audit(store):
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(name="forge", root_agent=LlmAgent(name="forge", model=FailedModel())),
        session_service=sessions,
    )
    await sessions.create_session(app_name="forge", user_id="u", session_id="s")
    try:
        chunks = [
            chunk
            async for chunk in assistant_refuse(
                runner,
                AssistantRequest(
                    app_name="forge",
                    user_id="u",
                    session_id="s",
                    new_message=types.Content(
                        role="user", parts=[types.Part(text="Hello")]
                    ),
                ),
                "Configure the model key.",
                usage=store,
                attribution=Attribution("org-1", "assistant", "assistant", "Assistant"),
            )
        ]
        terminal = outcomes(chunks)[-1]
        assert terminal["status"] == "failed"
        assert terminal["errorCode"] == "NOT_CONFIGURED"
        saved = await sessions.get_session(
            app_name="forge", user_id="u", session_id="s"
        )
        user, marker = saved.events
        assert user.author == "user" and user.content.parts[0].text == "Hello"
        assert marker.invocation_id == user.invocation_id
        assert marker.custom_metadata[TURN_KEY] == terminal
        async with store.engine.begin() as connection:
            row = (await connection.execute(sa.select(invocations))).mappings().one()
        assert row["id"] == terminal["runId"]
        assert row["status"] == "failed"
        assert "NOT_CONFIGURED" in row["error"]
    finally:
        await runner.close()
