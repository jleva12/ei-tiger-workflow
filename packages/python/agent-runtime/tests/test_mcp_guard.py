"""MCP tools connect as HTTP tools' requests do (forge_agent_runtime.mcp_guard):
the MCP client's httpx2 and httpcore2 resolve, check and pin every address,
whether the runtime makes the toolset (a server by URL) or the hosted
runtime's MCP servers do (one of the organization's); and ADK's mTLS probe,
whose client would check nothing, is off."""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpcore2
import pytest
import uvicorn
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, StreamableHTTPConnectionParams
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from forge_agent_runtime.build import BuildError, build_tools
from forge_agent_runtime.http_guard import NetworkPolicy
from forge_agent_runtime.mcp_guard import (
    GuardedMcpClient,
    McpGuardError,
    check_server_url,
    guarded_mcp_toolset,
    mcp_client_factory,
)
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools, http_client
from forge_common.adk import ToolFailure


def dns_rows(address: str, port: int | None) -> list[tuple[Any, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 443))]


def resolving(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    """Names resolve to each address in turn (the last from then on): :return: The names looked up."""
    asked: list[str] = []

    async def resolve(host: str, port: int | None, **kwargs: Any) -> list[tuple[Any, ...]]:
        asked.append(host)
        return dns_rows(addresses[min(len(asked), len(addresses)) - 1], port)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    return asked


def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A refused address reached a socket")

    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", forbidden)


class RecordedStream(httpcore2.AsyncNetworkStream):
    def __init__(self, written: list[bytes], tls: list[str | None], response: bytes) -> None:
        self.written, self.tls, self.response = written, tls, response

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109 - httpcore protocol
        result, self.response = self.response, b""
        return result

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109 - httpcore protocol
        self.written.append(buffer)

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore protocol
    ) -> "RecordedStream":
        self.tls.append(server_hostname)
        return self

    async def aclose(self) -> None:
        pass


async def mcp_toolset(config: dict[str, Any], services: RuntimeServices) -> McpToolset:
    [group] = await build_tools(
        "Help center", "mcp", config, models=None, http=http_client(), services=services
    )
    assert isinstance(group, GuardedTools)
    toolset = group.items[0]
    assert isinstance(toolset, McpToolset)
    return toolset


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/mcp",
        "http://100.100.100.200/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "https://intranet.example.com/mcp",
    ],
)
async def test_an_mcp_server_by_url_on_the_runtimes_own_network_doesnt_build(monkeypatch, url):
    resolving(monkeypatch, "10.0.0.5")
    with pytest.raises(BuildError, match="private network"):
        await mcp_toolset({"url": url}, RuntimeServices())


async def test_an_mcp_server_by_url_is_checked_as_it_connects_not_only_as_its_built(monkeypatch):
    # Public as it's built, the runtime's own as it connects: a name that rebinds.
    asked = resolving(monkeypatch, "8.8.8.8", "127.0.0.1")
    no_sockets(monkeypatch)
    toolset = await mcp_toolset({"url": "https://mcp.example.com/mcp"}, RuntimeServices())
    with pytest.raises(ConnectionError, match="private network"):
        await toolset.get_tools()
    assert len(asked) >= 2


async def test_the_organizations_mcp_servers_connect_through_the_guard_too(monkeypatch):
    made: list[McpToolset] = []

    async def organization_servers(config: Any, *, organization_id: str | None) -> BaseToolset:
        # As forge_mcp_servers makes one: ADK's own connection settings.
        made.append(
            McpToolset(connection_params=StreamableHTTPConnectionParams(url="https://mcp.example.com/mcp"))
        )
        return made[0]

    resolving(monkeypatch, "100.100.100.200")
    no_sockets(monkeypatch)
    toolset = await mcp_toolset({"server": "mcp_1"}, RuntimeServices(mcp_servers=organization_servers))
    assert toolset is made[0]
    with pytest.raises(ConnectionError, match="private network"):
        await toolset.get_tools()
    # ADK's mTLS probe would connect with Google's session instead, checking nothing.
    assert await toolset._mcp_session_manager._get_mtls_transport() is None


