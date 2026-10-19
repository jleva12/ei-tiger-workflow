"""What building and running an agent may use, beyond its document.

A deployment fills in what it has: the hosted runtime resolves saved agents
from its store, runs workflows on its worker and connects to its
organizations' MCP servers; a standalone one usually has none of these, and
an agent that needs one fails to build, saying which.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.code_executors.base_code_executor import BaseCodeExecutor
    from google.adk.tools.base_toolset import BaseToolset
    from google.adk.tools.tool_context import ToolContext

    from forge_agent_runtime.access import AccessCheck
    from forge_agent_runtime.document import ChatAgentDocument
    from forge_agent_runtime.generative_ui.library import UiComponentLibrary
    from forge_agent_runtime.skills import SkillLibrary


#: Seconds a workflow tool's call may take past its wait: to start the run,
#: and to answer where it got to. So a slow run answers "still running" with
#: its ID, rather than the call failing as too slow.
WORKFLOW_WAIT_MARGIN = 30.0


#: What starts a workflow run, when it isn't an agent's Workflow tool: ``intake``
#: (an intake completing starts its ``on_complete`` workflows).
INTAKE = "intake"

_starting: ContextVar[str | None] = ContextVar("forge_workflow_starter", default=None)


@contextlib.contextmanager
def starting_workflows(starter: str) -> Iterator[None]:
    """Runs a block whose workflow runs ``starter`` starts (e.g. :data:`INTAKE`),
    for the runner to record (:func:`workflow_starter`)."""
    token = _starting.set(starter)
    try:
        yield
    finally:
        _starting.reset(token)


def workflow_starter() -> str | None:
    """:return: What starts the workflow runs started here, when it isn't an
    agent's Workflow tool (:func:`starting_workflows`)."""
    return _starting.get()


class AgentResolver(Protocol):
    """Finds the saved agents a chat agent uses (``saved_agent`` nodes)."""

    async def __call__(self, agent_id: str) -> ChatAgentDocument:
        """
        :return: The agent to use: its latest published version.
        :raises LookupError: There's no such agent, or it has none published.
        """
        ...


@dataclass(frozen=True)
class WorkflowStart:
    """
    A workflow run to start once the change that asks for it is kept: an
    intake completing (its record), a request submitted.

    :ivar call_id: What a chat follows the run by, in place of a model's
        call ID: made from the record or request and the workflow, so it's
        the same however often the start is asked for (and so is the run).
    :ivar ref: The workflow and which of its versions (``ag_x``, ``ag_x@3``, ``ag_x@draft``).
    :ivar name: The workflow's name.
    :ivar input: What its start takes.
    """

    call_id: str
    ref: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class StartSource:
    """
    The change starts wait for: what tells whether it was kept, so starts
    written before it are started only once it is.

    :ivar kind: ``record`` (an intake's record, kept in the conversation's
        state under ``state_key``) or ``request`` (a request, in the
        :class:`RequestStore`).
    :ivar id: The record's or request's ID.
    """

    kind: str
    id: str
    state_key: str | None = None


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
        wait: float | None = None,
    ) -> Any:
        """
        :param wait: Seconds to wait for the run before answering where it got
            to; None for the runner's own default.
        :return: What the run answered, or where it got to (paused, still running).
        """
        ...

    async def intend(
        self,
        starts: Sequence[WorkflowStart],
        *,
        organization_id: str | None,
        context: CallbackContext,
        source: StartSource,
    ) -> list[str]:
        """
        Writes down runs to start once ``source``'s change is kept (the
        start's outbox): nothing starts yet. A start written down whose
        change is never kept is dropped; one whose change is kept is started
        by :meth:`dispatch`, or, should that not happen (the process
        stopped), by the runner's own upkeep.

        :param context: The call or callback whose turn asks for them: who
            they're for, as the conversation's.
        :return: Each run's ID, in order: the same however often they're asked for.
        """
        ...

    async def dispatch(self, run_id: str, *, organization_id: str | None, wait: float) -> dict[str, Any]:
        """
        Starts a run written down by :meth:`intend`, once its change is kept:
        once, however often it's asked (a run already started is the run).

        :param wait: Seconds to wait for it before answering where it got to.
        :return: Where it got to: ``{"run_id", "status", "result"?, "error"?}``;
            ``status`` ``refused`` (and why, in ``error``) for a start
            refused for good (its input doesn't fit, it may not run).
        :raises Exception: It can't be started for now (its store isn't
            answering): it's started later, by the runner's upkeep.
        """
        ...

    async def progress(
        self,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        call_id: str,
        organization_id: str | None,
    ) -> dict[str, Any] | None:
        """
        How the run a call started is going, for the chat to show while it
        waits: ``{"run_id", "workflow", "status", "steps": [{"id", "name",
        "kind", "status", "started_at", "finished_at", "handed_to"}], ...}``,
        without what the steps were handed or handed on.

        :param agent_id: The chat agent (its ID, whichever version runs).
        :param user_id: Whose conversation it is.
        :param session_id: The conversation.
        :param call_id: The model's call of the workflow (ADK's function call ID).
        :return: None when that call, in that person's conversation, started no run.
        """
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


