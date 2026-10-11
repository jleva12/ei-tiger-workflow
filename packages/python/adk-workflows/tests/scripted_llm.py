"""A scripted model in a real one's place: each call answers with its next turn."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


def blocked_for_safety() -> LlmResponse:
    """A Gemini answer stopped for safety, as ADK's Gemini model makes it: an
    answer with an ``error_code``, not an exception."""
    stopped = types.Candidate(finish_reason=types.FinishReason.SAFETY, finish_message="Blocked for safety")
    return LlmResponse.create(types.GenerateContentResponse(candidates=[stopped]))


class ScriptedLlm(BaseLlm):
    """
    Answers each model call with its next turn: a list of parts, an
    ``LlmResponse`` as it is (an error), or an exception to raise. Streaming,
    it first sends each text part in two halves (partial responses) and then
    the whole turn, as Gemini does. It takes ``delay`` seconds to answer.

    Steps side by side ask in either order: ``routed`` keeps turns for the
    requests whose system instruction holds their key, each used once, before
    ``turns``.
    """

    model: str = "gemini-3.5-flash"
    turns: list[Any] = Field(default_factory=list)
    routed: dict[str, list[Any]] = Field(default_factory=dict)
    requests: list[LlmRequest] = Field(default_factory=list)
    delay: float = 0.0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        instruction = str(llm_request.config.system_instruction or "") if llm_request.config else ""
        key = next((key for key, turns in self.routed.items() if turns and key in instruction), None)
        turn = self.routed[key].pop(0) if key is not None else self.turns.pop(0)
        await asyncio.sleep(self.delay)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, LlmResponse):
            yield turn
            return
        if stream:
            for part in turn:
                if part.text:
                    middle = len(part.text) // 2
                    for chunk in (part.text[:middle], part.text[middle:]):
                        yield LlmResponse(
                            content=types.Content(
                                role="model",
                                parts=[types.Part(text=chunk, thought=part.thought)],
                            ),
                            partial=True,
                        )
        yield LlmResponse(content=types.Content(role="model", parts=turn))
