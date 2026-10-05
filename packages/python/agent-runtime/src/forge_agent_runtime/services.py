"""What building and running an agent may use, beyond its document.

A deployment fills in what it has: the hosted runtime resolves saved agents
from its store, runs workflows on its worker and connects to its
organizations' MCP servers; a standalone one usually has none of these, and
an agent that needs one fails to build, saying which.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from google.adk.tools.base_toolset import BaseToolset
    from google.adk.tools.tool_context import ToolContext

    from forge_agent_runtime.document import ChatAgentDocument


class AgentResolver(Protocol):
    """Finds the saved agents a chat agent uses (``saved_agent`` nodes)."""

    async def __call__(self, agent_id: str) -> ChatAgentDocument:
        """
        :return: The agent to use: its latest published version.
        :raises LookupError: There's no such agent, or it has none published.
        """
        ...


class WorkflowRunner(Protocol):
    """Runs the workflows a chat agent calls as tools (``adk_workflow`` nodes)."""

    async def describe(
        self, workflow_id: str, *, organization_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        """
        :return: The workflow's name, what it does, and the JSON Schema of the
            input its start takes.
        :raises LookupError: There's no such workflow.
        """
        ...

    async def run(
        self,
        workflow_id: str,
        workflow_input: dict[str, Any],
        *,
        organization_id: str | None,
        context: ToolContext,
    ) -> Any:
        """:return: What the run answered, or where it got to (paused, still running)."""
        ...


class KnowledgeBases(Protocol):
    """Searches the organization's knowledge bases an agent names by ID (a
    ``knowledge_base`` node's ``knowledge_bases``)."""

    async def describe(
        self, knowledge_base_ids: Sequence[str], *, organization_id: str | None
    ) -> list[tuple[str, str]]:
        """
        :return: Each knowledge base's name and what it holds, in order.
        :raises LookupError: The organization has no such knowledge base.
        """
        ...

    async def search(
        self,
        knowledge_base_ids: Sequence[str],
        query: str,
        *,
        organization_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        :return: The passages that best match the query, best first: each
            ``{"document", "section", "text", "score"}``.
        :raises LookupError: The organization has no such knowledge base.
        """
        ...


class McpServers(Protocol):
    """Connects to the MCP servers an agent names by ID (an ``mcp`` node's ``server``)."""

    async def __call__(self, config: Mapping[str, Any], *, organization_id: str | None) -> BaseToolset:
        """
        :return: The server's toolset, filtered and confirmed as ``config`` says.
        :raises LookupError: There's no such server.
        """
        ...


@dataclass(frozen=True)
class RuntimeServices:
    """
    :ivar environment: What ``${NAME}`` in tools' URLs and headers is filled
        in from; the process environment by default. Exported agents carry no
        secrets: they name them.
    :ivar allow_private: HTTP tools may reach private networks (local runs).
    :ivar allowed_hosts: Hosts HTTP tools may always reach.
    :ivar max_response_bytes: The most an HTTP tool reads of a response.
    :ivar http_timeout: Seconds an HTTP tool's request may take.
    :ivar tool_timeout: Seconds any tool call may take; None for no limit.
    :ivar model_timeout: Seconds one model call may take; None leaves the provider's.
    :ivar resolve_agent: Finds saved agents; without it, only those in a
        document's ``dependencies`` are found.
    :ivar workflows: Runs workflows; without it, agents calling one don't build.
    :ivar mcp_servers: Connects to MCP servers by ID; without it, an MCP tool
        must say its URL itself.
    :ivar knowledge_bases: Searches knowledge bases by ID; without it, agents
        searching one don't build.
    """

    environment: Mapping[str, str] = field(default_factory=lambda: os.environ)
    allow_private: bool = False
    allowed_hosts: tuple[str, ...] = ()
    max_response_bytes: int = 2_000_000
    http_timeout: float = 30.0
    tool_timeout: float | None = 120.0
    model_timeout: float | None = None
    resolve_agent: AgentResolver | None = None
    workflows: WorkflowRunner | None = None
    mcp_servers: McpServers | None = None
    knowledge_bases: KnowledgeBases | None = None

    def but(self, **changes: Any) -> RuntimeServices:
        """:return: These services with some changed."""
        return replace(self, **changes)
