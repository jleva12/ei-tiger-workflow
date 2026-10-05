"""What the usage plugin records of an agent's model and tool calls: their
tokens counted the same whichever API answered, their price, and whom
they're for."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from forge_common.adk.usage import (
    MODEL_KEY,
    Attribution,
    Tokens,
    UsageCall,
    UsagePlugin,
    attributed,
    cost_of,
    current_sink,
    recording,
    tokens_of,
)
from forge_common.model_provider import Cost

WHO = Attribution("org-1", "agent", "ca_support", "Support desk")
AT = datetime(2026, 10, 4, 9, 30, tzinfo=UTC)


def usage(**counts: int) -> types.GenerateContentResponseUsageMetadata:
    return types.GenerateContentResponseUsageMetadata.model_validate(counts)


def price(**per_million: float) -> Cost:
    """A price as model_provider.yaml writes it (``cacheRead``)."""
    return Cost.model_validate(
        {"cacheRead" if k == "cache_read" else k: v for k, v in per_million.items()}
    )


class TestTokens:
    def test_litellm_counts_reasoning_inside_the_reply(self) -> None:
        # OpenAI: completion 300 includes 120 reasoning; total = prompt + completion.
        found = tokens_of(
            usage(
                prompt_token_count=1000,
                candidates_token_count=300,
                thoughts_token_count=120,
                cached_content_token_count=400,
                total_token_count=1300,
            )
        )
        assert found == Tokens(input=1000, cached=400, output=300, thinking=120)

    def test_gemini_counts_thoughts_beside_the_reply(self) -> None:
        found = tokens_of(
            usage(
                prompt_token_count=1000,
                candidates_token_count=300,
                thoughts_token_count=120,
                total_token_count=1420,
            )
        )
        assert found == Tokens(input=1000, cached=0, output=420, thinking=120)

    def test_tool_use_prompts_are_input(self) -> None:
        found = tokens_of(
            usage(
                prompt_token_count=100,
                tool_use_prompt_token_count=50,
                candidates_token_count=10,
                total_token_count=160,
            )
        )
        assert found.input == 150

    def test_no_usage_is_nothing(self) -> None:
        assert tokens_of(None) == Tokens()


class TestCost:
    def test_cached_input_costs_the_cache_price(self) -> None:
        cost = cost_of(
            Tokens(input=1_000_000, cached=500_000, output=100_000),
            price(input=2, output=10, cache_read=0.2),
        )
        # 500k fresh at $2, 500k cached at $0.20, 100k out at $10.
        assert cost == pytest.approx(1.0 + 0.1 + 1.0)

    def test_a_cache_write_without_its_own_price_costs_input(self) -> None:
        cost = cost_of(Tokens(input=1_000_000, cache_write=1_000_000), price(input=2, output=10))
        assert cost == pytest.approx(2.0)

    def test_no_price_or_an_all_zero_one_is_unpriced(self) -> None:
        assert cost_of(Tokens(input=10), None) is None
        assert cost_of(Tokens(input=10), Cost()) is None


class Scripted(BaseLlm):
    """A model that answers with the responses it's given, in order."""

    model: str = "scripted"
    replies: list[list[LlmResponse]] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        for reply in self.replies.pop(0):
            yield reply


def lookup(city: str) -> dict[str, Any]:
    """Looks a city's weather up."""
    return {"result": f"Sunny in {city}"}


def broken(city: str) -> dict[str, Any]:
    """Always fails."""
    return {"error": "No such city"}


def reply(text: str | None = None, *, call: str | None = None, **counts: int) -> LlmResponse:
    parts = (
        [types.Part(function_call=types.FunctionCall(name=call, args={"city": "Oslo"}))]
        if call
        else [types.Part(text=text or "")]
    )
    return LlmResponse(
        content=types.Content(role="model", parts=parts),
        usage_metadata=usage(**counts) if counts else None,
        custom_metadata={MODEL_KEY: "openai/gpt-5.2"},
    )


