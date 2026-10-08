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
  from the run's data.
- :class:`AgentServices`: what both use: the models, the runtime's services
  (``${NAME}`` filled in from the worker's environment, where HTTP may go,
  the organization's MCP servers), knowledge base search, and the run's
  snapshots; and the toolsets a run opened, closed when it stops.
- :class:`ChildRuns` and :class:`RunWorkflows`: a workflow called as a tool
  is a run of its own, started and waited for, as the hosted runtime's are.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import httpx
from forge_agent_runtime import (
    BuildError,
    RuntimeServices,
    StateRefused,
    build_root_agent,
    build_tools,
    parse_document,
)
from forge_agent_runtime.document import DocumentError
from forge_agent_runtime.executor import check_state
from forge_agent_runtime.templating import REQUEST_KEY
from forge_codegraph import Repository, WorkerError, code_passages, interleave, system_summary
from google.adk import Context, Event
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import BaseNode, FunctionNode
from google.genai import types
from jsonschema import Draft202012Validator

from forge_task_adk_workflows.graph.data import as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.support.errors import StepFailed

log = logging.getLogger(__name__)

#: How long a workflow called as a tool is polled for its end.
_POLL = 0.5
#: A run that's still going (the run store's open statuses, but paused).
_GOING = frozenset({"queued", "running", "waiting"})


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

    async def start(self, payload: dict[str, Any]) -> str:
        """:return: The new run's ID, queued with a job to take it."""
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
    :ivar code: The code graph worker's API, which searches graph knowledge
        bases (``forge_codegraph.CodeGraph``); None when it isn't set up.
    :ivar workflow_wait: Seconds a workflow called as a tool is waited for.
    :ivar organization_id: The run's organization (per run).
    :ivar chat_agents: The agents from the Agents page the run carries, by reference (per run).
    :ivar knowledge_bases: The knowledge bases it carries: name, description and
        kind, and a graph one's repositories, by ID (per run).
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
    workflows: Any = None
    opened: list[BaseToolset] = field(default_factory=list)

    def for_run(
        self,
        *,
        organization_id: str,
        chat_agents: Mapping[str, dict[str, Any]],
        knowledge_bases: Mapping[str, dict[str, Any]],
        workflows: Any = None,
    ) -> "AgentServices":
        """:return: These services for one run: what it carries, and nothing opened yet."""
        return replace(
            self,
            organization_id=organization_id,
            chat_agents=dict(chat_agents),
            knowledge_bases=dict(knowledge_bases),
            workflows=workflows,
            opened=[],
        )

    @property
    def dependencies(self) -> dict[str, dict[str, Any]]:
        """The agents the run carries as the runtime finds saved agents: ``<id>@<version>``."""
        return {f"{doc.get('id')}@{doc.get('version')}": doc for doc in self.chat_agents.values()}

    def tool_services(self) -> RuntimeServices:
        """:return: The runtime's services for this run's tools: its knowledge bases and workflows."""
        return replace(
            self.runtime,
            knowledge_bases=SnapshotKnowledgeBases(self.knowledge_bases, self.search, self.code),
            workflows=self.workflows,
        )

    def keep(self, built: Sequence[Any]) -> None:
        """Note the toolsets something built, so they're closed when the run stops."""
        self.opened.extend(item for item in built if isinstance(item, BaseToolset))

    async def close(self) -> None:
        """Close what the run opened (MCP sessions)."""
        while self.opened:
            toolset = self.opened.pop()
            try:
                await toolset.close()
            except Exception:
                log.warning("adk_workflows: closing a tool failed", exc_info=True)


class SnapshotKnowledgeBases:
    """The organization's knowledge bases a run carries (checked as it
    started), searched with the worker's search: the runtime's ``KnowledgeBases``.
    A RAG knowledge base's documents with the documents task's search; a
    system design one's code, of the repositories the run carries for it,
    with the code graph worker's API, described with how its applications
    connect. Their passages are taken in turn, best first."""

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

    def _about(self, knowledge_base_id: str) -> str:
        description = str(self.known[knowledge_base_id].get("description") or "")
        if not self._graph(knowledge_base_id):
            return description
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
                "Graph knowledge bases can't be searched on this worker: set "
                "HYBRID_ADK_WORKFLOWS__CODEGRAPH_URL and HYBRID_ADK_WORKFLOWS__CODEGRAPH_TOKEN"
            )
        # Each repository's hits cited as from the first knowledge base that has it.
        owners: dict[str, tuple[str, Any]] = {}
        for i in knowledge_base_ids:
            for repository in self._repositories(i):
                owners.setdefault(repository.graph_id, (i, repository))
        if not owners:
            return []
        try:
            hits = await self.code.search(list(owners)[:50], query, limit=limit)
        except WorkerError as error:
            raise RuntimeError(f"The code graph is unavailable; try again ({error.code}).") from None
        passages: list[dict[str, Any]] = []
        for hit in hits:
            owner = owners.get(str(hit.get("repository_id", "")))
            if owner is not None:
                knowledge_base_id, repository = owner
                passages += code_passages(
                    [hit],
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base=self._name(knowledge_base_id),
                    repositories=[repository],
                )
        return passages


