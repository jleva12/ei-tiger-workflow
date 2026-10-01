from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import BaseLlm, Gemini
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from forge_common.adk.models import ModelCall, ProviderModels, gemini_thinking_config
from forge_common.model_provider import (
    ModelProviderConfig,
    ModelProviderConfigError,
    parse_model_provider_yaml,
)

CONFIG = """
version: 1
default: {provider: google, model: gemini-3.5-flash}
providers:
  google:
    name: Google Gemini
    baseUrl: https://generativelanguage.googleapis.com/v1beta
    api: google-generative-ai
    headers: {x-app: forge}
    auth: {type: apiKey, key: gemini-key}
    models:
      - {id: gemini-3.5-flash, reasoning: true}
      - {id: gemini-2.5-flash, reasoning: true}
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: openai-key}
    models:
      - id: gpt-5.2
        reasoning: true
        maxTokens: 32000
        thinkingLevelMap: {off: none, xhigh: xhigh}
        headers: {X-App: coding}
      - {id: gpt-4.1, api: openai-completions}
  anthropic:
    baseUrl: https://api.anthropic.com
    api: anthropic-messages
    auth: {type: apiKey, key: anthropic-key}
    models:
      - {id: claude-sonnet-5, reasoning: true}
"""


def config(text: str = CONFIG) -> ModelProviderConfig:
    return parse_model_provider_yaml(text, {})


class ScriptedLlm(BaseLlm):
    """Answers each model call with its next turn."""

    model: str = "scripted"
    turns: list[list[types.Part]] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request.model_copy(deep=True))
        parts = self.turns.pop(0) if self.turns else [types.Part.from_text(text="Done.")]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


class Recorder:
    """A build that records each call and answers with one scripted model."""

    def __init__(self, llm: ScriptedLlm | None = None) -> None:
        self.llm = llm or ScriptedLlm()
        self.calls: list[ModelCall] = []

    def __call__(self, call: ModelCall) -> BaseLlm:
        self.calls.append(call)
        return self.llm


async def run(models: ProviderModels, request: LlmRequest) -> list[LlmResponse]:
    return [reply async for reply in models.generate_content_async(request)]


async def test_a_request_runs_on_the_model_select_chose() -> None:
    build = Recorder()
    models = ProviderModels(config(), build=build)
    request = LlmRequest()

    chosen = models.select(request, "openai/gpt-5.2", "HIGH")
    replies = await run(models, request)

    assert chosen.ref == "openai/gpt-5.2"
    assert replies[0].content.parts[0].text == "Done."  # type: ignore[union-attr,index]
    [call] = build.calls
    assert (call.model.ref, call.thinking_level, call.credential) == (
        "openai/gpt-5.2",
        "high",
        "openai-key",
    )
    [sent] = build.llm.requests
    assert sent.model == "openai/responses/gpt-5.2"
    assert sent.config.max_output_tokens == 32000
    assert sent.config.thinking_config is None


