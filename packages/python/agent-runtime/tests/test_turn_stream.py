"""Invocation completion is explicit, including failure and cancellation."""

import asyncio
import json

import pytest
from google.adk.events import Event
from google.genai import types

from forge_agent_runtime.server import KEEPALIVE, stream_events
from forge_common.adk.turns import TURN_KEY, current_turn


def decode(chunk):
    return json.loads(chunk.removeprefix("data: "))


def outcomes(chunks):
    return [
        event["customMetadata"][TURN_KEY]
        for chunk in chunks
        if chunk.startswith("data: ") and TURN_KEY in (event := decode(chunk)).get("customMetadata", {})
    ]


def reply(text="An intermediate answer."):
    return Event(author="agent", content=types.Content(role="model", parts=[types.Part(text=text)]))


async def test_completion_waits_for_the_whole_invocation_and_cleanup():
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def events():
        try:
            yield reply()
            await release.wait()
            yield reply("The final answer.")
        finally:
            cleaned.set()

    stream = stream_events(events(), "agent", keepalive=0.01)
    chunks = [await anext(stream) for _ in range(4)]
    assert chunks[-1] == KEEPALIVE
    assert [outcome["status"] for outcome in outcomes(chunks)] == ["running"]
    assert not cleaned.is_set()

    release.set()
    chunks.extend([chunk async for chunk in stream])
    assert cleaned.is_set()
    started, finished = outcomes(chunks)
    assert started["runId"] == finished["runId"]
    assert finished["status"] == "succeeded"
    assert outcomes([chunks[-1]]) == [finished]


@pytest.mark.parametrize("failure", ["event", "exception", "serialization", "cancelled"])
async def test_each_failure_has_one_terminal_outcome_and_preserves_partial_text(failure):
    seen = []

    async def events():
        try:
            yield reply("Partial answer")
            if failure == "event":
                yield Event(author="agent", error_code="SAFETY", error_message="The reply was blocked.")
            elif failure == "exception":
                raise RuntimeError("provider disconnected")
            elif failure == "serialization":
                yield Event(author="agent", custom_metadata={"bad": object()})
            else:
                raise asyncio.CancelledError()
        finally:
            turn = current_turn()
            seen.append(turn)

    chunks = [chunk async for chunk in stream_events(events(), "agent")]
    statuses = [outcome["status"] for outcome in outcomes(chunks)]
    assert statuses == ["running", "cancelled" if failure == "cancelled" else "failed"]
    assert "Partial answer" in "".join(chunks)
    assert seen[0].run_id == outcomes(chunks)[-1]["runId"]
    if failure != "cancelled":
        assert outcomes(chunks)[-1]["errorMessage"]
        assert any(decode(c).get("errorCode") for c in chunks if c.startswith("data: "))


async def test_closing_stream_stops_producer_and_finishes_its_cleanup():
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    state = []

    async def events():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield reply()
        finally:
            # Cleanup is allowed to await even when the HTTP stream stops.
            await asyncio.sleep(0)
            state.append(current_turn())
            cleaned.set()

    stream = stream_events(events(), "agent", keepalive=0.01)
    for _ in range(3):
        await anext(stream)
    assert entered.is_set()
    await stream.aclose()
    assert cleaned.is_set()
    assert state[0].status == "cancelled"
    assert current_turn() is None


async def test_native_failure_is_not_overwritten_by_a_later_exception():
    async def events():
        yield Event(author="agent", invocation_id="native", error_code="SAFETY", error_message="Blocked.")
        raise RuntimeError("Later cleanup failure")

    chunks = [chunk async for chunk in stream_events(events(), "agent")]
    terminal = outcomes(chunks)[-1]
    assert terminal["status"] == "failed"
    assert terminal["errorCode"] == "SAFETY"
    assert terminal["errorMessage"] == "Blocked."


async def test_terminal_waits_for_history_persistence_attempt():
    entered, release = asyncio.Event(), asyncio.Event()

    async def events():
        raise RuntimeError("Failed")
        yield

    async def persist(outcome):
        assert outcome.status == "failed"
        entered.set()
        await release.wait()

    stream = stream_events(events(), "agent", keepalive=0.01, persist=persist)
    chunks = [await anext(stream) for _ in range(3)]
    await entered.wait()
    assert outcomes(chunks)[-1]["status"] == "running"
    assert await anext(stream) == KEEPALIVE
    release.set()
    chunks.extend([chunk async for chunk in stream])
    assert outcomes(chunks)[-1]["status"] == "failed"
