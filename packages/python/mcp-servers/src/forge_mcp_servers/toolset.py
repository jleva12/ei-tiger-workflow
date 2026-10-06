"""An organization's MCP server as a Google ADK toolset, for its agents.

An agent's MCP tool names a server by ID (``config.server``), the tools it
may call (``config.tools``, comma-separated; empty for all) and whether a
person confirms each call (``config.confirm``). :func:`mcp_toolset` makes
that an ``McpToolset`` whose headers are worked out before each connection,
from the server as it's kept then (:class:`ServerStore`): a token is
refreshed as it runs out, and the renewed grant kept, under a lock on its
row, so two processes never spend one refresh token.
"""

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

from forge_mcp_servers.service import McpServers, ServerRow


class McpServerGone(LookupError):
    """The organization has no such MCP server."""


class ServerStore(Protocol):
    """Where servers are kept: the admin's models, or :class:`.store.SqlServerStore`."""

    def open(self, server_id: str) -> AbstractAsyncContextManager[ServerRow | None]:
        """
        :return: The server's row (None when there's none), locked against
            other processes until the block ends; what the block changed on
            it is kept then.
        """
        ...


def tool_names(tools: str) -> list[str] | None:
    """:return: The names in a comma-separated list; None (every tool) for none."""
    names = [name.strip() for name in tools.split(",") if name.strip()]
    return names or None


async def mcp_toolset(
    servers: McpServers,
    store: ServerStore,
    *,
    organization_id: str,
    config: dict[str, Any],
) -> McpToolset:
    """
    :param servers: The MCP servers' service.
    :param store: Where the servers are kept.
    :param organization_id: The agent's organization: the server must be its.
    :param config: The agent's MCP tool: ``server``, ``tools``, ``confirm``.
    :return: The toolset.
    :raises McpServerGone: The organization has no such server.
    """
    server_id = str(config.get("server") or "")
    async with store.open(server_id) as server:
        if server is None or server.organization_id != organization_id:
            raise McpServerGone(f"The organization has no MCP server {server_id!r}")
        url, timeout = server.url, server.timeout_seconds

    async def headers(_context: ReadonlyContext) -> dict[str, str]:
        async with store.open(server_id) as current:
            if current is None:
                raise McpServerGone(f"The MCP server {server_id!r} was deleted")
            return await servers.headers(current)

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, timeout=timeout),
        tool_filter=tool_names(str(config.get("tools") or "")),
        require_confirmation=bool(config.get("confirm")),
        header_provider=headers,
    )
