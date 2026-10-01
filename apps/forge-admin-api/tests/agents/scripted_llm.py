"""A scripted model in Gemini's place, and helpers to talk to the /agents routes."""

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx2 as httpx
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

USER = "alice"
BASE = "/api/v1/agents"
SESSIONS = f"{BASE}/apps/forge/users/{USER}/sessions"


class ScriptedLlm(BaseLlm):
    """
    Answers each model call with its next turn: a list of parts, or an
    exception to raise. Streaming, it first sends each text part in two
    halves (partial responses) and then the whole turn, as Gemini does.
    """

    model: str = "gemini-3.5-flash"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        turn = self.turns.pop(0)
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


def delegate(specialist: str, request: str) -> types.Part:
    """The supervisor handing ``request`` to a specialist."""
    return types.Part(
        function_call=types.FunctionCall(name=specialist, args={"request": request})
    )


def finish(result: str) -> types.Part:
    """A specialist reporting back to the supervisor."""
    return types.Part(
        function_call=types.FunctionCall(name="finish_task", args={"result": result})
    )


def sse_events(response: httpx.Response) -> list[dict[str, Any]]:
    """The ADK events of a ``run_sse`` response, in order."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        json.loads(message.removeprefix("data: "))
        for message in response.text.split("\n\n")
        if message.startswith("data: ")
    ]


def run(client: TestClient, session_id: str, *parts: dict[str, Any], **extra: Any):
    """Post a user turn to ``run_sse`` as the web console does."""
    return client.post(
        f"{BASE}/run_sse",
        json={
            "appName": "forge",
            "userId": USER,
            "sessionId": session_id,
            "newMessage": {"role": "user", "parts": list(parts)},
            "streaming": True,
            **extra,
        },
    )
