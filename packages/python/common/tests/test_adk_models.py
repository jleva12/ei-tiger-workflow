import asyncio
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
from google.genai import errors, types
from pydantic import Field

from forge_common.adk.models import ModelCall, ProviderModels, gemini_thinking_config
from forge_common.adk.usage import MODEL_KEY
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


class ResponseLlm(BaseLlm):
    """Exact provider frames, including empty and prematurely ended streams."""

    model: str = "scripted"
    responses: list[LlmResponse] = Field(default_factory=list)
    failure: Any = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        for response in self.responses:
            yield response
        if self.failure is not None:
            raise self.failure


async def provider_responses(*responses: LlmResponse) -> list[LlmResponse]:
    models = ProviderModels(config(), build=lambda _: ResponseLlm(responses=list(responses)))
    return [response async for response in models.generate_content_async(LlmRequest(), stream=True)]


@pytest.mark.parametrize("reason", [types.FinishReason.MAX_TOKENS, types.FinishReason.SAFETY])
async def test_gemini_abnormal_completion_keeps_text_and_reports_failure(
    reason: types.FinishReason,
) -> None:
    # ADK leaves error_code empty when a Gemini candidate contains text,
    # even when the provider explicitly reports an incomplete/refused reply.
    raw = LlmResponse.create(
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="Answer so far")]),
                    finish_reason=reason,
                )
            ]
        )
    )
    assert raw.error_code is None
    [response] = await provider_responses(raw)
    assert response.error_code == reason.value
    assert response.error_message
    assert response.content and response.content.parts
    assert response.content.parts[0].text == "Answer so far"
    assert response.custom_metadata == {MODEL_KEY: "google/gemini-3.5-flash"}


@pytest.mark.parametrize(
    "responses",
    [
        [],
        [LlmResponse()],
        [
            LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=" ")]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
    ],
)
async def test_empty_provider_exhaustion_is_an_explicit_failure(
    responses: list[LlmResponse],
) -> None:
    completed = await provider_responses(*responses)
    assert completed[-1].error_code == "EMPTY_MODEL_RESPONSE"
    assert completed[-1].error_message == "The model returned no answer. Try again."


async def test_partial_only_exhaustion_keeps_text_without_promoting_tool_arguments() -> None:
    chunks = [
        LlmResponse(
            partial=True,
            content=types.Content(
                role="model",
                parts=[
                    types.Part(text="Working", thought=True),
                    types.Part(text="Answer "),
                ],
            ),
        ),
        LlmResponse(
            partial=True,
            content=types.Content(
                role="model",
                parts=[
                    types.Part(text="so far"),
                    types.Part(
                        function_call=types.FunctionCall(name="save", args={"value": "unfinished"})
                    ),
                ],
            ),
        ),
    ]
    completed = await provider_responses(*chunks)
    assert completed[:2] == chunks
    final = completed[-1]
    assert final.error_code == "INCOMPLETE_MODEL_RESPONSE"
    assert final.partial is False
    assert final.content and final.content.parts
    assert [(part.text, part.thought) for part in final.content.parts] == [
        ("Working", True),
        ("Answer ", None),
        ("so far", None),
    ]
    assert not final.get_function_calls()


