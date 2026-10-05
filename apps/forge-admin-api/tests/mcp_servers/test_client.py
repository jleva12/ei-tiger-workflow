"""Listing a server's tools, against a real MCP server (the SDK's own, over
streamable HTTP on a local port) that wants an API key."""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from forge_admin.mcp_servers.client import Listing, McpConnectionError, list_tools


class RequireKey(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.headers.get("X-API-Key") != "sk-123":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    """The server's MCP endpoint."""
    weather = MCPServer("Weather", version="1.2.3")

    @weather.tool()
    def forecast(city: str) -> str:
        """Tomorrow's weather in a city."""
        return f"Sunny in {city}"

    @weather.tool(title="Alerts")
    def alerts(region: str) -> list[str]:
        """Weather alerts for a region."""
        return []

    app = weather.streamable_http_app()
    app.add_middleware(RequireKey)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    for _ in range(100):
        if running.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    running.should_exit = True
    thread.join(timeout=5)


def listed(url: str, **headers: str) -> Listing:
    return asyncio.run(list_tools(url, headers=headers, seconds=5))


def test_its_tools_and_what_it_is_are_listed(server: str) -> None:
    listing = listed(server, **{"X-API-Key": "sk-123"})
    assert listing.server_info == {"name": "Weather", "version": "1.2.3"}
    assert sorted(listing.tools, key=lambda tool: tool["name"]) == [
        {
            "name": "alerts",
            "title": "Alerts",
            "description": "Weather alerts for a region.",
        },
        {
            "name": "forecast",
            "title": None,
            "description": "Tomorrow's weather in a city.",
        },
    ]


def test_refused_credentials_are_a_401(server: str) -> None:
    with pytest.raises(
        McpConnectionError, match="401: it refused the credentials"
    ) as caught:
        listed(server, **{"X-API-Key": "wrong"})
    assert caught.value.status == 401


def test_a_url_without_mcp_is_a_404(server: str) -> None:
    with pytest.raises(McpConnectionError, match="404: there's no MCP endpoint"):
        listed(server.replace("/mcp", "/nope"), **{"X-API-Key": "sk-123"})


def test_a_server_that_isnt_there_cant_be_reached() -> None:
    with pytest.raises(McpConnectionError, match="can't be reached"):
        listed("http://127.0.0.1:1/mcp")
