"""An organization's MCP server as a Google ADK toolset, for its agents.

A chat agent's MCP tool names a server by ID (``config.server``), the tools
it may call (``config.tools``, comma-separated; empty for all) and whether a
person confirms each call (``config.confirm``). :func:`mcp_toolset` makes
that an ``McpToolset`` whose headers are worked out before each connection,
from the server as it's saved then: a token is refreshed as it runs out,
and the renewed grant saved.
"""

from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.mcp_servers.service import McpServers
from forge_admin.models import McpServer


class McpServerGone(LookupError):
    """The organization has no such MCP server."""


def tool_names(tools: str) -> list[str] | None:
    """:return: The names in a comma-separated list; None (every tool) for none."""
    names = [name.strip() for name in tools.split(",") if name.strip()]
    return names or None


async def mcp_toolset(
    servers: McpServers,
    sessions: async_sessionmaker[AsyncSession],
    *,
    organization_id: str,
    config: dict[str, Any],
) -> McpToolset:
    """
    :param servers: The MCP servers' service.
    :param sessions: Opens a session on the admin MySQL.
    :param organization_id: The agent's organization: the server must be its.
    :param config: The agent's MCP tool: ``server``, ``tools``, ``confirm``.
    :return: The toolset.
    :raises McpServerGone: The organization has no such server.
    """
    server_id = config.get("server") or ""
    async with sessions() as session:
        server = await session.get(McpServer, server_id)
        if server is None or server.organization_id != organization_id:
            raise McpServerGone(f"The organization has no MCP server {server_id!r}")
        url, timeout = server.url, server.timeout_seconds

    async def headers(_context: ReadonlyContext) -> dict[str, str]:
        async with sessions() as session:
            current = await session.get(McpServer, server_id)
            if current is None:
                raise McpServerGone(f"The MCP server {server_id!r} was deleted")
            sent = await servers.headers(current)
            # A renewed grant.
            await session.commit()
            return sent

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, timeout=timeout),
        tool_filter=tool_names(str(config.get("tools") or "")),
        require_confirmation=bool(config.get("confirm")),
        header_provider=headers,
    )