async def test_a_provider_exception_saves_partial_text_and_error_without_executing_tools() -> None:
    calls: list[str] = []

    def save(value: str) -> dict[str, str]:
        """Save a value."""
        calls.append(value)
        return {"saved": value}

    provider = ResponseLlm(
        responses=[
            LlmResponse(
                partial=True,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Working", thought=True), types.Part(text="Answer ")],
                ),
            ),
            LlmResponse(
                partial=True,
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(text="so far"),
                        types.Part(
                            function_call=types.FunctionCall(
                                name="save", args={"value": "unfinished"}
                            )
                        ),
                    ],
                ),
            ),
        ],
        failure=RuntimeError("Internal provider connection details: secret"),
    )
    models = ProviderModels(config(), build=lambda _: provider)
    runner = InMemoryRunner(
        agent=LlmAgent(name="helper", model=models, tools=[save]), app_name="test"
    )
    session = await runner.session_service.create_session(app_name="test", user_id="u")
    events = [
        event
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Save it")]),
        )
    ]
    stored = await runner.session_service.get_session(
        app_name="test", user_id="u", session_id=session.id
    )
    assert stored is not None
    [saved] = [event for event in stored.events if event.author == "helper"]
    assert saved.error_code == "INTERNAL_ERROR"
    assert saved.error_message == "The model response could not be completed. Try again."
    assert saved.partial is False
    assert saved.content and saved.content.parts
    assert [(part.text, part.thought) for part in saved.content.parts] == [
        ("Working", True),
        ("Answer ", None),
        ("so far", None),
    ]
    assert calls == []
    assert not saved.get_function_calls()
    assert [event.error_code for event in events if event.error_code] == ["INTERNAL_ERROR"]
    await runner.close()


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_terminal_frames_keep_partial_text_in_saved_history(
    partial: bool, interrupted: bool
) -> None:
    final = LlmResponse(
        partial=partial,
        interrupted=interrupted,
        error_code=None if interrupted else "PROVIDER_UNAVAILABLE",
        error_message=None if interrupted else "Please retry later.",
        content=(
            types.Content(
                role="model",
                parts=[
                    types.Part(text="so far"),
                    types.Part(function_call=types.FunctionCall(name="save", args={})),
                ],
            )
            if partial
            else None
        ),
    )
    models = ProviderModels(
        config(),
        build=lambda _: ResponseLlm(
            responses=[
                LlmResponse(
                    partial=True,
                    content=types.Content(role="model", parts=[types.Part(text="Answer ")]),
                ),
                final,
            ],
        ),
    )
    runner = InMemoryRunner(agent=LlmAgent(name="helper", model=models), app_name="test")
    session = await runner.session_service.create_session(app_name="test", user_id="u")
    events = [
        event
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Hello")]),
        )
    ]
    stored = await runner.session_service.get_session(
        app_name="test", user_id="u", session_id=session.id
    )
    assert stored is not None
    [saved] = [event for event in stored.events if event.author == "helper"]
    assert saved.partial is False
    assert saved.interrupted is interrupted
    assert saved.error_code == (None if interrupted else "PROVIDER_UNAVAILABLE")
    assert saved.content
    assert "".join(part.text or "" for part in saved.content.parts or ()) == (
        "Answer so far" if partial else "Answer "
    )
    assert not saved.get_function_calls()
    assert len(events) == 2
    await runner.close()


async def test_a_failed_completed_answer_is_not_duplicated_by_its_prior_partials() -> None:
    completed = await provider_responses(
        LlmResponse(
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text="Answer ")]),
        ),
        LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="Answer so far")]),
            finish_reason=types.FinishReason.MAX_TOKENS,
        ),
    )
    assert completed[-1].content and completed[-1].content.parts
    assert [part.text for part in completed[-1].content.parts] == ["Answer so far"]


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            errors.APIError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "secret"}}),
            "RESOURCE_EXHAUSTED",
            "The model's provider is rate limiting requests. Wait a moment and try again.",
        ),
        (
            errors.APIError(503, {"error": {"status": "UNAVAILABLE", "message": "secret"}}),
            "UNAVAILABLE",
            "The model's provider is unavailable. Wait a moment and try again.",
        ),
        (
            httpx.ReadTimeout("secret"),
            "MODEL_TIMEOUT",
            "The model timed out before completing its response. Try again.",
        ),
    ],
)
async def test_provider_exception_after_partial_text_uses_a_safe_actionable_error(
    error: Exception, code: str, message: str
) -> None:
    models = ProviderModels(
        config(),
        build=lambda _: ResponseLlm(
            responses=[
                LlmResponse(
                    partial=True,
                    content=types.Content(role="model", parts=[types.Part(text="Answer so far")]),
                )
            ],
            failure=error,
        ),
    )
    replies = [reply async for reply in models.generate_content_async(LlmRequest(), stream=True)]
    assert len(replies) == 2
    assert replies[-1].error_code == code
    assert replies[-1].error_message == message
    assert replies[-1].content and replies[-1].content.parts
    assert replies[-1].content.parts[0].text == "Answer so far"


