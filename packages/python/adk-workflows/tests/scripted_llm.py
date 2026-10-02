"""A scripted model in a real one's place: each call answers with its next turn."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class ScriptedLlm(BaseLlm):
    """
    Answers each model call with its next turn: a list of parts, or an
    exception to raise. Streaming, it first sends each text part in two
    halves (partial responses) and then the whole turn, as Gemini does. It
    takes ``delay`` seconds to answer.
    """

    model: str = "gemini-3.5-flash"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)
    delay: float = 0.0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        turn = self.turns.pop(0)
        await asyncio.sleep(self.delay)
        if isinstance(turn, Exception):
            raise turn
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
