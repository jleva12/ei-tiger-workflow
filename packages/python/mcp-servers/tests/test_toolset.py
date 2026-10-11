"""An organization's MCP server as an ADK toolset, against a real MCP server
(the SDK's low-level one, over streamable HTTP on a local port) whose tool's
output schema has a runaway pattern: the call is answered in time, as the
tool's error, and the rest of the process goes on meanwhile, its results
checked on the bounded engine (forge_common.mcp) rather than by the client.

A regression to the client's own check, on Python's re, would hang the test
rather than fail it, so it's held to a timeout (pytest-timeout's signal, which
re answers)."""

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn
from google.adk.events.event_actions import EventActions
from mcp import types
from mcp.server import Server

from forge_mcp_servers import McpServers, SecretBox, mcp_toolset

RUNAWAY = "^(a|aa)+$"
LONG = "a" * 60 + "b"


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    """The server's MCP endpoint. Its one tool, lookup, says its result's code
    matches a runaway pattern, and answers with one that re would take for
    ever to tell doesn't."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        code = {"type": "string", "pattern": RUNAWAY}
        tool = types.Tool(
            name="lookup",
            input_schema={"type": "object"},
            output_schema={"type": "object", "properties": {"code": code}},
        )
        return types.ListToolsResult(tools=[tool])

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=LONG)],
            structured_content={"code": LONG},
        )

    lookup = Server("Lookup", on_list_tools=list_tools, on_call_tool=call_tool)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(
        lookup.streamable_http_app(), port=port, log_level="warning"
    )
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    for _ in range(100):
        if running.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    running.should_exit = True
    thread.join(timeout=5)


@dataclass
class Row:
    """A server as it's kept, signed in to with nothing."""

    url: str
    id: str = "srv_1"
    organization_id: str = "org_1"
    headers: list[dict[str, str]] = field(default_factory=list)
    timeout_seconds: float = 5.0
    auth_kind: str = "none"
    auth_settings: dict[str, Any] = field(default_factory=dict)
    auth_secrets: str | None = None
    auth_grant: str | None = None
    status: str = "ok"
    last_error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    server_info: dict[str, Any] | None = None
    checked_at: datetime | None = None


class Store:
    """Keeps one server."""

    def __init__(self, row: Row) -> None:
        self.row = row

    @asynccontextmanager
    async def open(self, server_id: str) -> AsyncIterator[Row | None]:
        yield self.row if server_id == self.row.id else None


@pytest.mark.timeout(10)
async def test_a_runaway_output_schema_is_answered_in_time_as_the_tools_error(
    server: str,
) -> None:
    toolset = await mcp_toolset(
        McpServers(SecretBox("k" * 40)),
        Store(Row(url=server)),
        organization_id="org_1",
        config={"server": "srv_1"},
    )
    ticks: list[float] = []
    stop = threading.Event()

    def ticking() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            time.sleep(0.002)

    try:
        (lookup,) = await toolset.get_tools()
        thread = threading.Thread(target=ticking)
        thread.start()
        try:
            began = time.perf_counter()
            answer = await lookup.run_async(
                args={}, tool_context=MagicMock(actions=EventActions())
            )
            took = time.perf_counter() - began
        finally:
            stop.set()
            thread.join()
    finally:
        await toolset.close()

    assert took < 1.0
    assert answer["isError"] is True and "structuredContent" not in answer
    assert answer["content"] == [
        {
            "type": "text",
            "text": "The result of the MCP tool 'lookup' was refused: it doesn't fit "
            "its output schema: its regular expression /^(a|aa)+$/ took longer than "
            "the 500 ms a value's regular expressions get (at $.code)",
        }
    ]
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 20 and max(gaps) < 0.1
