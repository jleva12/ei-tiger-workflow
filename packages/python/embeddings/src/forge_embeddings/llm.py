"""LLM clients (Anthropic Messages API, OpenAI Responses API) and a JSON-output helper."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from forge_tasks.errors import TaskError, TransientError


class LLMError(TransientError):
    pass


def _failure(exc: Exception) -> TaskError:
    """A provider error as the pipeline sees it: a request the provider
    rejected is permanent (retrying the same request fails the same way);
    anything else (rate limits, overload, network) is transient."""
    status = getattr(exc, "status_code", None)
    if status in (400, 401, 403, 404, 413):
        err = TaskError(f"LLM request rejected ({status}): {exc}")
        err.permanent = True
        return err
    return LLMError(f"LLM call failed: {exc}")


class AnthropicLLMClient:
    """LLMClient backed by the Anthropic Messages API, with prompt caching."""

    def __init__(
        self,
        *,
        model: str = "claude-haiku-4-5-20251001",
        api_key: str | None = None,
        max_retries: int = 4,
        client: Any = None,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, max_retries=max_retries)
        self._client = client
        self.model_id = model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 200,
        cache_system: bool = True,
        temperature: float | None = None,
    ) -> str:
        block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            resp = await self._client.messages.create(
                model=self.model_id,
                max_tokens=max_tokens,
                system=[block],
                messages=[{"role": "user", "content": user}],
                **kwargs,
            )
        except Exception as exc:
            raise _failure(exc) from exc
        return "".join(getattr(b, "text", "") for b in resp.content)


class OpenAILLMClient:
    """LLMClient backed by the OpenAI Responses API.

    OpenAI caches long prompt prefixes by itself, so ``cache_system`` has no
    effect. ``reasoning_effort`` (e.g. "low") bounds how much of ``max_tokens``
    a reasoning model spends thinking before it answers. Reasoning models
    reject ``temperature``, so with a reasoning effort it is never sent.
    Otherwise the first request that has one finds out, while concurrent ones
    wait for it rather than each being rejected: once the model rejects it, it
    is left out of every later request."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        max_retries: int = 4,
        client: Any = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, max_retries=max_retries)
        self._client = client
        self.model_id = model
        self._reasoning_effort = reasoning_effort
        # None until a request finds out.
        self._temperature_supported: bool | None = False if reasoning_effort else None
        self._temperature_probe = asyncio.Lock()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 200,
        cache_system: bool = True,
        temperature: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        try:
            if temperature is not None and self._temperature_supported is None:
                async with self._temperature_probe:
                    if self._temperature_supported is None:
                        return await self._probe_temperature(system, user, max_tokens, kwargs, temperature)
            if temperature is not None and self._temperature_supported:
                kwargs["temperature"] = temperature
            resp = await self._create(system, user, max_tokens, kwargs)
        except Exception as exc:
            raise _failure(exc) from exc
        text: str = resp.output_text
        return text

    async def _probe_temperature(
        self, system: str, user: str, max_tokens: int, kwargs: dict[str, Any], temperature: float
    ) -> str:
        """Send the first request with a temperature, learning whether the
        model takes one; asked again without it if not."""
        try:
            resp = await self._create(system, user, max_tokens, {**kwargs, "temperature": temperature})
        except Exception as exc:
            if getattr(exc, "param", None) != "temperature":
                raise
            self._temperature_supported = False
            resp = await self._create(system, user, max_tokens, kwargs)
        else:
            self._temperature_supported = True
        text: str = resp.output_text
        return text

    async def _create(self, system: str, user: str, max_tokens: int, kwargs: dict[str, Any]) -> Any:
        return await self._client.responses.create(
            model=self.model_id, instructions=system, input=user, max_output_tokens=max_tokens, **kwargs
        )


class LazyLLMClient:
    """Defers client construction (and its API-key check) to the first call,
    so tasks that may use an LLM can be built without credentials."""

    def __init__(self, factory: Callable[[], Any], *, model_id: str) -> None:
        self._factory = factory
        self._client: Any = None
        self.model_id = model_id

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 200,
        cache_system: bool = True,
        temperature: float | None = None,
    ) -> str:
        if self._client is None:
            self._client = self._factory()
        result: str = await self._client.complete(
            system=system, user=user, max_tokens=max_tokens, cache_system=cache_system, temperature=temperature
        )
        return result


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model reply (tolerates code fences and
    leading prose). Raises ValueError if none is found."""
    cleaned = _FENCE.sub("", text.strip())
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(obj, dict):
        raise ValueError("reply JSON is not an object")
    return obj
