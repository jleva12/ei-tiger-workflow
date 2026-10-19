"""
Agents called as tools, in a built graph every conversation shares: one
chat's call ending leaves open what another chat's call is using, and the
graph's owner closes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from forge_agent_runtime import AgentExecutor, RunRequest, RuntimeServices, parse_document
from forge_agent_runtime.executor import DocumentAgentSource
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from tests.conftest import PROVIDERS, call, text

AGENT = "ca_desk000001"


class ByAgent(BaseLlm):
    """
    Answers whichever agent asks: the desk calls its researcher, the
    researcher searches the docs, and either answers once its call is.
    """

    model: str = "gpt-test"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        answered = any(
            part.function_response for content in llm_request.contents for part in content.parts or []
        )
        if answered:
            parts = [text("Done.")]
        elif "Research." in str(llm_request.config.system_instruction):
            parts = [call("search_docs", q="refunds")]
        else:
            parts = [call("researcher", request="Look into refunds.")]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


class Docs(BaseToolset):
    """
    An MCP server's toolset: its first two searches each wait for their own
    go-ahead (``go``), and note whether it was still open as they answer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0
        self.started = [asyncio.Event(), asyncio.Event()]
        self.go = [asyncio.Event(), asyncio.Event()]
        self.open_as_they_answered: list[bool] = []

        async def search_docs(q: str) -> dict[str, Any]:
            """Searches the docs."""
            n = sum(started.is_set() for started in self.started)
            self.started[n].set()
            await self.go[n].wait()
            self.open_as_they_answered.append(self.closed == 0)
            return {"found": f"Docs about {q}"}

        self.tool = FunctionTool(search_docs)

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        return [self.tool]

    async def close(self) -> None:
        self.closed += 1


def desk() -> Any:
    """A desk calling a researcher as a tool, which searches an organization's MCP server."""
    return parse_document(
        {
            "format": "forge.chat_agent/v1",
            "id": AGENT,
            "name": "Desk",
            "organization_id": "org_1",
            "version": 1,
            "nodes": [
                {"id": "agent", "kind": "agent", "name": "Desk", "config": {"instruction": "Help."}},
                {
                    "id": "res",
                    "kind": "sub_agent",
                    "name": "Researcher",
                    "config": {"instruction": "Research."},
                },
                {"id": "docs", "kind": "mcp", "name": "Docs", "config": {"server": "srv_docs", "tools": ""}},
            ],
            "edges": [
                {"id": "e1", "source": "agent", "source_output": "tools", "target": "res"},
                {"id": "e2", "source": "res", "source_output": "tools", "target": "docs"},
            ],
        }
    )


def turn(session: str) -> RunRequest:
    return RunRequest(
        app_name=AGENT,
        user_id="ada",
        session_id=session,
        new_message=types.Content(role="user", parts=[text("Refunds?")]),
    )


@pytest.mark.timeout(10)
async def test_one_chats_agent_call_ending_leaves_another_chats_toolsets_open() -> None:
    docs = Docs()
    made: list[Docs] = []

    async def servers(config: Any, *, organization_id: str | None) -> BaseToolset:
        made.append(docs)
        return docs

    models = ProviderModels(parse_model_provider_yaml(PROVIDERS), build=lambda _call: ByAgent())
    executor = AgentExecutor(
        DocumentAgentSource(desk()), models=models, services=RuntimeServices(mcp_servers=servers)
    )
    for session in ("a", "b"):
        await executor.sessions.create_session(app_name=AGENT, user_id="ada", session_id=session)

    async def chat(session: str) -> list[Any]:
        return [event async for event in executor.run(turn(session))]

    try:
        # Both chats' researchers search at once, on the one built graph.
        first = asyncio.create_task(chat("a"))
        await docs.started[0].wait()
        second = asyncio.create_task(chat("b"))
        await docs.started[1].wait()
        # The first chat's turn ends, its call's runner with it, while the second's call goes on.
        docs.go[0].set()
        await first
        docs.go[1].set()
        await second
        assert docs.open_as_they_answered == [True, True]
        assert docs.closed == 0
        assert len(made) == 1
    finally:
        await executor.close()
    # The graph's owner closes it, once.
    assert docs.closed == 1
