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
    from google.adk.code_executors.base_code_executor import BaseCodeExecutor
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
        :param workflow_id: The workflow and which of its versions (``ag_x``,
            its latest published; ``ag_x@3``; ``ag_x@draft``).
        :return: The workflow's name, what it does, and the JSON Schema of the
            input its start takes.
        :raises LookupError: There's no such workflow, or version.
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
        :return: The passages of these knowledge bases that best match the
            query, ranked together, best first: each ``{"ref",
            "knowledge_base", "knowledge_base_id", "document", "document_id",
            "section", "location", "text", "score"}``. ``ref`` is what an
            answer cites it by ("[KQM4821]"), the same in every search;
            ``knowledge_base`` the name of the one it's from; ``location``
            where in the document, in words ("page 4"); ``score`` how
            relevant it is (higher is better). Empty when nothing is relevant
            enough: the tool then says so, rather than handing the model the
            nearest of what's there.
        :raises LookupError: The organization has no such knowledge base.
        """
        ...

    async def design(
        self,
        knowledge_base_ids: Sequence[str],
        *,
        organization_id: str | None,
        focus: str = "",
    ) -> dict[str, Any] | None:
        """
        :param focus: An application's, component's or group's name: only
            that part, what it connects to and how; everything when empty.
        :return: What the system design knowledge bases among these say of
            their systems (``forge_codegraph.design_view``): ``{"systems":
            [...]}``, each with its groups, applications, components (its
            databases, queues, external systems…) and how they all connect;
            and a ``note`` when the focus picks nothing. None when none of
            them is a system design knowledge base.
        :raises LookupError: The organization has no such knowledge base.
        """
        ...


@dataclass(frozen=True)
class RequestAuth:
    """What a credential has a request send: headers, and query parameters."""

    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, str] = field(default_factory=dict)


class CredentialProvider(Protocol):
    """Signs requests with the organization's API credentials, as an HTTP or
    OpenAPI tool names one (its ``credential``): each sent only to the URLs
    it was given for."""

    async def __call__(self, credential_id: str, *, organization_id: str | None, url: str) -> RequestAuth:
        """
        :return: What a request to ``url`` sends through the credential.
        :raises LookupError: There's no such credential, it may not be sent
            to ``url``, nobody signed in with it, or what it sends can't be
            had; the error says which.
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


class ToolValues(Protocol):
    """Fills in the ``{{ expression }}`` parts of an HTTP tool's URL and body
    as it's called, from what the run has: a workflow's data, where its
    agent runs in one."""

    async def __call__(self, expression: str, context: ToolContext | None, *, what: str) -> Any:
        """
        :param expression: The part's JSONata.
        :param context: The call's: its session's state.
        :param what: What's being filled in, for errors.
        :return: What it gives, as plain JSON.
        :raises ToolFailure: It failed, saying why.
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
    :ivar credentials: Signs HTTP and OpenAPI tools' requests with the
        organization's credentials; without it, a tool naming one doesn't build.
    :ivar environment_note: What a ``${NAME}`` the environment doesn't have
        adds to its error: where a deployment's names are listed.
    :ivar tool_values: Fills in ``{{ }}`` in HTTP tools' URLs and bodies;
        without it, from the chat's request and state, as instructions are.
    :ivar ask_person: Agents whose ``ask_person`` setting is on may ask the
        person mid-turn (``forge_common.adk.ask_person``); off where nobody is
        there to answer, e.g. in a workflow's run.
    :ivar code_executor: Runs the code agents' Code execution tools are sent
        (``forge_agent_runtime.code_execution``); without it, agents running
        code don't build.
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
    credentials: CredentialProvider | None = None
    environment_note: str = ""
    tool_values: ToolValues | None = None
    ask_person: bool = True
    code_executor: BaseCodeExecutor | None = None

    def but(self, **changes: Any) -> RuntimeServices:
        """:return: These services with some changed."""
        return replace(self, **changes)
