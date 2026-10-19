"""One turn at a time in each conversation: a second turn is refused before
anything of it runs, across executors sharing their leases, and a turn
whose lease went to another stops without writing to the conversation."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from forge_agent_runtime.document import parse_document
from forge_agent_runtime.executor import AgentExecutor, DocumentAgentSource, RunRequest
from forge_agent_runtime.leases import (
    MemoryTurnLeases,
    TurnBusy,
    TurnKey,
    TurnLease,
    TurnLeaseLost,
)
from forge_agent_runtime.server import create_router
from forge_common.adk.models import ModelCall, ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from tests.conftest import PROVIDERS

AGENT = "ca_quiet"
DOC = {
    "format": "forge.chat_agent/v1",
    "id": AGENT,
    "name": "Quiet",
    "nodes": [{"id": "agent", "kind": "agent", "name": "Helper", "config": {"instruction": "Help."}}],
    "edges": [],
}


class GatedLlm(BaseLlm):
    """Answers once it's let go: a turn that takes as long as the test likes."""

    model: str = "gpt-test"
    gate: Any = Field(default_factory=asyncio.Event)
    started: Any = Field(default_factory=asyncio.Event)
    calls: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.calls += 1
        self.started.set()
        await self.gate.wait()
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=f"Answer {self.calls}.")])
        )


def executor(llm: GatedLlm, **options: Any) -> AgentExecutor:
    def build(call: ModelCall) -> BaseLlm:
        return llm

    options.setdefault("sessions", InMemorySessionService())
    return AgentExecutor(
        DocumentAgentSource(parse_document(DOC)),
        models=ProviderModels(parse_model_provider_yaml(PROVIDERS), build=build),
        **options,
    )


def turn(said: str) -> RunRequest:
    return RunRequest(
        app_name=AGENT,
        user_id="ada",
        session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=said)]),
    )


async def answers(runner: AgentExecutor, request: RunRequest) -> list[str]:
    return [
        part.text
        async for event in runner.run(request)
        if event.content
        for part in event.content.parts or ()
        if part.text and not event.partial
    ]


async def test_leases_are_one_owners_until_given_back_or_run_out() -> None:
    now = [0.0]
    leases = MemoryTurnLeases(clock=lambda: now[0])
    key = TurnKey(AGENT, "ada", "s1")
    assert await leases.acquire(key, owner="a", seconds=30)
    assert not await leases.acquire(key, owner="b", seconds=30)
    # Another conversation is another lease.
    assert await leases.acquire(TurnKey(AGENT, "ada", "s2"), owner="b", seconds=30)
    assert await leases.renew(key, owner="a", seconds=30)
    assert not await leases.renew(key, owner="b", seconds=30)
    now[0] = 31
    # Run out: the next turn takes it, and the first can't renew it.
    assert await leases.acquire(key, owner="b", seconds=30)
    assert not await leases.renew(key, owner="a", seconds=30)
    await leases.release(key, owner="a")
    assert not await leases.acquire(key, owner="c", seconds=30)
    await leases.release(key, owner="b")
    assert await leases.acquire(key, owner="c", seconds=30)


async def test_a_turn_waits_a_moment_for_the_conversation_then_is_refused() -> None:
    leases = MemoryTurnLeases()
    key = TurnKey(AGENT, "ada", "s1")
    first = await TurnLease.take(leases, key, wait=0)
    with pytest.raises(TurnBusy):
        await TurnLease.take(leases, key, wait=0.1)

    async def give_back_soon() -> None:
        await asyncio.sleep(0.05)
        await first.release()

    giving = asyncio.create_task(give_back_soon())
    second = await TurnLease.take(leases, key, wait=2)
    await giving
    await second.release()