async def test_a_deployment_that_allows_a_host_or_private_networks_reaches_its_mcp_servers():
    for services in (RuntimeServices(allow_private=True), RuntimeServices(allowed_hosts=("10.0.0.5",))):
        toolset = await mcp_toolset({"url": "http://10.0.0.5/mcp"}, services)
        params = toolset._mcp_session_manager._connection_params
        assert isinstance(params, StreamableHTTPConnectionParams)
        client = params.httpx_client_factory()
        assert isinstance(client, GuardedMcpClient)
        assert client._policy == NetworkPolicy(services.allow_private, services.allowed_hosts)


async def test_the_mcp_client_connects_to_the_address_it_checked_and_follows_no_redirect(monkeypatch):
    resolving(monkeypatch, "8.8.8.8")
    connected: list[tuple[str, int]] = []
    written: list[bytes] = []
    tls: list[str | None] = []
    moved = (
        b"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://169.254.169.254/\r\nContent-Length: 0\r\n\r\n"
    )

    async def connect(self: Any, host: str, port: int, **kwargs: Any) -> RecordedStream:
        connected.append((host, port))
        return RecordedStream(written, tls, moved)

    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", connect)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    async with mcp_client_factory(NetworkPolicy())() as client:
        response = await client.post("https://mcp.example.com/mcp", json={})
    assert response.status_code == 307
    assert connected == [("8.8.8.8", 443)]
    assert tls == ["mcp.example.com"]
    assert b"Host: mcp.example.com\r\n" in b"".join(written)


async def test_mcp_proxy_routes_fail_closed(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9123")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    async with mcp_client_factory(NetworkPolicy())() as client:
        with pytest.raises(ToolFailure, match="proxy routes"):
            await client.get("https://8.8.8.8/mcp")


async def test_a_server_url_that_cant_be_found_yet_is_left_to_its_connection(monkeypatch):
    async def unknown(host: str, port: int | None, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", unknown)
    assert (
        await check_server_url("https://new.example.com/mcp", NetworkPolicy())
        == "https://new.example.com/mcp"
    )
    with pytest.raises(ToolFailure, match="http:// or https://"):
        await check_server_url("ftp://new.example.com/mcp", NetworkPolicy())


def test_an_mcp_toolset_that_isnt_over_http_cant_be_guarded():
    stdio = McpToolset(
        connection_params=StdioConnectionParams(server_params=StdioServerParameters(command="true"))
    )
    with pytest.raises(McpGuardError):
        guarded_mcp_toolset(stdio, NetworkPolicy())


@pytest.fixture(scope="module")
def weather() -> Iterator[str]:
    """A real MCP server (the SDK's own, over streamable HTTP) on a local port: its endpoint."""
    server = MCPServer("Weather", version="1.0")

    @server.tool()
    def forecast(city: str) -> str:
        """Tomorrow's weather in a city."""
        return f"Sunny in {city}"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), port=port, log_level="warning"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    for _ in range(100):
        if running.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    running.should_exit = True
    thread.join(timeout=5)


async def test_an_allowed_mcp_server_is_listed_and_called_through_the_pinned_connection(weather):
    toolset = await mcp_toolset({"url": weather}, RuntimeServices(allowed_hosts=("127.0.0.1",)))
    try:
        [tool] = await toolset.get_tools()
        assert tool.name == "forecast"
        answered = await tool.run_async(args={"city": "Oslo"}, tool_context=None)
        assert "Sunny in Oslo" in str(answered)
    finally:
        await toolset.close()
    # Where it isn't allowed, the runtime's own machine is refused.
    with pytest.raises(BuildError, match="private network"):
        await mcp_toolset({"url": weather}, RuntimeServices())
