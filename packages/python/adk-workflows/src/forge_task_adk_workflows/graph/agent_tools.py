"""
An LLM agent's tools, and agents from the Agents page, in a run: built as
the hosted runtime builds them (``forge_agent_runtime``), from what the run
carries (its payload's snapshots, ``graph.uses``), when they're first used.

- :class:`LazyToolset`: one of an LLM agent's tools (``tools`` in its
  settings: the Agents builder's kinds and settings). The graph is built as
  the run starts, synchronously; a tool is built (an MCP server connected,
  an OpenAPI spec fetched) the first time its agent asks the model, and a
  tool that can't be fails its step, saying which.
- :func:`agent_node`: an ``llm`` node whose ``source`` is ``agent``: the
  agent from the Agents page, whole (its instruction, model, tools and
  hand-offs), sent the node's message, with its input schema's fields set
  from the run's data. Tools of its that can't be listed fail its step too
  (:class:`StepTools`), where a chat would run without them.
- :class:`AgentServices`: what both use: the models, the runtime's services
  (``${NAME}`` filled in from the worker's environment, where HTTP may go,
  the organization's MCP servers), knowledge base search, and the run's
  snapshots; and the toolsets a run opened, closed when it stops.
- :class:`ChildRuns` and :class:`RunWorkflows`: a workflow called as a tool
  is a run of its own, under an ID its call makes for it (:func:`child_run_id`):
  a call answered after its worker stopped is answered with the run it
  started. The calling run doesn't wait for it in its worker's slot: it
  pauses until that run ends (:meth:`RunWorkflows.waits_apart`).
- :func:`reads_only` and :func:`note_tool_calls`: which tools only read, and
  the run's note of each other call, made before it runs.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import httpx
from forge_agent_runtime import (
    BuildError,
    BundledSkills,
    RequestAuth,
    RuntimeServices,
    StateRefused,
    build_root_agent,
    build_tools,
    parse_document,
)
from forge_agent_runtime.access import HeldAccess, caller_from_json
from forge_agent_runtime.document import DocumentError
from forge_agent_runtime.executor import check_state
from forge_agent_runtime.services import ToolValues
from forge_agent_runtime.templating import REQUEST_KEY
from forge_agent_runtime.tools import (
    CalledAgent,
    HttpTool,
    KnowledgeBaseTool,
    LentToolset,
    SystemDesignTool,
    WorkflowTool,
)
from forge_codegraph import (
    Repository,
    Searched,
    design_summary,
    design_view,
    interleave,
    search_passages,
    system_summary,
)
from forge_codegraph_query import GraphError
from forge_common.adk import ForgeBaseToolset
from forge_common.patterns import schema_errors
from google.adk import Context, Event
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.load_artifacts_tool import LoadArtifactsTool
from google.adk.tools.skill_toolset import ListSkillsTool, LoadSkillResourceTool, LoadSkillTool
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.transfer_to_agent_tool import TransferToAgentTool
from google.adk.workflow import BaseNode, FunctionNode
from google.genai import types

from forge_task_adk_workflows.files import file_tools
from forge_task_adk_workflows.graph.data import CALLED_WITH_KEY, as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.support.errors import StepFailed

log = logging.getLogger(__name__)

#: Seconds a run waits for each thing it opened to finish closing (an MCP
#: session, its runner), as ADK's own runner waits for a toolset.
CLOSE_SECONDS = 10.0
#: How often a workflow called as a tool by a sub-agent's own runner (which a
#: run can't pause) is read until it ends.
_POLL = 0.5
#: A run that's still going (the run store's open statuses, but paused):
#: what a call waiting in place waits through.
_GOING = frozenset({"queued", "running", "waiting"})
#: A run that ended (the run store's finished statuses): what a call waiting
#: apart waits for.
_ENDED = frozenset({"succeeded", "failed", "abandoned"})
#: What the IDs of the runs a run's calls start are made in (:func:`child_run_id`).
_CHILDREN = uuid.UUID("5f0e6a2c-8f53-4c3e-9a4e-0d7b3c1f2a61")
#: Tools that only read: calling one again does nothing it didn't.
_READING = (
    KnowledgeBaseTool,
    SystemDesignTool,
    LoadArtifactsTool,
    TransferToAgentTool,
    ListSkillsTool,
    LoadSkillTool,
    LoadSkillResourceTool,
)
#: HTTP methods that only read.
_READING_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class KnowledgeSearch(Protocol):
    """Searches knowledge bases (the documents task's hybrid search)."""

    async def __call__(self, knowledge_bases: Mapping[str, str], query: str, limit: int) -> list[dict[str, Any]]:
        """
        :param knowledge_bases: The knowledge bases searched, together: each one's name by ID.
        :return: The passages that match best, best first: ``{knowledge_base, document, section, text, score}``.
        """
        ...


class ChildRuns(Protocol):
    """Starts another run, and reads it: what the worker's run control offers a run."""

    async def start(self, payload: dict[str, Any], *, run_id: str | None = None) -> str:
        """
        :param run_id: Its ID; started already, it's that run, not another.
        :return: The run's ID, queued with a job to take it.
        """
        ...

    async def get(self, run_id: str) -> dict[str, Any] | None:
        """:return: The run as the run store has it."""
        ...


@dataclass(frozen=True)
class AgentServices:
    """
    What a run's LLM agents use for their tools and agents from the Agents page.

    :ivar models: The models they run on (``ProviderModels``).
    :ivar runtime: The runtime's services: the environment ``${NAME}`` comes
        from, where HTTP tools may go, the organization's MCP servers.
    :ivar search: Searches knowledge bases; None when the worker can't.
    :ivar code: The code graph, which system design knowledge bases' code is
        searched in (``forge_codegraph_query.CodeGraph``); None when it isn't set up.
    :ivar workflow_wait: Seconds a workflow called as a tool is waited for.
    :ivar organization_id: The run's organization (per run).
    :ivar chat_agents: The agents from the Agents page the run carries, by reference (per run).
    :ivar knowledge_bases: The knowledge bases it carries: name, description and
        kind, and a graph one's repositories, by ID (per run).
    :ivar skills: The skills its LLM agents' Skills tools use, whole, by ID (per run).
    :ivar access: What whoever the run is for held of the agent permissions its
        agents' rules name, as it started (``caller_json``; per run); None: nobody holds any.
    :ivar workflows: Runs the workflows its LLM agents call (per run).
    :ivar opened: The toolsets the run built: closed when it stops.
    """

    models: Any
    runtime: RuntimeServices = field(default_factory=RuntimeServices)
    http: httpx.AsyncClient | None = None
    search: KnowledgeSearch | None = None
    code: Any = None
    workflow_wait: float = 120.0
    organization_id: str | None = None
    chat_agents: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    knowledge_bases: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    skills: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    access: Mapping[str, Any] | None = None
    workflows: Any = None
    opened: list[BaseToolset] = field(default_factory=list)

    def for_run(
        self,
        *,
        organization_id: str,
        chat_agents: Mapping[str, dict[str, Any]],
        knowledge_bases: Mapping[str, dict[str, Any]],
        workflows: Any = None,
        skills: Mapping[str, dict[str, Any]] | None = None,
        access: Mapping[str, Any] | None = None,
    ) -> "AgentServices":
        """:return: These services for one run: what it carries, and nothing opened yet."""
        return replace(
            self,
            organization_id=organization_id,
            chat_agents=dict(chat_agents),
            knowledge_bases=dict(knowledge_bases),
            skills=dict(skills or {}),
            access=access,
            workflows=workflows,
            opened=[],
        )

    @property
    def dependencies(self) -> dict[str, dict[str, Any]]:
        """The agents the run carries as the runtime finds saved agents: ``<id>@<version>``."""
        return {f"{doc.get('id')}@{doc.get('version')}": doc for doc in self.chat_agents.values()}

    def tool_services(self) -> RuntimeServices:
        """
        :return: The runtime's services for this run's tools: its knowledge
            bases, workflows and skills, and what its agents' rules are
            checked against: what whoever it's for held as it started.
        """
        return replace(
            self.runtime,
            knowledge_bases=SnapshotKnowledgeBases(self.knowledge_bases, self.search, self.code),
            workflows=self.workflows,
            skills=BundledSkills(self.skills),
            access=HeldAccess(caller_from_json(self.access)) if self.access is not None else None,
        )

    def keep(self, built: Sequence[Any]) -> None:
        """Note the toolsets something built, so they're closed when the run stops."""
        self.opened.extend(item for item in built if isinstance(item, BaseToolset))

    async def close(self) -> None:
        """Close what the run opened (MCP sessions), waiting CLOSE_SECONDS at most for each."""
        while self.opened:
            toolset = self.opened.pop()
            try:
                async with asyncio.timeout(CLOSE_SECONDS):
                    await toolset.close()
            except TimeoutError:
                log.warning(
                    "adk_workflows: closing a tool took more than %gs; the run goes on without it", CLOSE_SECONDS
                )
            except Exception:
                log.warning("adk_workflows: closing a tool failed", exc_info=True)


class SnapshotKnowledgeBases:
    """The organization's knowledge bases a run carries (checked as it
    started), searched with the worker's search: the runtime's ``KnowledgeBases``.
    A RAG knowledge base's documents with the documents task's search; a
    system design one's code, of the repositories the run carries for it,
    in the code graph (``forge_codegraph.search_passages``, as the admin API
    searches it), described with its design (its
    applications, components, groups and how they connect), which the tool
    beside the search reads. Their passages are taken in turn, best first."""

    def __init__(self, known: Mapping[str, dict[str, Any]], search: KnowledgeSearch | None, code: Any = None) -> None:
        self.known = known
        self.search_with = search
        self.code = code

    def _check(self, ids: Sequence[str]) -> None:
        missing = [i for i in ids if i not in self.known]
        if missing:
            raise LookupError(f"The run doesn't carry the knowledge base {missing[0]}")

    def _name(self, knowledge_base_id: str) -> str:
        return str(self.known[knowledge_base_id].get("name") or knowledge_base_id)

    def _graph(self, knowledge_base_id: str) -> bool:
        # A system design knowledge base; runs from before the rename say graph.
        return self.known[knowledge_base_id].get("kind") in ("system", "graph")

    def _repositories(self, knowledge_base_id: str) -> list[Any]:
        listed = self.known[knowledge_base_id].get("repositories")
        return [
            Repository(id=str(r["id"]), graph_id=str(r["graph_id"]), name=str(r.get("name") or ""))
            for r in (listed if isinstance(listed, list) else [])
            if isinstance(r, dict) and r.get("id") and r.get("graph_id")
        ]

    def _design(self, knowledge_base_id: str) -> Mapping[str, Any] | None:
        # Runs started before designs carry the connections alone.
        design = self.known[knowledge_base_id].get("design")
        return design if isinstance(design, Mapping) else None

    def _about(self, knowledge_base_id: str) -> str:
        description = str(self.known[knowledge_base_id].get("description") or "")
        if not self._graph(knowledge_base_id):
            return description
        design = self._design(knowledge_base_id)
        if design is not None:
            return design_summary(description, design)
        listed = self.known[knowledge_base_id].get("connections")
        connections = [
            {k: str(c.get(k) or "") for k in ("source", "kind", "target", "description")}
            for c in (listed if isinstance(listed, list) else [])
            if isinstance(c, dict)
        ]
        return system_summary(description, [r.name for r in self._repositories(knowledge_base_id)], connections)

    async def describe(
        self, knowledge_base_ids: Sequence[str], *, organization_id: str | None
    ) -> list[tuple[str, str]]:
        self._check(knowledge_base_ids)
        return [(self._name(i), self._about(i)) for i in knowledge_base_ids]

    async def design(
        self, knowledge_base_ids: Sequence[str], *, organization_id: str | None, focus: str = ""
    ) -> dict[str, Any] | None:
        self._check(knowledge_base_ids)
        systems = [i for i in dict.fromkeys(knowledge_base_ids) if self._graph(i)]
        if not systems:
            return None
        return design_view(
            [
                (
                    self._name(i),
                    str(self.known[i].get("description") or ""),
                    self._design(i) or {"connections": self.known[i].get("connections") or []},
                )
                for i in systems
            ],
            focus,
        )

    async def search(
        self, knowledge_base_ids: Sequence[str], query: str, *, organization_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        self._check(knowledge_base_ids)
        wanted = list(dict.fromkeys(knowledge_base_ids))
        documents = [i for i in wanted if not self._graph(i)]
        code = [i for i in wanted if self._graph(i)]
        ranked: list[list[dict[str, Any]]] = []
        if documents:
            if self.search_with is None:
                raise RuntimeError(
                    "Knowledge bases can't be searched on this worker: its documents task isn't set up "
                    "(HYBRID_MONGO__URI, HYBRID_EMBEDDING__*)"
                )
            ranked.append(await self.search_with({i: self._name(i) for i in documents}, query, limit))
        if code:
            ranked.append(await self._search_code(code, query, limit))
        return interleave(ranked, limit)

    async def _search_code(self, knowledge_base_ids: list[str], query: str, limit: int) -> list[dict[str, Any]]:
        if self.code is None:
            raise RuntimeError(
                "System design knowledge bases can't be searched on this worker: set "
                "HYBRID_ADK_WORKFLOWS__CODEGRAPH__SPANNER__DATABASE and "
                "HYBRID_ADK_WORKFLOWS__CODEGRAPH__SPANNER__CURSOR_SIGNING_KEY"
            )
        # The admin API's search too (forge_codegraph.search_passages).
        searched = [Searched(i, self._name(i), self._repositories(i)) for i in knowledge_base_ids]
        try:
            return await search_passages(self.code, searched, query, limit=limit)
        except GraphError as error:
            raise RuntimeError(f"The code graph couldn't search: {error}.") from None


class LazyToolset(BaseToolset):
    """
    One of an LLM agent's tools, built the first time its agent asks the
    model (``forge_agent_runtime.build_tools``).

    :param tool: The tool: ``{id, kind, name, config}``.
    :param where: Its node and agent, for a failure: ``Node 'Triage', tool 'Docs'``.
    :param step: Its node's ID, for a failure.
    :param services: The run's services.
    :param values: Fills in ``{{ }}`` in an HTTP tool's URL and body from the run's data.
    """

    def __init__(
        self,
        tool: dict[str, Any],
        *,
        where: str,
        step: str | None,
        services: Any,
        values: ToolValues | None = None,
    ) -> None:
        super().__init__()
        self.tool = tool
        self.where = where
        self.step = step
        self.services = services
        self.values = values
        self._built: list[Any] | None = None
        self._lock = asyncio.Lock()
        #: Why it couldn't be built or listed: ADK runs the agent without a
        #: toolset that fails to load, so its step fails with this instead
        #: (:func:`failing_tools_first`).
        self.failure: RunFailed | None = None

    async def _build(self) -> list[Any]:
        async with self._lock:
            if self._built is None:
                agents: AgentServices | None = getattr(self.services, "agents", None)
                if agents is None:
                    self.failure = RunFailed(f"{self.where}: tools can't be used on this worker", step=self.step)
                    raise self.failure
                config = self.tool.get("config")
                try:
                    built = await build_tools(
                        str(self.tool.get("name") or ""),
                        str(self.tool.get("kind") or ""),
                        config if isinstance(config, dict) else {},
                        models=agents.models,
                        services=agents.tool_services().but(tool_values=self.values),
                        http=agents.http,
                        organization_id=agents.organization_id,
                        dependencies=agents.dependencies,
                    )
                except BuildError as error:
                    self.failure = RunFailed(f"{self.where}: {error}", step=self.step)
                    raise self.failure from None
                agents.keep(built)
                self._built = built
            return self._built

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for item in await self._build():
            if isinstance(item, BaseToolset):
                try:
                    # A chat agent runs without tools that won't list; a step fails.
                    listing = item.get_tools_or_raise if isinstance(item, ForgeBaseToolset) else item.get_tools
                    tools.extend(await listing(readonly_context))
                except Exception as error:  # An MCP server that won't connect, or lets nothing in.
                    self.failure = RunFailed(f"{self.where}: {type(error).__name__}: {error}", step=self.step)
                    raise self.failure from None
            else:
                tools.append(item)
        return tools

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        # What a built toolset adds to the request besides its tools: a Skills
        # tool's guidance and its skills' names and descriptions.
        for item in self._built or []:
            if isinstance(item, BaseToolset):
                await item.process_llm_request(tool_context=tool_context, llm_request=llm_request)

    async def close(self) -> None:
        for item in self._built or []:
            if isinstance(item, BaseToolset):
                await item.close()


class StepTools(LentToolset):
    """
    A toolset of an Agents-page agent a step runs (:func:`agent_node`), or of
    one it hands off to or calls as a tool: used as the agent has it, but
    when its tools can't be listed (an MCP server that won't connect), the
    step fails saying why (``failure``, :func:`fail_unlisted_tools`), as an
    LLM step's own tools do, where a chat agent would run without them. The
    run closes the toolset (``AgentServices.keep``), not a runner it's lent to.

    :param label: How the failure names the step: ``Triage (triage)``.
    :param step: The step's ID.
    """

    def __init__(self, toolset: BaseToolset, *, label: str, step: str | None) -> None:
        super().__init__(toolset)
        self.label = label
        self.step = step
        self.failure: RunFailed | None = None

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        toolset = self.toolset
        listing = toolset.get_tools_or_raise if isinstance(toolset, ForgeBaseToolset) else toolset.get_tools_with_prefix
        try:
            tools = await listing(readonly_context)
        except Exception as error:
            agent = readonly_context.agent_name if readonly_context is not None else "its agent"
            self.failure = RunFailed(
                f"{self.label} failed: tools of {agent} can't be used: {type(error).__name__}: {error}",
                step=self.step,
            )
            raise self.failure from None
        # Listed again once it can be (the step run again): it no longer fails it.
        self.failure = None
        return tools


def fail_unlisted_tools(agent: BaseAgent, *, label: str, step: str | None) -> None:
    """
    Gives an Agents-page agent a step runs, and every agent it hands off to
    or calls as a tool, :class:`StepTools` in place of its toolsets, and has
    each check them all first among its ``before_model_callback``: tools any
    of them can't list fail the step, however the call that reached it ends.
    """
    listed: list[StepTools] = []
    agents: list[LlmAgent] = []

    def visit(at: BaseAgent) -> None:
        if isinstance(at, LlmAgent):
            agents.append(at)
            tools: list[Any] = []
            for tool in at.tools:
                own = tool.toolset if isinstance(tool, LentToolset) else tool
                if isinstance(own, CalledAgent):
                    # An agent it calls as a tool: its own toolsets, on a runner of its own.
                    visit(own.tool.agent)
                elif isinstance(own, BaseToolset):
                    tool = StepTools(own, label=label, step=step)
                    listed.append(tool)
                tools.append(tool)
            at.tools = tools
        for sub in at.sub_agents:
            visit(sub)

    visit(agent)
    check = failing_tools_first(listed, None)
    for at in agents:
        current = at.before_model_callback
        at.before_model_callback = [check, *(current if isinstance(current, list) else [current] if current else [])]


def failing_tools_first(tools: Sequence[LazyToolset | StepTools], callback: Any) -> Callable[..., Any]:
    """
    An LLM agent's ``before_model_callback``: it fails its step when one of
    its tools couldn't be set up (ADK would have it answer without it), then
    does what ``callback`` does (the model and thinking level its settings pick).
    """

    def check(callback_context: CallbackContext, llm_request: Any) -> Any:
        for toolset in tools:
            if toolset.failure is not None:
                raise toolset.failure
        return callback(callback_context, llm_request) if callback is not None else None

    return check


def failing_model_errors(label: str, step: str | None) -> Callable[..., None]:
    """
    An LLM agent's ``after_model_callback``: a model that answers with an error
    instead of an answer fails the agent's step, as a model that raised would.
    ADK reports some failures that way without raising, including errors on
    partial responses and interrupted generations. Those must not let later
    steps run on an unfinished answer.

    :param label: How the failure names the step: ``Triage (triage)``.
    :param step: The step's ID.
    """

    def check(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
        if llm_response.error_code or llm_response.error_message:
            raise RunFailed(f"{label} failed: {model_error(llm_response)}", step=step)
        if llm_response.interrupted:
            raise RunFailed(
                f"{label} failed: its model stopped before finishing its response. Please try again.", step=step
            )

    return check


#: What the model error codes that don't say so themselves mean, and what to try.
MODEL_ERRORS = {
    "MALFORMED_FUNCTION_CALL": "it called a tool with arguments that aren't a JSON object. Most often its "
    "answer was cut off at its output token limit (raise its max output tokens), or the model can't follow "
    "the tool's parameters (simplify them, or try another model)",
    "MAX_TOKENS": "its answer was cut off at its output token limit: raise its max output tokens",
}


def model_error(llm_response: LlmResponse) -> str:
    """
    :return: A model's error answer, readably: its code, what it said, what
        the code means, and how many tokens it had answered with (``its model
        answered with an error (MAX_TOKENS) after 1,024 output tokens: ...``).
    """
    code = str(getattr(llm_response.error_code, "value", llm_response.error_code) or "MODEL_ERROR")
    usage = llm_response.usage_metadata
    tokens = usage.candidates_token_count if usage is not None else None
    after = f" after {tokens:,} output tokens" if tokens else ""
    said = f": {llm_response.error_message}" if llm_response.error_message else ""
    meaning = MODEL_ERRORS.get(code)
    return f"its model answered with an error ({code}){after}{said}" + (f". That means {meaning}" if meaning else "")


def fail_model_errors(agent: BaseAgent, check: Callable[..., None]) -> None:
    """Puts ``check`` (:func:`failing_model_errors`) first among the
    ``after_model_callback`` of an agent and of every agent it hands off to."""
    if isinstance(agent, LlmAgent):
        current = agent.after_model_callback
        agent.after_model_callback = [check, *(current if isinstance(current, list) else [current] if current else [])]
    for sub in agent.sub_agents:
        fail_model_errors(sub, check)


def add_tools(agent: BaseAgent, tools: list[BaseTool]) -> None:
    """Gives an agent, and every agent it hands off to, ``tools`` too (an LLM
    agent that has a tool of the same name keeps its own)."""
    if isinstance(agent, LlmAgent):
        names = {getattr(tool, "name", None) for tool in agent.tools}
        agent.tools = [*agent.tools, *(tool for tool in tools if tool.name not in names)]
    for sub in agent.sub_agents:
        add_tools(sub, tools)


def note_tool_calls(agent: BaseAgent, note: Callable[..., Any] | None) -> None:
    """Puts ``note`` (the run's, ``RunServices.tool_calls``) first among the
    ``before_tool_callback`` of an agent and of every agent it hands off to."""
    if note is None:
        return
    if isinstance(agent, LlmAgent):
        current = agent.before_tool_callback
        agent.before_tool_callback = [note, *(current if isinstance(current, list) else [current] if current else [])]
    for sub in agent.sub_agents:
        note_tool_calls(sub, note)


def reads_only(tool: BaseTool) -> bool:
    """
    Whether a tool only reads, so calling it again does nothing it didn't: a
    knowledge base's search, a system's design, the run's files, a hand-off,
    an HTTP tool that GETs, an MCP tool its server says only reads
    (``readOnlyHint``).
    """
    if isinstance(tool, _READING):
        return True
    if isinstance(tool, HttpTool):
        return tool.method in _READING_METHODS
    annotations = getattr(getattr(tool, "_mcp_tool", None), "annotations", None)
    return getattr(annotations, "readOnlyHint", None) is True


def starts_runs(tool: BaseTool) -> bool:
    """Whether a tool's call starts a run (a workflow): what it does is that run, once."""
    return isinstance(tool, WorkflowTool)


def child_run_id(run_id: str, call_id: str) -> str:
    """:return: The ID of the run a workflow tool's call (``call_id``, in run
    ``run_id``) starts: the same each time the call is answered."""
    return uuid.uuid5(_CHILDREN, f"{run_id}/{call_id}").hex


def toolsets_of(agent: BaseAgent) -> list[BaseToolset]:
    """:return: The toolsets of an agent and the agents it hands off to, for closing."""
    found: list[BaseToolset] = []
    if isinstance(agent, LlmAgent):
        found.extend(tool for tool in agent.tools if isinstance(tool, BaseToolset))
    for sub in agent.sub_agents:
        found.extend(toolsets_of(sub))
    return found


@dataclass(frozen=True)
class AgentSource:
    """
    An ``llm`` node that is an agent from the Agents page.

    :ivar ref: Which one, at which version (``graph.uses.agent_ref``).
    :ivar message: What it's sent: a template; empty for what the node's handed.
    :ivar inputs: Its input schema's fields, each a JSONata expression.
    """

    ref: str
    message: str
    inputs: Mapping[str, str]


def agent_node(
    *,
    name: str,
    label: str,
    step: str,
    source: AgentSource,
    services: Any,
    data: Callable[[Context, Any], dict[str, Any]],
    parse: Callable[[str], Any],
) -> BaseNode:
    """
    :param name: The node's ADK name: the agent runs under it.
    :param label: How a failure names the step: ``Triage (triage)``.
    :param step: The node's ID.
    :param services: The run's services (``RunServices``).
    :param data: What the node's expressions read, as the run goes.
    :param parse: How its answer becomes what it hands on.
    """
    built: list[LlmAgent] = []
    lock = asyncio.Lock()

    def fail(message: str) -> RunFailed:
        return RunFailed(f"{label} failed: {message}", step=step)

    async def agent_of(agents: AgentServices) -> LlmAgent:
        async with lock:
            if not built:
                raw = agents.chat_agents.get(source.ref)
                if raw is None:
                    raise fail(f"the run doesn't carry the agent {source.ref}")
                try:
                    document = parse_document(
                        {**raw, "dependencies": {**agents.dependencies, **raw.get("dependencies", {})}}
                    )
                    root = await build_root_agent(
                        document, models=agents.models, services=agents.tool_services(), http=agents.http
                    )
                except (BuildError, DocumentError) as error:
                    raise fail(f"its agent can't be built: {error}") from None
                agents.keep(toolsets_of(root))
                agent = root.clone(update={"name": name})
                if getattr(services, "artifacts", False) or getattr(services, "files", ()):
                    # It reads the run's files and saves its own, as the run's LLM agents do.
                    add_tools(agent, file_tools(file.name for file in services.files))
                fail_model_errors(agent, failing_model_errors(label, step))
                fail_unlisted_tools(agent, label=label, step=step)
                note_tool_calls(agent, getattr(services, "tool_calls", None))
                built.append(agent)
            return built[0]

    async def node(adk: Context, node_input: Any) -> AsyncGenerator[Event]:
        agents: AgentServices | None = getattr(services, "agents", None)
        if agents is None:
            raise fail("agents from the Agents page can't run on this worker")
        known = data(adk, node_input)
        evaluator = services.evaluator
        try:
            message = (
                await evaluator.render(source.message, known, what="Its message")
                if source.message.strip()
                else node_input
            )
            inputs = {
                key: as_data(await evaluator.evaluate(expression, known, what=f"Its input {key}"))
                for key, expression in source.inputs.items()
                if expression.strip()
            }
        except StepFailed as error:
            raise fail(error.message) from None
        raw = agents.chat_agents.get(source.ref) or {}
        try:
            checked = check_state(parse_document(raw), inputs) if raw else inputs
        except (StateRefused, DocumentError) as error:
            raise fail(f"its inputs don't fit the agent: {error}") from None
        agent = await agent_of(agents)
        text = message if isinstance(message, str) else json.dumps(as_data(message), ensure_ascii=False)
        request = {
            "appName": str(raw.get("id") or source.ref),
            "userId": adk.session.user_id,
            "sessionId": adk.session.id,
            "newMessage": {"role": "user", "parts": [{"text": text}]},
            "streaming": False,
        }

        def set_state(callback_context: CallbackContext) -> types.Content | None:
            # What its instructions read: {{ request.* }} and {{ state.<field> }}.
            callback_context.state[REQUEST_KEY] = request
            for key, value in checked.items():
                callback_context.state[key] = value
            return None

        # Before its own: the check of its node's rule, when it has one.
        current = agent.before_agent_callback
        kept = current if isinstance(current, list) else [current] if current else []
        running = agent.clone(update={"before_agent_callback": [set_state, *kept]})
        # What it asks its agent, kept before the agent runs: the run page
        # shows it, though the agent fail.
        yield Event(custom_metadata={CALLED_WITH_KEY: {"message": text, "inputs": as_data(checked)}})
        answer = await adk.run_node(running, node_input=text, run_id="agent", use_sub_branch=True)
        yield Event(output=parse(answer) if isinstance(answer, str) else as_data(answer))

    return FunctionNode(name=name, func=node, rerun_on_resume=True)


class RunWorkflows:
    """
    The workflows a run's LLM agents call as tools (the runtime's
    ``WorkflowRunner``): each a run of its own, of the document the run
    carries, as the member the run acts as, waited for a while (the call's
    wait). Each call's run has the ID the call makes for it
    (:func:`child_run_id`): a call answered after its worker stopped is
    answered with the run it started, never another.

    A call made in the run's own invocation (:meth:`waits_apart`) starts its
    run and returns nothing: its tool is long-running there
    (``forge_agent_runtime.WorkflowTools``), so ADK pauses its agent at the
    call, and the calling run waits for the run at a gate of its own (runs.py),
    its worker's slot given back meanwhile. The run's end (or the call's wait
    being up) answers the call with the run as it is then (:meth:`answer_of`).
    A call made anywhere else (a sub-agent's tool, which runs in a runner of
    its own that the run can't pause) waits for its run in place (:meth:`attach`).

    :param payload: The calling run (its ``RunPayload``, as a dict).
    :param run_id: The calling run's ID.
    :param children: Starts and reads runs.
    :param wait: Seconds to wait for one.
    :param invocation_id: The calling run's ADK invocation: calls made in it
        wait for their runs apart. None: every call waits in place.
    :param adopted: Calls answered by a run an earlier call started, by call
        ID (an agent that sees only its own turn making that call again while
        the run still goes): ``{run, since, wait}``, the run, and when the call
        was made and how long it waits. They start no run of their own. Kept
        with the run.
    """

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        run_id: str,
        children: ChildRuns,
        wait: float,
        invocation_id: str | None = None,
        adopted: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.payload = payload
        self.run_id = run_id
        self.children = children
        self.wait = wait
        self.invocation_id = invocation_id
        self.adopted = adopted if adopted is not None else {}
        #: What calls waiting apart that failed answer (their tool's failure), by call ID: for this attempt.
        self.failures: dict[str, dict[str, Any]] = {}

    def waits_apart(self, context: Any) -> bool:
        """:return: Whether a call made in ``context`` waits for its run apart
        from the call: in the run's own invocation, which can pause."""
        return self.invocation_id is not None and getattr(context, "invocation_id", None) == self.invocation_id

    def refused(self, context: Any, answer: dict[str, Any]) -> None:
        """A call waiting apart failed before its run started (input that
        doesn't fit the workflow's start, a run that can't be started): the
        calling run answers it with ``answer``, its tool's failure."""
        call_id = getattr(context, "function_call_id", None)
        if call_id:
            self.failures[str(call_id)] = dict(answer)

    def child_of(self, call_id: str) -> str:
        """:return: The run a call waits for: the one it started, or the one it was answered by."""
        adopted = self.adopted.get(call_id)
        return str(adopted["run"]) if adopted else child_run_id(self.run_id, call_id)

    def _document(self, workflow_id: str) -> dict[str, Any]:
        """:param workflow_id: Its reference: ``ag_x``, ``ag_x@3``, ``ag_x@draft``."""
        document = (self.payload.get("saved") or {}).get(workflow_id)
        if not isinstance(document, dict):
            raise LookupError(f"The run doesn't carry the workflow {workflow_id}")
        return document

    async def describe(self, workflow_id: str, *, organization_id: str | None) -> tuple[str, str, dict[str, Any]]:
        document = self._document(workflow_id)
        schema: dict[str, Any] = {}
        for node in document.get("nodes") or []:
            if isinstance(node, dict) and node.get("kind") == "start":
                found = (node.get("config") or {}).get("input_schema")
                schema = found if isinstance(found, dict) else {}
        return str(document.get("name") or workflow_id), str(document.get("description") or ""), schema

    async def run(
        self,
        workflow_id: str,
        workflow_input: dict[str, Any],
        *,
        organization_id: str | None,
        context: Any,
        wait: float | None = None,
    ) -> Any:
        document = self._document(workflow_id)
        call_id = getattr(context, "function_call_id", None)
        _name, _about, schema = await self.describe(workflow_id, organization_id=organization_id)
        if schema:
            problems = [error.message for error in schema_errors(schema, workflow_input)]
            if problems:
                raise ValueError("The input doesn't fit the workflow's start: " + "; ".join(problems[:5]))
        payload = {
            **{
                key: self.payload.get(key) for key in ("tenant_id", "saved", "chat_agents", "knowledge_bases", "skills")
            },
            # It's called by reference (ag_x@3): the run is of the workflow,
            # at the version it carries (published) or its draft.
            "agent_id": workflow_id.partition("@")[0],
            "version": document.get("version", "draft"),
            "revision": 0,
            "name": str(document.get("name") or ""),
            "document": document,
            "input": workflow_input,
            "session_id": str(uuid.uuid4()),
            "run_as": self.payload.get("run_as"),
            "run_as_name": self.payload.get("run_as_name") or "",
            # What whoever the calling run is for held of the permissions its
            # agents' rules name, as it started (the agents of the workflows
            # it calls among them): the run's own agents are checked against
            # it too, whether its call waits apart or in place.
            "agent_access": self.payload.get("agent_access"),
            "trigger": {
                "type": "workflow",
                "run_id": self.run_id,
                "agent_id": self.payload.get("agent_id"),
                "tool_of": getattr(context, "agent_name", None),
                "call_id": call_id,
                # How long its call waits for it: the calling run's gate's deadline.
                "wait_seconds": self.wait if wait is None else wait,
            },
        }
        child = self.child_of(call_id) if call_id and call_id in self.adopted else None
        if child is None:
            child = await self.children.start(payload, run_id=child_run_id(self.run_id, call_id) if call_id else None)
        if call_id and self.waits_apart(context):
            return None  # the calling run waits for it, and answers the call
        return await self.attach(child, wait=wait)

    async def attach(self, child: str, *, wait: float | None = None) -> dict[str, Any]:
        """
        :param child: A run a call started.
        :param wait: Seconds to wait for it, as its Workflow tool says; None
            for this runner's default.
        :return: What the call answers: the run's ID and status, and its
            result or error once it ended; waited for a while first.
        """
        deadline = asyncio.get_running_loop().time() + (self.wait if wait is None else wait)
        current: dict[str, Any] | None = None
        while True:
            current = await self.children.get(child)
            status = current["status"] if current else "missing"
            if status not in _GOING or asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(_POLL)
        return self.answer_of(child, current)

    @staticmethod
    def answer_of(child: str, current: Mapping[str, Any] | None) -> dict[str, Any]:
        """
        :param current: The run as the run store has it now; None when there's no such run.
        :return: What a call answers of the run it started: the run's ID and
            status (``missing`` when there's none), and its result or error
            once it ended.
        """
        answer: dict[str, Any] = {"run_id": child, "status": current["status"] if current else "missing"}
        if current is not None:
            for key in ("result", "error"):
                if current.get(key) is not None:
                    answer[key] = current[key]
        return answer

    @staticmethod
    def going(current: Mapping[str, Any] | None) -> bool:
        """:return: Whether a run is there and hasn't ended (paused for a person, it's still going)."""
        return current is not None and current.get("status") not in _ENDED


__all__ = [
    "AgentServices",
    "AgentSource",
    "ChildRuns",
    "KnowledgeSearch",
    "LazyToolset",
    "RunWorkflows",
    "SnapshotKnowledgeBases",
    "agent_node",
    "child_run_id",
    "note_tool_calls",
    "reads_only",
    "starts_runs",
    "toolsets_of",
]


class WorkerMcpServers:
    """
    The organization's MCP servers, for a run's tools (the runtime's
    ``McpServers``): connected with their credentials as the admin keeps
    them (``forge_mcp_servers``), a renewed grant kept on the server's row.

    :param servers: The servers' service (their auth methods and the key their
        credentials are encrypted with); None when the worker has no key.
    :param store: Where they're kept: the admin MySQL.
    :param unavailable: Why they can't be used, when they can't.
    """

    def __init__(self, servers: Any, store: Any, *, unavailable: str | None = None) -> None:
        self.servers = servers
        self.store = store
        self.unavailable = unavailable

    async def __call__(self, config: Mapping[str, Any], *, organization_id: str | None) -> BaseToolset:
        from forge_mcp_servers import mcp_toolset

        if self.servers is None or self.store is None:
            raise LookupError(self.unavailable or "the organization's MCP servers can't be used on this worker")
        if organization_id is None:
            raise LookupError("only an organization's workflows can use its MCP servers")
        return await mcp_toolset(self.servers, self.store, organization_id=organization_id, config=dict(config))


#: What a ``${NAME}`` the worker doesn't let tools read adds to its error.
TOOL_ENVIRONMENT_NOTE = (
    ": workflow runs fill in only the names HYBRID_ADK_WORKFLOWS__TOOL_ENVIRONMENT lists. "
    "Use one of the organization's credentials instead"
)


def tool_environment(names: list[str], environment: Mapping[str, str]) -> dict[str, str]:
    """:return: The variables of ``environment`` that ``names`` lists: what tools may read."""
    return {name: environment[name] for name in names if name in environment}


class WorkerCredentials:
    """
    The organization's API credentials, for a run's HTTP steps and its tools
    (the runtime's ``CredentialProvider``): read from the admin MySQL with the
    admin's secrets key (``forge_mcp_servers.api_credentials``), each sent only
    to the URLs it's for, a renewed OAuth grant kept on its row.

    :param credentials: The credentials' service; None when the worker has no key.
    :param store: Where they're kept: the admin MySQL.
    :param unavailable: Why they can't be used, when they can't.
    """

    def __init__(self, credentials: Any, store: Any, *, unavailable: str | None = None) -> None:
        self.credentials = credentials
        self.store = store
        self.unavailable = unavailable

    async def __call__(self, credential_id: str, *, organization_id: str | None, url: str) -> RequestAuth:
        from forge_mcp_servers import request_credentials

        if self.credentials is None or self.store is None:
            raise LookupError(self.unavailable or "the organization's credentials can't be used on this worker")
        found = await request_credentials(
            self.credentials,
            self.store,
            credential_id=credential_id,
            organization_id=organization_id,
            url=url,
        )
        return RequestAuth(headers=found.headers, params=found.params)


class DocumentsSearch:
    """
    Knowledge bases searched with the documents task's knowledge base search,
    as the admin API searches them: the ones a tool names, and only those,
    ranked together.

    :param search: ``forge_task_documents.retrieval.KnowledgeBaseSearch``.
    """

    def __init__(self, search: Any) -> None:
        self.search = search

    async def __call__(self, knowledge_bases: Mapping[str, str], query: str, limit: int) -> list[dict[str, Any]]:
        passages = await self.search.search(list(knowledge_bases), query, limit=limit)
        return [passage.for_agent(knowledge_base=knowledge_bases[passage.knowledge_base_id]) for passage in passages]