class LazyToolset(BaseToolset):
    """
    One of an LLM agent's tools, built the first time its agent asks the
    model (``forge_agent_runtime.build_tools``).

    :param tool: The tool: ``{id, kind, name, config}``.
    :param where: Its node and agent, for a failure: ``Node 'Triage', tool 'Docs'``.
    :param step: Its node's ID, for a failure.
    :param services: The run's services.
    """

    def __init__(self, tool: dict[str, Any], *, where: str, step: str | None, services: Any) -> None:
        super().__init__()
        self.tool = tool
        self.where = where
        self.step = step
        self.services = services
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
                        services=agents.tool_services(),
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
                    tools.extend(await item.get_tools(readonly_context))
                except Exception as error:  # An MCP server that won't connect, or lets nothing in.
                    self.failure = RunFailed(f"{self.where}: {type(error).__name__}: {error}", step=self.step)
                    raise self.failure from None
            else:
                tools.append(item)
        return tools

    async def close(self) -> None:
        for item in self._built or []:
            if isinstance(item, BaseToolset):
                await item.close()


def failing_tools_first(tools: Sequence[LazyToolset], callback: Any) -> Callable[..., Any]:
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
                built.append(root.clone(update={"name": name}))
            return built[0]

    async def run(adk: Context, node_input: Any) -> Any:
        agents: AgentServices | None = getattr(services, "agents", None)
        if agents is None:
            raise fail("agents from the Agents page can't run on this worker")
        known = data(adk, node_input)
        evaluator = services.evaluator
        try:
            message = (
                evaluator.render(source.message, known, what="Its message") if source.message.strip() else node_input
            )
            inputs = {
                key: as_data(evaluator.evaluate(expression, known, what=f"Its input {key}"))
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

        running = agent.clone(update={"before_agent_callback": set_state})
        answer = await adk.run_node(running, node_input=text, run_id="agent", use_sub_branch=True)
        return parse(answer) if isinstance(answer, str) else as_data(answer)

    async def node(adk: Context, node_input: Any) -> Event:
        return Event(output=await run(adk, node_input))

    return FunctionNode(name=name, func=node, rerun_on_resume=True)


class RunWorkflows:
    """
    The workflows a run's LLM agents call as tools (the runtime's
    ``WorkflowRunner``): each a run of its own, of the document the run
    carries, as the member the run acts as, waited for a while. A run that
    pauses (an approval, a person's answer) or takes longer answers with
    where it got to.

    :param payload: The calling run (its ``RunPayload``, as a dict).
    :param run_id: The calling run's ID.
    :param children: Starts and reads runs.
    :param wait: Seconds to wait for one.
    """

    def __init__(self, payload: dict[str, Any], *, run_id: str, children: ChildRuns, wait: float) -> None:
        self.payload = payload
        self.run_id = run_id
        self.children = children
        self.wait = wait

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
        self, workflow_id: str, workflow_input: dict[str, Any], *, organization_id: str | None, context: Any
    ) -> Any:
        document = self._document(workflow_id)
        _name, _about, schema = await self.describe(workflow_id, organization_id=organization_id)
        if schema:
            problems = [error.message for error in Draft202012Validator(schema).iter_errors(workflow_input)]
            if problems:
                raise ValueError("The input doesn't fit the workflow's start: " + "; ".join(problems[:5]))
        payload = {
            **{key: self.payload.get(key) for key in ("tenant_id", "saved", "chat_agents", "knowledge_bases")},
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
            "trigger": {
                "type": "workflow",
                "run_id": self.run_id,
                "agent_id": self.payload.get("agent_id"),
                "tool_of": getattr(context, "agent_name", None),
            },
        }
        child = await self.children.start(payload)
        deadline = asyncio.get_running_loop().time() + self.wait
        current: dict[str, Any] | None = None
        while True:
            current = await self.children.get(child)
            status = current["status"] if current else "missing"
            if status not in _GOING or asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(_POLL)
        answer: dict[str, Any] = {"run_id": child, "status": status}
        if current is not None:
            for key in ("result", "error"):
                if current.get(key) is not None:
                    answer[key] = current[key]
        return answer


__all__ = [
    "AgentServices",
    "AgentSource",
    "ChildRuns",
    "KnowledgeSearch",
    "LazyToolset",
    "RunWorkflows",
    "SnapshotKnowledgeBases",
    "agent_node",
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
