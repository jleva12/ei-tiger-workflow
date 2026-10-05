"""The OpenAI summarizer client: request shape, temperature, and error classes."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from forge_embeddings.clients import build_models
from forge_embeddings.config import ModelSettings
from forge_embeddings.llm import LLMError, OpenAILLMClient
from forge_tasks.errors import TaskError


class Rejected(Exception):
    def __init__(self, status_code: int, param: str | None = None) -> None:
        super().__init__(f"error {status_code}")
        self.status_code = status_code
        self.param = param


class FakeResponses:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output_text=outcome)


def client(responses: FakeResponses, **kwargs: Any) -> OpenAILLMClient:
    return OpenAILLMClient(model="gpt-6-luna", client=SimpleNamespace(responses=responses), **kwargs)


async def test_sends_the_system_prompt_as_instructions_with_the_output_budget() -> None:
    responses = FakeResponses('{"summary": "x"}')
    reply = await client(responses, reasoning_effort="low").complete(system="sys", user="diff", max_tokens=800)
    assert reply == '{"summary": "x"}'
    assert responses.calls == [
        {
            "model": "gpt-6-luna",
            "instructions": "sys",
            "input": "diff",
            "max_output_tokens": 800,
            "reasoning": {"effort": "low"},
        }
    ]


async def test_a_model_that_rejects_temperature_is_asked_again_without_it_from_then_on() -> None:
    responses = FakeResponses(Rejected(400, "temperature"), "first", "second")
    llm = client(responses)
    assert await llm.complete(system="s", user="u", temperature=0.0) == "first"
    assert await llm.complete(system="s", user="u", temperature=0.0) == "second"
    assert [("temperature" in call) for call in responses.calls] == [True, False, False]


async def test_a_reasoning_model_is_never_sent_a_temperature() -> None:
    responses = FakeResponses("first", "second")
    llm = client(responses, reasoning_effort="low")
    assert await llm.complete(system="s", user="u", temperature=0.0) == "first"
    assert await llm.complete(system="s", user="u", temperature=0.0) == "second"
    assert [("temperature" in call) for call in responses.calls] == [False, False]


async def test_concurrent_requests_wait_for_the_first_to_learn_about_temperature() -> None:
    responses = FakeResponses(Rejected(400, "temperature"), *[str(n) for n in range(8)])
    llm = client(responses)
    replies = await asyncio.gather(*(llm.complete(system="s", user="u", temperature=0.0) for _ in range(8)))
    assert sorted(replies) == [str(n) for n in range(8)]
    # One rejection, not one per request in flight.
    assert [("temperature" in call) for call in responses.calls] == [True] + [False] * 8


async def test_a_model_that_takes_temperature_keeps_getting_it() -> None:
    responses = FakeResponses("first", "second")
    llm = client(responses)
    assert await llm.complete(system="s", user="u", temperature=0.2) == "first"
    assert await llm.complete(system="s", user="u", temperature=0.2) == "second"
    assert [call.get("temperature") for call in responses.calls] == [0.2, 0.2]


async def test_rejected_requests_are_permanent_and_others_transient() -> None:
    with pytest.raises(TaskError) as rejected:
        await client(FakeResponses(Rejected(401))).complete(system="s", user="u")
    assert rejected.value.permanent
    with pytest.raises(LLMError):
        await client(FakeResponses(Rejected(429))).complete(system="s", user="u")
    # Another 400 is not mistaken for the temperature rejection.
    with pytest.raises(TaskError) as other:
        await client(FakeResponses(Rejected(400, "input"))).complete(system="s", user="u", temperature=0.0)
    assert other.value.permanent


def test_the_openai_provider_builds_an_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = ModelSettings(
        _env_file=None, llm={"provider": "openai", "model": "gpt-6-luna", "reasoning_effort": "low"}
    )  # type: ignore[call-arg]
    models = build_models(settings)
    assert models.llm_factory is not None
    llm = models.llm_factory()
    assert isinstance(llm, OpenAILLMClient)
    assert llm.model_id == "gpt-6-luna"
