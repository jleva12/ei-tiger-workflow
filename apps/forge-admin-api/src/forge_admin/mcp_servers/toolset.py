"""An organization's MCP server as a Google ADK toolset, for the hosted
runtime's agents: ``forge_mcp_servers.mcp_toolset`` over the servers as this
API keeps them (:class:`OrmServerStore`).

Every connection to a server, and to its authorization server, is held to
:func:`network_policy`, as chat agents' HTTP tools are: never this API's own
network unless ``agent_tools_allow_private`` or ``agent_tools_allowed_hosts``
allows it. The runtime holds an agent's toolset to it as it builds the agent
(``forge_agent_runtime.mcp_guard``); checking a server and signing in to it
go through that module's clients (``McpServers(client_factory=...)``).
"""

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from forge_agent_runtime.http_guard import NetworkPolicy
from forge_mcp_servers import McpServerGone, McpServers
from forge_mcp_servers import mcp_toolset as shared_toolset
from forge_mcp_servers.toolset import tool_names
from google.adk.tools.mcp_tool import McpToolset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.config import Settings
from forge_admin.models import McpServer

__all__ = [
    "McpServerGone",
    "McpServerStamps",
    "OrmServerStore",
    "mcp_toolset",
    "network_policy",
    "tool_names",
]


def network_policy(settings: Settings) -> NetworkPolicy:
    """:return: Where the organizations' MCP servers may be: where chat agents' tools may reach."""
    return NetworkPolicy(
        settings.agent_tools_allow_private,
        tuple(sorted(host.lower() for host in settings.agent_tools_allowed_hosts)),
    )


class McpServerStamps:
    """Connection settings of an agent's servers, for its compiled runner cache."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(
        self, organization_id: str | None, document: Mapping[str, Any]
    ) -> str:
        ids = {
            str(config["server"])
            for node in document.get("nodes") or []
            if isinstance(node, Mapping)
            and node.get("kind") == "mcp"
            and isinstance(config := node.get("config"), Mapping)
            and config.get("server")
        }
        if not ids or organization_id is None:
            return ""
        async with self.sessions() as session:
            rows = await session.execute(
                select(McpServer.id, McpServer.url, McpServer.timeout_seconds)
                .where(
                    McpServer.organization_id == organization_id, McpServer.id.in_(ids)
                )
                .order_by(McpServer.id)
            )
        # Actual values avoid timestamp precision collisions, and an absent
        # row changes the stamp too. Tokens refresh independently of the
        # connection settings and must not trigger rebuilds on every turn.
        encoded = json.dumps([tuple(row) for row in rows], separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


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