@pytest.mark.parametrize("partial", [None, False, True])
async def test_model_errors_before_output_or_after_completion_and_cancellation_propagate(
    partial: bool | None,
) -> None:
    # A completed response is already saved. Cancellation must never be
    # converted into a failure or attempt to yield from generator teardown.
    error = asyncio.CancelledError() if partial else RuntimeError("Model failed")
    response = LlmResponse(
        partial=partial,
        content=types.Content(role="model", parts=[types.Part(text="Answer")]),
    )
    models = ProviderModels(
        config(),
        build=lambda _: ResponseLlm(responses=[] if partial is None else [response], failure=error),
    )
    replies: list[LlmResponse] = []
    with pytest.raises(type(error)) as raised:
        async for reply in models.generate_content_async(LlmRequest(), stream=True):
            replies.append(reply)
    assert raised.value is error
    assert replies == ([] if partial is None else [response])


async def test_transient_partial_frames_wait_for_the_completed_reply() -> None:
    partial = LlmResponse(
        partial=True,
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="Answer"),
            ],
        ),
    )
    final = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="Answer.")]),
        finish_reason=types.FinishReason.STOP,
    )
    completed = await provider_responses(partial, final)
    assert completed == [partial, final]
    assert all(response.error_code is None for response in completed)


@pytest.mark.parametrize("tool", ["look_up", "ask_person", "adk_request_confirmation"])
async def test_complete_tool_calls_and_person_pauses_are_not_empty_failures(tool: str) -> None:
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_call=types.FunctionCall(name=tool, args={})),
            ],
        )
    )
    assert await provider_responses(response) == [response]
    assert response.error_code is None


async def test_native_error_details_are_kept_without_a_second_empty_failure() -> None:
    response = LlmResponse(error_code="QUOTA", error_message="Please retry later.")
    assert await provider_responses(response) == [response]
    assert response.error_message == "Please retry later."


async def test_a_message_only_provider_error_gets_a_code_without_losing_its_reason() -> None:
    response = LlmResponse(error_message="The provider refused the response.")
    assert await provider_responses(response) == [response]
    assert response.error_code == "MODEL_ERROR"
    assert response.error_message == "The provider refused the response."


async def test_empty_completion_keeps_usage_on_one_failed_model_response() -> None:
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=20, candidates_token_count=0
    )
    [response] = await provider_responses(LlmResponse(usage_metadata=usage))
    assert response.error_code == "EMPTY_MODEL_RESPONSE"
    assert response.usage_metadata == usage


@pytest.mark.parametrize("partial", [False, True])
async def test_an_explicit_model_interruption_remains_a_cancellation(partial: bool) -> None:
    response = LlmResponse(interrupted=True, partial=partial)
    assert await provider_responses(response) == [response]
    assert response.error_code is None


@pytest.mark.parametrize(
    "failure",
    [
        {"finish_reason": types.FinishReason.MAX_TOKENS},
        {"interrupted": True},
        {"error_message": "The provider refused the response."},
    ],
)
async def test_a_failed_or_interrupted_function_call_cannot_execute_a_tool(
    failure: dict[str, Any],
) -> None:
    calls: list[str] = []

    def save(value: str) -> dict[str, str]:
        """Save a value."""
        calls.append(value)
        return {"saved": value}

    failed = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="Starting the operation"),
                types.Part(
                    function_call=types.FunctionCall(name="save", args={"value": "incomplete"})
                ),
            ],
        ),
        **failure,
    )
    models = ProviderModels(config(), build=lambda _: ResponseLlm(responses=[failed]))
    runner = InMemoryRunner(
        agent=LlmAgent(name="helper", model=models, tools=[save]), app_name="test"
    )
    session = await runner.session_service.create_session(app_name="test", user_id="u")
    events = [
        event
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Save it")]),
        )
    ]
    assert calls == []
    if failure.get("interrupted"):
        assert any(event.interrupted for event in events)
        assert all(event.error_code is None for event in events)
    else:
        expected_code = "MAX_TOKENS" if "finish_reason" in failure else "MODEL_ERROR"
        assert any(event.error_code == expected_code for event in events)
    assert not any(event.get_function_calls() for event in events)
    assert any(
        part.text == "Starting the operation"
        for event in events
        if event.content
        for part in event.content.parts or ()
    )
    await runner.close()


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
