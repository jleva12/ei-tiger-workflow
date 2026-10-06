"""An organization's MCP server as a Google ADK toolset, for the hosted
runtime's agents: ``forge_mcp_servers.mcp_toolset`` over the servers as this
API keeps them (:class:`OrmServerStore`)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from forge_mcp_servers import McpServerGone, McpServers
from forge_mcp_servers import mcp_toolset as shared_toolset
from forge_mcp_servers.toolset import tool_names
from google.adk.tools.mcp_tool import McpToolset
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.models import McpServer

__all__ = ["McpServerGone", "OrmServerStore", "mcp_toolset", "tool_names"]


class OrmServerStore:
    """Servers as this API keeps them: a row locked while open, committed as it closes."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @asynccontextmanager
    async def open(self, server_id: str) -> AsyncIterator[McpServer | None]:
        async with self.sessions() as session:
            server = await session.get(McpServer, server_id, with_for_update=True)
            yield server
            await session.commit()


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
    :raises McpServerGone: The organization has no such server.
    """
    return await shared_toolset(
        servers,
        OrmServerStore(sessions),
        organization_id=organization_id,
        config=config,
    )