async def test_a_second_turn_is_refused_before_it_runs_and_the_first_finishes() -> None:
    llm = GatedLlm()
    runner = executor(llm, turn_wait=0)
    await runner.sessions.create_session(app_name=AGENT, user_id="ada", session_id="s1")
    first = asyncio.create_task(answers(runner, turn("Hello")))
    await asyncio.wait_for(llm.started.wait(), 5)
    # Sent while the first runs: refused, its message never reaches the conversation.
    with pytest.raises(TurnBusy):
        await answers(runner, turn("Hello again"))
    llm.gate.set()
    # Without the lease the first failed here: its session went stale under the second.
    assert await asyncio.wait_for(first, 5) == ["Answer 1."]
    assert llm.calls == 1
    session = await runner.sessions.get_session(app_name=AGENT, user_id="ada", session_id="s1")
    assert session is not None
    said = [
        part.text
        for event in session.events
        if event.author == "user" and event.content is not None
        for part in event.content.parts or ()
    ]
    assert said == ["Hello"]
    # Once it's over, the next turn runs.
    assert await answers(runner, turn("Hello again")) == ["Answer 2."]
    await runner.close()


async def test_turns_are_one_at_a_time_across_executors_sharing_leases() -> None:
    """Two processes of a deployment: their sessions and leases are shared."""
    leases, sessions = MemoryTurnLeases(), InMemorySessionService()
    first_llm, second_llm = GatedLlm(), GatedLlm()
    one = executor(first_llm, leases=leases, sessions=sessions, turn_wait=0)
    two = executor(second_llm, leases=leases, sessions=sessions, turn_wait=0)
    await sessions.create_session(app_name=AGENT, user_id="ada", session_id="s1")
    running = asyncio.create_task(answers(one, turn("Hello")))
    await asyncio.wait_for(first_llm.started.wait(), 5)
    with pytest.raises(TurnBusy):
        await answers(two, turn("Hello from another tab"))
    assert second_llm.calls == 0
    first_llm.gate.set()
    await asyncio.wait_for(running, 5)
    second_llm.gate.set()
    assert await answers(two, turn("Hello from another tab")) == ["Answer 1."]
    await one.close()
    await two.close()


class TakenAway(MemoryTurnLeases):
    """Leases whose renewal finds another turn took the conversation."""

    async def renew(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        return False


async def test_a_turn_whose_lease_went_to_another_stops_without_writing() -> None:
    llm = GatedLlm()
    runner = executor(llm, leases=TakenAway(), lease_seconds=0.03, turn_wait=0)
    await runner.sessions.create_session(app_name=AGENT, user_id="ada", session_id="s1")
    running = asyncio.create_task(answers(runner, turn("Hello")))
    await asyncio.wait_for(llm.started.wait(), 5)
    await asyncio.sleep(0.1)  # Its renewal finds the conversation gone.
    llm.gate.set()
    with pytest.raises(TurnLeaseLost):
        await asyncio.wait_for(running, 5)
    session = await runner.sessions.get_session(app_name=AGENT, user_id="ada", session_id="s1")
    assert session is not None
    # Nothing past the point it lost the conversation: no answer, no failure written.
    assert not any(event.author != "user" for event in session.events)
    await runner.close()


def test_the_run_api_answers_409_while_another_turn_runs() -> None:
    llm = GatedLlm()
    llm.gate.set()
    runner = executor(llm, turn_wait=0)
    app = FastAPI()
    app.include_router(create_router(runner))
    with TestClient(app) as client:
        sid = client.post(f"/apps/{AGENT}/users/ada/sessions", json={}).json()["id"]
        body = {
            "appName": AGENT,
            "userId": "ada",
            "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": "Hello"}]},
        }
        held = client.portal.call(  # type: ignore[union-attr]
            lambda: runner.take_turn(RunRequest.model_validate(body))
        )
        refused = client.post("/run_sse", json=body)
        assert refused.status_code == 409
        assert refused.headers["Retry-After"] == "2"
        client.portal.call(held.release)  # type: ignore[union-attr]
        ran = client.post("/run_sse", json=body)
        assert ran.status_code == 200 and "Answer 1." in ran.text
        # Given back with the turn: the next one runs too.
        assert client.post("/run_sse", json=body).status_code == 200


async def test_a_lease_whose_stream_never_started_is_given_back_as_its_dropped() -> None:
    leases = MemoryTurnLeases()
    key = TurnKey(AGENT, "ada", "s1")

    async def never_started() -> AsyncGenerator[str, None]:
        yield "never"

    lease = await TurnLease.take(leases, key, wait=0)
    stream = never_started()
    lease.held_by(stream)
    del stream
    gc.collect()
    for _ in range(10):
        await asyncio.sleep(0)
    assert await leases.acquire(key, owner="next", seconds=30)