class RequestConflict(Exception):
    """A request changed since it was read (or a new one's ID is taken): read it again."""


class RequestStore(Protocol):
    """Keeps the requests agents file to their intakes (``forge_agent_runtime.requests``),
    by organization, agent and the person they're for, beyond one conversation.
    Each change is made to the version it was read at (compare-and-set)."""

    async def requests(
        self,
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
        ids: Sequence[str] | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        :param ids: Only these.
        :param status: Only those with it (``open``, ``submitting``, ``submitted``, ``cancelled``).
        :param limit: The most to read, newest first; None for all.
        :return: The person's requests with the agent, newest first, each as
            it was saved (``IntakeRequest``'s fields), with the ``version`` it's at.
        """
        ...

    async def save(
        self,
        requests: Sequence[Mapping[str, Any]],
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
    ) -> None:
        """
        Keeps these requests (``IntakeRequest``'s fields), new or changed, for
        the person: all of them or none. Each changed one is kept only if
        it's still at the ``version`` it was read at (None for a new one),
        and is at the next version once kept.

        :raises RequestConflict: One changed since it was read, or a new
            one's ID is taken: nothing was kept.
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
    :ivar workflow_wait: Seconds a workflow tool waits for its run when its
        node doesn't say (``wait_seconds``), before answering where it got to.
    :ivar workflow_wait_max: The most seconds a workflow tool may wait, whatever
        its node says: the deployment's cap. A workflow tool's call may take
        that long and :data:`WORKFLOW_WAIT_MARGIN` more, past ``tool_timeout``.
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
    :ivar skills: Finds the organization's skills its Skills tools name by
        ID (``forge_agent_runtime.skills``); without it, only those a
        document carries (its ``skills``) are found.
    :ivar ui_components: Finds the organization's team UI components its
        Rich answers nodes pick by key (``forge_agent_runtime.generative_ui``);
        without it, only those a document carries (its ``ui_components``)
        are found.
    :ivar intake_requests: Keeps the requests agents file to the intakes on
        their Tools way, for the person, across conversations; without it,
        each conversation keeps its own (in its state).
    :ivar access: Tells which of the organization's agent permissions
        whoever the current call is for holds, which agents' rules are
        checked against (``forge_agent_runtime.access``); without it, nobody
        holds any, so agents with a rule refuse.
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
    workflow_wait: float = 120.0
    workflow_wait_max: float = 900.0
    mcp_servers: McpServers | None = None
    knowledge_bases: KnowledgeBases | None = None
    credentials: CredentialProvider | None = None
    environment_note: str = ""
    tool_values: ToolValues | None = None
    ask_person: bool = True
    code_executor: BaseCodeExecutor | None = None
    skills: SkillLibrary | None = None
    ui_components: UiComponentLibrary | None = None
    intake_requests: RequestStore | None = None
    access: AccessCheck | None = None

    def wait_for_workflow(self, wanted: Any) -> float:
        """
        :param wanted: What a workflow tool's node says (``wait_seconds``):
            seconds, or nothing.
        :return: How long the tool waits for its run: what the node says, or
            :attr:`workflow_wait`, at most :attr:`workflow_wait_max`.
        """
        seconds = (
            float(wanted)
            if isinstance(wanted, int | float) and not isinstance(wanted, bool) and wanted > 0
            else self.workflow_wait
        )
        return min(seconds, self.workflow_wait_max)

    def but(self, **changes: Any) -> RuntimeServices:
        """:return: These services with some changed."""
        return replace(self, **changes)
