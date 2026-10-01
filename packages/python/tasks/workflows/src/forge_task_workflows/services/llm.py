"""Agent steps: a conversation with a model, as a step of a run.

The model is one of the shared model provider configuration's
(model_provider.yaml, which the Forge assistant reads too): the one the
step names, else the configuration's default. Without a
configuration it's Gemini, with the Google API key, as before there was one.
``forge_common.adk.models.ProviderModels`` speaks each provider's API (Gemini
itself; the OpenAI and Anthropic APIs through LiteLLM) and applies the step's
thinking level as its model takes it.

The model gets the step's instructions, and what the previous step handed on;
it may call the tools it's given, each call answered and handed back, until it
answers or runs out of turns (model calls). An agent told to answer in JSON
gives its answer in JSON last, held to the step's schema.

The conversation is kept with the run after every turn, so a retry carries
on from the last turn instead of asking again (and paying again).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from forge_task_workflows.errors import StepFailed
from forge_tasks.errors import TransientError

if TYPE_CHECKING:
    from forge_common.adk.models import ProviderModels
    from forge_common.model_provider import ModelProviderConfig, ResolvedModel

# Tool results handed back to the model, at most (characters of JSON).
TOOL_RESULT_LIMIT = 20_000
# Without a model provider configuration: the Gemini API, and its model when
# none is set (the admin API's assistant has the same, agents/language_models.py).
GEMINI_PROVIDER = "google"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
# Statuses a model's API answers that are worth trying again.
RETRYABLE = frozenset({408, 429})
# What a provider's client raises when it didn't get an answer (LiteLLM's, by name).
UNANSWERED = frozenset(
    {"APIConnectionError", "Timeout", "ServiceUnavailableError", "InternalServerError", "RateLimitError"}
)
_FENCED = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class Tool:
    """A tool the model may call: its name (``orders__lookup``), what it does, its parameters (a JSON Schema)."""

    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], Awaitable[Any]]


def tool_name(name: str) -> str:
    """A tool's name as models take it: letters, digits and underscores (``orders.lookup`` → ``orders__lookup``)."""
    return name.replace(".", "__")


def gemini_config(api_key: str, model: str | None = None) -> ModelProviderConfig:
    """
    The Gemini API as a model provider configuration: ``model`` (by default
    gemini-3.5-flash), which reasons and reads text and images.
    """
    from forge_common.model_provider import ModelProviderConfig

    model = model or DEFAULT_GEMINI_MODEL
    return ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": GEMINI_PROVIDER, "model": model},
            "providers": {
                GEMINI_PROVIDER: {
                    "name": "Google Gemini",
                    "baseUrl": GEMINI_BASE_URL,
                    "api": "google-generative-ai",
                    "auth": {"type": "apiKey", "key": api_key},
                    "models": [
                        {
                            "id": model,
                            "name": " ".join(w[:1].upper() + w[1:] for w in model.split("-")),
                            "reasoning": True,
                            "input": ["text", "image"],
                        }
                    ],
                }
            },
        }
    )