async def run(plugin: UsagePlugin, model: Scripted, tools: list[Any]) -> None:
    agent = LlmAgent(name="helper", model=model, tools=tools)
    runner = Runner(
        app=App(name="usage_test", root_agent=agent, plugins=[plugin]),
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    message = types.Content(role="user", parts=[types.Part(text="Weather?")])
    async for _ in runner.run_async(user_id="user-ada", session_id="s-1", new_message=message):
        pass
    await runner.close()


class TestPlugin:
    async def test_it_records_each_model_and_tool_call(self) -> None:
        calls: list[UsageCall] = []

        async def sink(call: UsageCall) -> None:
            calls.append(call)

        model = Scripted(
            replies=[
                [
                    reply(
                        call="lookup",
                        prompt_token_count=100,
                        candidates_token_count=20,
                        total_token_count=120,
                    )
                ],
                [
                    # A streamed fragment carries running counts: not a call.
                    reply("Sun", prompt_token_count=1, total_token_count=1).model_copy(
                        update={"partial": True}
                    ),
                    reply(
                        "Sunny.",
                        prompt_token_count=150,
                        candidates_token_count=5,
                        total_token_count=155,
                    ),
                ],
            ]
        )
        gpt = price(input=1.75, output=14, cache_read=0.175)
        plugin = UsagePlugin(
            sink,
            WHO,
            price=lambda name: gpt if name == "openai/gpt-5.2" else None,
            clock=lambda: AT,
        )
        await run(plugin, model, [lookup])

        assert [(call.call, call.name) for call in calls] == [
            ("model", "openai/gpt-5.2"),
            ("tool", "lookup"),
            ("model", "openai/gpt-5.2"),
        ]
        first = calls[0]
        assert first.attribution == WHO
        assert (first.input_tokens, first.output_tokens) == (100, 20)
        assert first.cost == pytest.approx((100 * 1.75 + 20 * 14) / 1_000_000)
        assert first.author == "helper"
        assert first.user_id == "user-ada"
        # An agent's run is its conversation.
        assert first.session_id == first.run_id == "s-1"
        assert first.at == AT
        assert not any(call.failed for call in calls)

    async def test_a_tool_that_answers_an_error_failed(self) -> None:
        calls: list[UsageCall] = []

        async def sink(call: UsageCall) -> None:
            calls.append(call)

        model = Scripted(replies=[[reply(call="broken")], [reply("Sorry.")]])
        await run(UsagePlugin(sink, WHO), model, [broken])
        tool = next(call for call in calls if call.call == "tool")
        assert tool.failed

    async def test_without_an_attribution_nothing_is_recorded_unless_the_turn_names_one(
        self,
    ) -> None:
        calls: list[UsageCall] = []

        async def sink(call: UsageCall) -> None:
            calls.append(call)

        plugin = UsagePlugin(sink)
        await run(
            plugin,
            Scripted(replies=[[reply("Hi.", prompt_token_count=5, total_token_count=5)]]),
            [],
        )
        assert calls == []

        assistant = Attribution("org-2", "assistant", "assistant", "Assistant", user_id="user-ada")
        with attributed(assistant):
            await run(
                plugin,
                Scripted(replies=[[reply("Hi.", prompt_token_count=5, total_token_count=5)]]),
                [],
            )
        assert [call.attribution for call in calls] == [assistant]

    async def test_a_sink_that_fails_never_fails_the_run(self) -> None:
        async def sink(call: UsageCall) -> None:
            raise RuntimeError("database down")

        await run(
            UsagePlugin(sink, WHO), Scripted(replies=[[reply("Hi.", prompt_token_count=5)]]), []
        )


def test_recording_names_the_sink_inside_only() -> None:
    async def sink(call: UsageCall) -> None: ...

    assert current_sink() is None
    with recording(sink):
        assert current_sink() is sink
    assert current_sink() is None