async def test_gemini_requests_get_its_model_id_and_thinking_config() -> None:
    build = Recorder()
    models = ProviderModels(config(), build=build)
    first, second, third = LlmRequest(), LlmRequest(), LlmRequest()

    models.select(first, "gemini-2.5-flash", "low")
    models.select(second, None, "off")
    # Not a level.
    models.select(third, "google/gemini-3.5-flash", "extreme")
    for request in (first, second, third):
        await run(models, request)

    sent = build.llm.requests
    assert [request.model for request in sent] == [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert sent[0].config.thinking_config == types.ThinkingConfig(
        include_thoughts=True, thinking_budget=1024
    )
    assert sent[1].config.thinking_config == types.ThinkingConfig(
        include_thoughts=False, thinking_level=types.ThinkingLevel.MINIMAL
    )
    # Its own level, thoughts shown.
    assert sent[2].config.thinking_config == types.ThinkingConfig(include_thoughts=True)
    # Gemini keeps the model's own reply limit.
    assert sent[0].config.max_output_tokens is None


async def test_unknown_or_unchosen_models_run_on_the_default() -> None:
    build = Recorder()
    models = ProviderModels(config(), build=build)
    unknown, named = LlmRequest(), LlmRequest(model="anthropic/claude-sonnet-5")

    assert models.select(unknown, "gemini-9-ultra").ref == "google/gemini-3.5-flash"
    await run(models, unknown)
    # Without select, the request's own model.
    await run(models, named)

    assert [call.model.ref for call in build.calls] == [
        "google/gemini-3.5-flash",
        "anthropic/claude-sonnet-5",
    ]
    assert build.calls[1].thinking_level is None


def test_the_models_it_serves_can_be_limited_and_given_a_default() -> None:
    models = ProviderModels(
        config(), default="gpt-5.2", models=["anthropic/claude-sonnet-5", "gpt-5.2"]
    )

    assert models.choices == ("openai/gpt-5.2", "anthropic/claude-sonnet-5")
    assert models.model == "openai/gpt-5.2"
    assert models.find("gemini-3.5-flash") is None
    assert models.resolve("gemini-3.5-flash").ref == "openai/gpt-5.2"


def test_models_it_cant_run_are_refused() -> None:
    with pytest.raises(ModelProviderConfigError, match="The default model names gpt-9"):
        ProviderModels(config(), default="gpt-9")
    oauth = CONFIG.replace(
        "auth: {type: apiKey, key: gemini-key}",
        "auth: {type: oauth2, tokenUrl: 'https://t.test', clientId: c, clientSecret: s}",
    )
    with pytest.raises(ModelProviderConfigError, match="OAuth2 isn't supported"):
        ProviderModels(config(oauth))


def test_gemini_thinking_follows_the_models_map() -> None:
    text = CONFIG.replace(
        "{id: gemini-3.5-flash, reasoning: true}",
        "{id: gemini-3.5-flash, reasoning: true, thinkingLevelMap: {low: medium}}",
    ).replace(
        "{id: gemini-2.5-flash, reasoning: true}",
        "{id: gemini-2.5-flash, reasoning: true, thinkingLevelMap: {high: '-1'}}",
    )
    parsed = config(text)
    three = parsed.find("gemini-3.5-flash").model  # type: ignore[union-attr]
    two = parsed.find("gemini-2.5-flash").model  # type: ignore[union-attr]

    assert gemini_thinking_config(three, "low").thinking_level == types.ThinkingLevel.MEDIUM
    assert gemini_thinking_config(three, "xhigh").thinking_level == types.ThinkingLevel.HIGH
    assert gemini_thinking_config(two, "high").thinking_budget == -1


async def test_openai_and_anthropic_run_on_litellm_with_their_settings() -> None:
    models = ProviderModels(config())
    target = models.resolve("gpt-5.2")

    responses = models._default_build(ModelCall(target, "xhigh", "openai-key"))
    completions = models._default_build(ModelCall(models.resolve("gpt-4.1"), None, "openai-key"))
    anthropic = models._default_build(
        ModelCall(models.resolve("claude-sonnet-5"), "off", "anthropic-key")
    )

    assert isinstance(responses, LiteLlm)
    assert responses.model == "openai/responses/gpt-5.2"
    assert responses._additional_args == {
        "api_base": "https://api.openai.com/v1",
        "api_key": "openai-key",
        "num_retries": 2,
        "extra_headers": {"X-App": "coding"},
        "reasoning_effort": "xhigh",
    }
    assert completions.model == "openai/gpt-4.1"
    assert "reasoning_effort" not in completions._additional_args  # type: ignore[attr-defined]
    assert anthropic.model == "anthropic/claude-sonnet-5"
    # off without a mapped value: the provider's default, which is no thinking.
    assert "reasoning_effort" not in anthropic._additional_args  # type: ignore[attr-defined]


def test_gemini_runs_on_adks_gemini_at_the_providers_url() -> None:
    models = ProviderModels(config())
    call = ModelCall(models.resolve(None), None, "gemini-key")

    gemini = models._default_build(call)

    assert isinstance(gemini, Gemini)
    assert gemini.model == "gemini-3.5-flash"
    assert models._default_build(call) is gemini
    options = gemini.api_client._api_client._http_options
    assert options.base_url == "https://generativelanguage.googleapis.com/"
    assert options.api_version == "v1beta"
    assert options.headers is not None and options.headers["x-app"] == "forge"


async def test_oauth2_providers_run_with_their_current_token() -> None:
    text = CONFIG.replace(
        "auth: {type: apiKey, key: openai-key}",
        "auth: {type: oauth2, tokenUrl: 'https://identity.test/token', clientId: c, "
        "clientSecret: s}",
    )
    tokens = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"access_token": "gateway-token"})
    )
    build = Recorder()
    models = ProviderModels(config(text), build=build, token_transport=tokens)
    request = LlmRequest()

    models.select(request, "gpt-5.2")
    await run(models, request)

    assert build.calls[0].credential == "gateway-token"


async def test_an_agent_changes_model_between_turns_and_keeps_tool_calls_paired() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part.from_function_call(name="look_up", args={"key": "a"})],
            [types.Part.from_text(text="It's 1.")],
            [types.Part.from_text(text="Hello.")],
        ]
    )
    models = ProviderModels(config(), build=Recorder(llm))

    def choose(callback_context: CallbackContext, llm_request: LlmRequest) -> None:
        state = callback_context.state
        models.select(llm_request, state.get("model"), state.get("thinking_level"))

    def look_up(key: str) -> dict[str, Any]:
        """Looks a key up."""
        return {"value": 1}

    agent = LlmAgent(name="helper", model=models, tools=[look_up], before_model_callback=choose)
    runner = InMemoryRunner(agent=agent, app_name="test")
    session = await runner.session_service.create_session(app_name="test", user_id="u")

    async def say(text: str, **state: str) -> None:
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
            state_delta=state,
        ):
            pass

    await say("What's a?", model="gpt-5.2", thinking_level="low")
    await say("Hi", model="google/gemini-2.5-flash", thinking_level="high")

    first, second, third = llm.requests
    assert [first.model, second.model, third.model] == [
        "openai/responses/gpt-5.2",
        "openai/responses/gpt-5.2",
        "gemini-2.5-flash",
    ]
    # ADK dropped the id it made up for the call; the router gave the call
    # and its result the same new one.
    parts = [part for content in second.contents for part in content.parts or ()]
    call = next(part.function_call for part in parts if part.function_call)
    response = next(part.function_response for part in parts if part.function_response)
    assert call.id and call.id == response.id
    assert third.config.thinking_config == types.ThinkingConfig(
        include_thoughts=True, thinking_budget=24576
    )
