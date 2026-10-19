"""Embedded calls retain terminal failures without relying on an HTTP stream."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from forge_agent_runtime.executor import AgentExecutor, FileAgentSource, RunRequest
from forge_common.adk.turns import TURN_KEY, current_turn
from tests.conftest import text, without

AGENT = "ca_1k2cuqsuev"


@pytest.fixture
async def embedded(tmp_path, example, models, web):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(without(example, "help", "memory", "orders", "billing", "billing_api")))
    executor = AgentExecutor(FileAgentSource(path), models=models, http=web())
    await executor.sessions.create_session(app_name=AGENT, user_id="ada", session_id="embedded")
    try:
        yield executor
    finally:
        await executor.close()


def request(words="Hello"):
    return RunRequest(
        app_name=AGENT,
        user_id="ada",
        session_id="embedded",
        new_message=types.Content(role="user", parts=[text(words)]),
    )


async def history(executor):
    session = await executor.sessions.get_session(app_name=AGENT, user_id="ada", session_id="embedded")
    return session.events


def markers(events):
    return [event for event in events if TURN_KEY in (event.custom_metadata or {})]


async def test_embedded_post_run_failure_remains_in_history_and_a_later_success_stays_successful(
    embedded, llm
):
    class ExitFailure:
        def plugins(self, agent):
            return ()

        @asynccontextmanager
        async def invocation(self, agent, run):
            yield
            raise RuntimeError("private storage detail")

    embedded.observer = ExitFailure()
    llm.turns = [[text("Answer before cleanup")], [text("Answer after recovery")]]

    with pytest.raises(RuntimeError, match="private storage detail"):
        _ = [event async for event in embedded.run(request())]

    kept = await history(embedded)
    [marker] = markers(kept)
    outcome = marker.custom_metadata[TURN_KEY]
    assert outcome["status"] == "failed"
    assert outcome["errorMessage"] == "The agent ran into a problem. Try again."
    assert "private storage detail" not in marker.model_dump_json()
    answer = next(event for event in kept if event.content and event.content.role == "model")
    assert marker.invocation_id == answer.invocation_id
    assert current_turn() is None

    embedded.observer = None
    _ = [event async for event in embedded.run(request("Try again"))]

    recovered = await history(embedded)
    assert len(markers(recovered)) == 1
    assert recovered[-1].content.parts[0].text == "Answer after recovery"
    assert recovered[-1].invocation_id != marker.invocation_id


async def test_closing_an_embedded_iterator_keeps_a_cancellation_on_its_own_reply(embedded, llm):
    llm.turns = [[text("Reply before caller stopped")]]
    events = embedded.run(request())
    answer = await anext(events)

    await events.aclose()

    [marker] = markers(await history(embedded))
    assert marker.custom_metadata[TURN_KEY]["status"] == "cancelled"
    assert marker.invocation_id == answer.invocation_id
    assert current_turn() is None


async def test_cancelling_before_model_output_does_not_mark_an_older_turn(embedded, llm, monkeypatch):
    llm.turns = [[text("Earlier answer")]]
    first = [event async for event in embedded.run(request("First"))]
    entered = asyncio.Event()

    async def stalled(self, model_request, stream=False):
        entered.set()
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(type(llm), "generate_content_async", stalled)

    async def consume():
        return [event async for event in embedded.run(request("Second"))]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    kept = await history(embedded)
    [marker] = markers(kept)
    assert marker.custom_metadata[TURN_KEY]["status"] == "cancelled"
    current_user = next(
        event
        for event in kept
        if event.author == "user" and event.content and event.content.parts[0].text == "Second"
    )
    assert marker.invocation_id == current_user.invocation_id
    assert marker.invocation_id != first[-1].invocation_id
    assert kept[-1] is marker
    assert current_turn() is None


@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_terminal_outcomes_have_one_marker_and_keep_the_original_event(
    embedded, llm, monkeypatch, interrupted
):
    async def generate(self, model_request, stream=False):
        yield LlmResponse(
            content=types.Content(role="model", parts=[text("Incomplete native reply")]),
            interrupted=interrupted,
            error_code=None if interrupted else "PROVIDER_UNAVAILABLE",
            error_message=None if interrupted else "The provider stopped responding. Please retry.",
        )

    monkeypatch.setattr(type(llm), "generate_content_async", generate)
    _ = [event async for event in embedded.run(request())]

    kept = await history(embedded)
    [marker] = markers(kept)
    native = next(event for event in kept if event.content and event.content.role == "model")
    assert native.content.parts[0].text == "Incomplete native reply"
    assert marker.invocation_id == native.invocation_id
    outcome = marker.custom_metadata[TURN_KEY]
    assert outcome["status"] == ("cancelled" if interrupted else "failed")
    if not interrupted:
        assert native.error_code == outcome["errorCode"] == "PROVIDER_UNAVAILABLE"
        assert native.error_message == outcome["errorMessage"]
