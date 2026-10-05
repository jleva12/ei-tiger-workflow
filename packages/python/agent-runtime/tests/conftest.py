"""A scripted model in a real one's place, and the builder's example agent."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from forge_common.adk.models import ModelCall, ProviderModels
from forge_common.model_provider import parse_model_provider_yaml

FIXTURES = Path(__file__).with_name("fixtures")
EXAMPLE = FIXTURES / "support-assistant.chat-agent.json"

PROVIDERS = """
version: 1
default: {provider: openai, model: gpt-test}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: sk-test}
    models: [{id: gpt-test, reasoning: true}, {id: gpt-other}]
"""

BILLING_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Billing", "version": "1"},
    "servers": [{"url": "https://api.example.com/billing"}],
    "paths": {
        "/invoices/{id}": {
            "get": {
                "operationId": "getInvoice",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/charges": {"get": {"operationId": "listCharges", "responses": {"200": {"description": "ok"}}}},
        "/refunds": {"post": {"operationId": "previewRefund", "responses": {"200": {"description": "ok"}}}},
    },
}


class ScriptedLlm(BaseLlm):
    """Answers each model call with its next turn (a list of parts, or an exception to raise)."""

    model: str = "gpt-test"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
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
                            content=types.Content(role="model", parts=[types.Part(text=chunk)]), partial=True
                        )
        yield LlmResponse(content=types.Content(role="model", parts=turn))


def text(value: str) -> types.Part:
    return types.Part(text=value)


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
def calls() -> list[ModelCall]:
    return []


@pytest.fixture
def models(llm: ScriptedLlm, calls: list[ModelCall]) -> ProviderModels:
    def build(call: ModelCall) -> BaseLlm:
        calls.append(call)
        return llm

    return ProviderModels(parse_model_provider_yaml(PROVIDERS), build=build)


@pytest.fixture
def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text())


def without(doc: dict[str, Any], *node_ids: str) -> dict[str, Any]:
    """The document without some nodes and their edges."""
    doc = copy.deepcopy(doc)
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] not in node_ids]
    doc["edges"] = [e for e in doc["edges"] if e["source"] not in node_ids and e["target"] not in node_ids]
    return doc


#: The example's nodes left out for a plain chat agent (the server tests' agent).
PLAIN = ("memory", "orders", "help", "billing", "billing_api")


@pytest.fixture
def agent_file(tmp_path: Path, example: dict[str, Any]) -> Path:
    """The example as a plain chat agent, in a file; its instruction reads the request."""
    doc = without(example, *PLAIN)
    doc["nodes"][0]["config"]["instruction"] = "Help {{ request.userId }}."
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def web() -> Callable[..., httpx.AsyncClient]:
    """An HTTP client answered by a handler instead of the network; serves the billing spec."""

    def make(handler: Callable[[httpx.Request], httpx.Response] | None = None) -> httpx.AsyncClient:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/openapi.json"):
                return httpx.Response(200, json=BILLING_SPEC)
            if handler is not None:
                return handler(request)
            return httpx.Response(404)

        return httpx.AsyncClient(transport=httpx.MockTransport(answer))

    return make