class LanguageModels:
    """
    Conversations with the configured models.

    :param models: The models steps may run on, as the assistant runs them.
    :param timeout: Seconds one model call may take.
    """

    def __init__(self, models: ProviderModels, *, timeout: float) -> None:
        self._models = models
        self._timeout = timeout

    @property
    def default_model(self) -> str:
        """The model a step that names none runs on: ``provider/model``."""
        return self._models.default.ref

    def model_for(self, provider: str, name: str) -> str | None:
        """
        The model a step names, as ``provider/model``: both, else the name
        alone when one provider has it; None for the default.

        :raises StepFailed: It names one the configuration doesn't offer.
        """
        provider, name = provider.strip(), name.strip()
        if not name:
            return None
        found = (self._models.find(f"{provider}/{name}") if provider else None) or self._models.find(name)
        if found is None:
            shown = f"{provider}/{name}" if provider else name
            raise StepFailed(
                f"The model {shown} isn't one Forge offers here; pick one of: {', '.join(self._models.choices)}"
            )
        return found.ref

    async def converse(
        self,
        *,
        model: str | None,
        thinking_level: str | None,
        instructions: str,
        prompt: str,
        tools: list[Tool],
        output_schema: dict[str, Any] | None,
        max_turns: int,
        history: list[dict[str, Any]] | None,
        keep: Callable[[list[dict[str, Any]], int], Awaitable[None]],
        turns: int = 0,
    ) -> Any:
        """The model's answer: text, or the JSON it was told to give."""
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types

        chosen = self._models.resolve(model)
        by_name = {tool.name: tool for tool in tools}
        contents: list[Any] = (
            [types.Content.model_validate(c) for c in history]
            if history
            else [types.Content(role="user", parts=[types.Part(text=prompt)])]
        )

        async def ask(config: Any) -> Any:
            request = LlmRequest(contents=contents, config=config)
            self._models.select(request, chosen.ref, thinking_level)
            reply = None
            try:
                async with asyncio.timeout(self._timeout):
                    async for response in self._models.generate_content_async(request, stream=False):
                        if not response.partial:
                            reply = response
            except (StepFailed, TransientError):
                raise
            except TimeoutError as exc:
                raise TransientError(f"{chosen.ref} didn't answer in {self._timeout:g} seconds") from exc
            except Exception as exc:  # each provider's client raises its own
                raise _refusal(chosen, exc) from exc
            if reply is None or reply.content is None:
                reason = f" ({reply.error_code}: {reply.error_message})" if reply and reply.error_code else ""
                raise StepFailed(f"{chosen.ref} gave no answer{reason}")
            return reply.content

        declarations = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools
        ]
        # A conversation kept from an earlier attempt that already ended in an answer isn't asked again.
        answered = (
            bool(history)
            and contents[-1].role == "model"
            and not any(part.function_call for part in contents[-1].parts or [])
        )
        while not answered:
            last = turns + 1 >= max_turns
            config = types.GenerateContentConfig(
                system_instruction=instructions,
                tools=[types.Tool(function_declarations=declarations)] if declarations else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                # The last turn it may take: it answers, without calling anything.
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE)
                )
                if declarations and last
                else None,
            )
            content = await ask(config)
            turns += 1
            contents.append(content)
            calls = [part.function_call for part in content.parts or [] if part.function_call]
            if not calls:
                await keep([c.model_dump(mode="json", exclude_none=True) for c in contents], turns)
                answered = True
                continue
            answers = []
            for call in calls:
                tool = by_name.get(call.name or "")
                if tool is None:
                    result: Any = {"error": f"There's no tool {call.name}"}
                else:
                    try:
                        result = {"result": await tool.call(dict(call.args or {}))}
                    except StepFailed as failed:
                        result = {"error": failed.message}
                text = json.dumps(result, default=str)
                if len(text) > TOOL_RESULT_LIMIT:
                    result = {"result": text[:TOOL_RESULT_LIMIT], "truncated": True}
                answers.append(
                    types.Part(
                        function_response=types.FunctionResponse(id=call.id, name=call.name or "", response=result)
                    )
                )
            contents.append(types.Content(role="user", parts=answers))
            await keep([c.model_dump(mode="json", exclude_none=True) for c in contents], turns)

        text = _text(contents[-1])
        if output_schema is None:
            return text
        # The answer in JSON, held to the schema: each API asks for it its own way.
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(text="Now give your answer as JSON matching this schema: " + json.dumps(output_schema))
                ],
            )
        )
        structured = (
            types.GenerateContentConfig(
                system_instruction=instructions,
                response_mime_type="application/json",
                response_json_schema=output_schema,
            )
            if chosen.api == "google-generative-ai"
            else types.GenerateContentConfig(system_instruction=instructions, response_schema=output_schema)
        )
        raw = _text(await ask(structured)).strip()
        fenced = _FENCED.match(raw)
        try:
            return json.loads(fenced[1] if fenced else raw)
        except json.JSONDecodeError as exc:
            raise StepFailed(f"{chosen.ref}'s answer isn't JSON: {raw[:200]}") from exc


def _text(content: Any) -> str:
    return "".join(part.text or "" for part in content.parts or [] if not part.thought)


def _refusal(model: ResolvedModel, exc: Exception) -> Exception:
    """What a provider's error means for the run: try again later, or the step fails."""
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or exc)[:500]
    retry = (
        (isinstance(status, int) and (status in RETRYABLE or status >= 500))
        or isinstance(exc, httpx.TransportError | ConnectionError)
        or type(exc).__name__ in UNANSWERED
    )
    if retry:
        return TransientError(f"{model.ref} didn't answer ({status or type(exc).__name__}): {message}")
    return StepFailed(f"{model.ref} refused it ({status or type(exc).__name__}): {message}", status=status)


def language_models(
    *,
    config_path: str | None,
    environment: Mapping[str, str],
    google_api_key: str | None,
    default_model: str | None,
    timeout: float,
) -> LanguageModels | None:
    """
    The models agent steps run on: the model provider configuration's, when
    ``config_path`` names one (its ``${NAME}`` references resolved from
    ``environment``); else Gemini's, with ``google_api_key``; else none.

    :raises ModelProviderConfigError: The configuration can't be read, isn't
        valid, or doesn't declare ``default_model``.
    """
    from forge_common.adk.models import ProviderModels
    from forge_common.model_provider import load_model_provider_config

    if config_path:
        config = load_model_provider_config(config_path, environment)
        return LanguageModels(ProviderModels(config, default=default_model or None), timeout=timeout)
    if google_api_key:
        return LanguageModels(ProviderModels(gemini_config(google_api_key, default_model)), timeout=timeout)
    return None
