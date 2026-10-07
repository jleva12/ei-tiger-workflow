"""A chat agent's document as a Google ADK app.

The entry agent is the app's root ``LlmAgent``. What its ``agents`` way out
connects to are its sub-agents, handed the conversation as each one's mode
says (``chat`` takes over; ``task`` does a task and hands back;
``single_turn`` answers once); what its ``tools`` way out connects to are its
tools, a sub-agent there called like a tool (``AgentTool``). Sub-agents are
built the same way, with tools and sub-agents of their own.

Every LLM agent's instruction is a template filled in before each model call
(:mod:`.templating`), and runs on the model its settings or the chat pick
(:mod:`.models`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.load_memory_tool import load_memory_tool
from google.adk.tools.openapi_tool import OpenAPIToolset
from google.adk.tools.preload_memory_tool import preload_memory_tool
from google.genai import types

from forge_agent_runtime.document import (
    DEFAULTS,
    HANDS_OFF,
    TOOLS,
    ChatAgentDocument,
    DocumentError,
    Node,
    parse_document,
)
from forge_agent_runtime.models import model_selection
from forge_agent_runtime.names import adk_name
from forge_agent_runtime.refs import AgentRef
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.templating import instruction_provider
from forge_agent_runtime.tools import (
    GuardedTools,
    HttpTool,
    KnowledgeBaseTool,
    MissingSetting,
    WorkflowTool,
    fill_references,
    header_dict,
)
from forge_common.adk.models import ProviderModels


class BuildError(ValueError):
    """An agent ADK can't build as it is, and why."""


def _names(value: Any) -> list[str] | None:
    """:return: The names in a comma-separated list; None (all) for none."""
    names = [name.strip() for name in str(value or "").split(",") if name.strip()]
    return names or None


def _snake(text: str) -> str:
    """An operation ID as ADK names its tool: ``getInvoice``: ``get_invoice``."""
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.lower().strip("_")


def _operations(value: Any) -> list[str] | None:
    """:return: The operations an OpenAPI tool may call, by ID or by the tool name ADK gives each."""
    names = _names(value)
    return sorted({form for name in names for form in (name, _snake(name))}) if names else None


#: What a tool item may be outside a chat agent's canvas (a workflow's LLM agent's tools).
TOOL_KINDS = frozenset(
    {"saved_agent", "adk_workflow", "memory", "knowledge_base", "http_tool", "openapi", "mcp"}
)


class _Builder:
    def __init__(
        self,
        doc: ChatAgentDocument | None,
        *,
        models: ProviderModels,
        services: RuntimeServices,
        http: httpx.AsyncClient,
        chain: tuple[str, ...] = (),
        organization_id: str | None = None,
        dependencies: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.doc = doc
        self.models = models
        self.services = services
        self.http = http
        self.organization_id = doc.organization_id if doc is not None else organization_id
        self.dependencies = doc.dependencies if doc is not None else dict(dependencies or {})
        # The saved agents being built around this one: one using itself would never end.
        self.chain = (*chain, doc.id) if doc is not None else chain

    async def agent(self, node: Node, *, handed_to: bool) -> LlmAgent:
        assert self.doc is not None, "An agent is built from its document."
        config = node.config
        name = node.adk_name
        what = f"{node.name}'s instruction"
        tools: list[Any] = []
        for target in self.doc.targets(node.id, TOOLS):
            tools.extend(await self.tool(target))
        sub_agents: list[Any] = [await self.handed(target) for target in self.doc.targets(node.id, HANDS_OFF)]
        options: dict[str, Any] = {}
        tokens = config.get("max_output_tokens")
        if isinstance(tokens, int) and tokens > 0:
            options["generate_content_config"] = types.GenerateContentConfig(max_output_tokens=tokens)
        if node.kind == "sub_agent":
            if handed_to:
                options["mode"] = config.get("mode") or "chat"
                options["disallow_transfer_to_parent"] = bool(config.get("disallow_transfer_to_parent"))
                options["disallow_transfer_to_peers"] = bool(config.get("disallow_transfer_to_peers"))
            options["include_contents"] = config.get("include_contents") or "default"
        return LlmAgent(
            name=name,
            description=str(config.get("description") or ""),
            instruction=instruction_provider(str(config.get("instruction") or ""), what=what),
            model=self.models,
            before_model_callback=model_selection(self.models, config, timeout=self.services.model_timeout),
            tools=tools,
            sub_agents=sub_agents,
            **options,
        )

    async def handed(self, node: Node) -> LlmAgent:
        """An agent handed the conversation: a sub-agent, or a saved agent's entry."""
        if node.kind == "saved_agent":
            return await self.saved(node, handed_to=True)
        return await self.agent(node, handed_to=True)

    async def saved(self, node: Node, *, handed_to: bool) -> LlmAgent:
        agent_id = str(node.config.get("agent") or "")
        if not agent_id:
            raise BuildError(f"{node.name!r} doesn't say which agent it uses.")
        if agent_id in self.chain:
            raise BuildError(
                f"{node.name!r} uses {agent_id}, which uses this agent: they'd run each other forever."
            )
        doc = await self._resolve(agent_id, node)
        inner = _Builder(doc, models=self.models, services=self.services, http=self.http, chain=self.chain)
        return await inner.agent(doc.entry, handed_to=handed_to)

    async def _resolve(self, agent_id: str, node: Node) -> ChatAgentDocument:
        # An exported agent carries what it uses; the hosted runtime finds the rest.
        for key, raw in self.dependencies.items():
            if key.split("@", 1)[0] == agent_id:
                try:
                    return parse_document({**raw, "dependencies": self.dependencies})
                except DocumentError as exc:
                    raise BuildError(f"{node.name!r} uses {agent_id}, which can't be built: {exc}") from exc
        if self.services.resolve_agent is None:
            raise BuildError(
                f"{node.name!r} uses the saved agent {agent_id}, which isn't in this agent's dependencies."
            )
        try:
            doc = await self.services.resolve_agent(agent_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r} uses {agent_id}: {exc}") from exc
        # It runs with its own organization's MCP servers, knowledge bases and
        # workflows: another organization's agent would hand this one theirs.
        if self.organization_id is not None and doc.organization_id != self.organization_id:
            raise BuildError(f"{node.name!r} uses {agent_id}, which isn't one of this organization's agents.")
        return doc

    async def tool(self, node: Node) -> list[BaseTool | BaseToolset]:
        config = node.config
        env = self.services.environment
        guarded = self.services.tool_timeout
        match node.kind:
            case "sub_agent":
                return [AgentTool(await self.agent(node, handed_to=False))]
            case "saved_agent":
                return [AgentTool(await self.saved(node, handed_to=False))]
            case "memory":
                return [preload_memory_tool if config.get("mode") == "every_turn" else load_memory_tool]
            case "http_tool":
                name = adk_name(node.name)
                tool = HttpTool(
                    name=name,
                    description=str(config.get("description") or ""),
                    method=str(config.get("method") or "GET"),
                    url=fill_references(str(config.get("url") or ""), env, where=f"{node.name!r}'s URL"),
                    headers=header_dict(config.get("headers"), env, where=f"{node.name!r}'s headers"),
                    parameters=config.get("parameters") or {},
                    confirm=bool(config.get("confirm")),
                    services=self.services,
                    http=self.http,
                )
                return [GuardedTools([tool], timeout_seconds=guarded)]
            case "openapi":
                return [
                    GuardedTools(
                        [await self.openapi(node)],
                        confirm=bool(config.get("confirm")),
                        timeout_seconds=guarded,
                    )
                ]
            case "mcp":
                return [GuardedTools([await self.mcp(node)], timeout_seconds=guarded)]
            case "knowledge_base":
                return [GuardedTools([await self.knowledge_base(node)], timeout_seconds=guarded)]
            case "adk_workflow":
                return [await self.workflow(node)]
        raise BuildError(f"{node.name!r} ({node.kind}) can't be a tool.")

    async def openapi(self, node: Node) -> OpenAPIToolset:
        config = node.config
        operations = _operations(config.get("operations"))
        if config.get("source") == "inline":
            spec = str(config.get("spec") or "").strip()
            if not spec:
                raise BuildError(f"{node.name!r} has no OpenAPI spec pasted in.")
        else:
            url = fill_references(
                str(config.get("url") or ""), self.services.environment, where=f"{node.name!r}"
            )
            if not url:
                raise BuildError(f"{node.name!r} has no spec URL.")
            try:
                response = await self.http.get(url, timeout=self.services.http_timeout)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise BuildError(f"{node.name!r} couldn't fetch its spec from {url}: {exc}") from exc
            spec = response.text
        kind: Literal["json", "yaml"] = "json" if spec.lstrip().startswith("{") else "yaml"
        try:
            return OpenAPIToolset(spec_str=spec, spec_str_type=kind, tool_filter=operations)
        except Exception as exc:  # The parser raises whatever the spec trips it on.
            raise BuildError(f"{node.name!r}'s OpenAPI spec can't be read: {exc}") from exc

    async def mcp(self, node: Node) -> BaseToolset:
        config = node.config
        if config.get("server"):
            if self.services.mcp_servers is None:
                raise BuildError(
                    f"{node.name!r} uses one of the organization's MCP servers, which only the hosted runtime has."
                )
            try:
                return await self.services.mcp_servers(config, organization_id=self.organization_id)
            except LookupError as exc:
                raise BuildError(f"{node.name!r}: {exc}") from exc
        try:
            from google.adk.tools.mcp_tool import (
                McpToolset,
                SseConnectionParams,
                StreamableHTTPConnectionParams,
            )
        except ImportError as exc:  # ADK's MCP support needs the mcp package.
            raise BuildError(
                f"{node.name!r} is an MCP tool, which needs the mcp package: pip install mcp"
            ) from exc
        env = self.services.environment
        url = fill_references(str(config.get("url") or ""), env, where=f"{node.name!r}'s URL")
        if not url:
            raise BuildError(f"{node.name!r} has no server URL.")
        headers = header_dict(config.get("headers"), env, where=f"{node.name!r}'s headers")
        params = (
            SseConnectionParams(url=url, headers=headers)
            if config.get("transport") == "sse"
            else StreamableHTTPConnectionParams(url=url, headers=headers)
        )
        return McpToolset(
            connection_params=params,
            tool_filter=_names(config.get("tools")),
            require_confirmation=bool(config.get("confirm")),
        )

    async def knowledge_base(self, node: Node) -> KnowledgeBaseTool:
        config = node.config
        ids = [str(i) for i in config.get("knowledge_bases") or [] if i]
        if not ids:
            raise BuildError(f"{node.name!r} doesn't say which knowledge base it searches.")
        searcher = self.services.knowledge_bases
        if searcher is None:
            raise BuildError(
                f"{node.name!r} searches the organization's knowledge bases, which only the hosted runtime has."
            )
        try:
            described = await searcher.describe(ids, organization_id=self.organization_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r}: {exc}") from exc
        description = str(config.get("description") or "").strip() or _knowledge_description(described)
        # Its own description or not, the model cites what it answers from.
        description = f"{description} {CITE_PASSAGES}"
        try:
            max_results = int(config.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        return KnowledgeBaseTool(
            name=adk_name(node.name) or "search_knowledge_base",
            description=description,
            knowledge_base_ids=ids,
            organization_id=self.organization_id,
            max_results=max(1, min(max_results, 20)),
            knowledge_bases=searcher,
            knowledge_base_names=[name for name, _ in described],
        )

    async def workflow(self, node: Node) -> BaseToolset:
        workflow_id = str(node.config.get("workflow") or "")
        if not workflow_id:
            raise BuildError(f"{node.name!r} doesn't say which workflow it runs.")
        runner = self.services.workflows
        if runner is None:
            raise BuildError(f"{node.name!r} runs a workflow, which only the hosted runtime can.")
        # Which of its versions: ag_x (its latest published), ag_x@3, ag_x@draft.
        ref = str(AgentRef.of(workflow_id, node.config.get("version")))
        try:
            name, description, schema = await runner.describe(ref, organization_id=self.organization_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r}: {exc}") from exc
        tool = WorkflowTool(
            name=adk_name(node.name) or adk_name(name) or "workflow",
            description=description or f"Runs the workflow {name}.",
            workflow_id=ref,
            organization_id=self.organization_id,
            schema=schema,
            runner=runner,
        )
        return GuardedTools([tool], timeout_seconds=self.services.tool_timeout)


#: How a knowledge base tool asks for citations: chat UIs draw "[KQM4821]"
#: as a numbered source showing the passage (``@forge-ui`` ``sources``).
CITE_PASSAGES = (
    "Each passage has a ref: cite the passages an answer uses by their refs in square brackets "
    "right after what they support, e.g. [KQM4821] or [KQM4821, BTR0042], and only refs this "
    "tool returned."
)


def _knowledge_description(described: list[tuple[str, str]]) -> str:
    """:return: What a knowledge base tool says it does, from the knowledge bases it searches."""
    parts = [f"{name} ({about.strip()})" if about.strip() else name for name, about in described]
    several = len(parts) > 1
    return (
        f"Searches the knowledge base{'s' if several else ''} {'; '.join(parts)} for passages "
        "that answer a question: of documents, or of code (each a declaration, with its file). "
        "Use it before answering from what they hold."
        + (" Each passage names the knowledge base it's from." if several else "")
    )


async def build_root_agent(
    doc: ChatAgentDocument,
    *,
    models: ProviderModels,
    services: RuntimeServices | None = None,
    http: httpx.AsyncClient | None = None,
) -> LlmAgent:
    """
    :return: The agent's entry ``LlmAgent``, with its tools and the agents it
        hands off to: what :func:`build_app` makes the app's root, for running
        it elsewhere (a workflow's step).
    :raises BuildError: Something it uses can't be found or set up.
    """
    builder = _Builder(
        doc, models=models, services=services or RuntimeServices(), http=http or httpx.AsyncClient()
    )
    try:
        return await builder.agent(doc.entry, handed_to=False)
    except MissingSetting as exc:
        raise BuildError(str(exc)) from exc
    except ValueError as exc:  # ADK refusing the tree it's given, e.g. a name used twice.
        if isinstance(exc, BuildError):
            raise
        raise BuildError(str(exc)) from exc


async def build_tools(
    name: str,
    kind: str,
    config: Mapping[str, Any],
    *,
    models: ProviderModels,
    services: RuntimeServices | None = None,
    http: httpx.AsyncClient | None = None,
    organization_id: str | None = None,
    dependencies: Mapping[str, dict[str, Any]] | None = None,
) -> list[BaseTool | BaseToolset]:
    """
    One tool, as a chat agent's tool node would be, for an agent built
    elsewhere (a workflow's LLM agent): its kind's settings, with what they
    lack at their defaults.

    :param name: What the tool is called (an HTTP tool's name for the model).
    :param kind: One of :data:`TOOL_KINDS`.
    :param organization_id: Whose MCP servers, knowledge bases and workflows it may use.
    :param dependencies: The saved agents it may use, by ``"<id>@<version>"``;
        others are found with ``services.resolve_agent``.
    :raises BuildError: It isn't a tool, or what it uses can't be found or set up.
    """
    if kind not in TOOL_KINDS:
        raise BuildError(f"{name!r} ({kind}) can't be a tool.")
    node = Node(id=adk_name(name) or kind, kind=kind, name=name, config={**DEFAULTS[kind], **config})  # type: ignore[arg-type]
    builder = _Builder(
        None,
        models=models,
        services=services or RuntimeServices(),
        http=http or httpx.AsyncClient(),
        organization_id=organization_id,
        dependencies=dependencies,
    )
    try:
        return await builder.tool(node)
    except MissingSetting as exc:
        raise BuildError(str(exc)) from exc
    except ValueError as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"{name!r}: {exc}") from exc


async def build_app(
    doc: ChatAgentDocument,
    *,
    models: ProviderModels,
    services: RuntimeServices | None = None,
    http: httpx.AsyncClient | None = None,
    app_name: str | None = None,
    plugins: Sequence[BasePlugin] = (),
) -> App:
    """
    :param doc: The chat agent.
    :param models: The models its agents run on.
    :param services: What building and running it may use.
    :param http: The client HTTP tools and spec fetches send with; one is made otherwise.
    :param app_name: The app's name: the agent's ID by default. Sessions are kept per app.
    :param plugins: ADK plugins for the app, e.g. one that records what it uses.
    :return: The app, its root the entry agent.
    :raises BuildError: Something it uses can't be found or set up.
    """
    root = await build_root_agent(doc, models=models, services=services, http=http)
    return App(name=app_name or doc.id, root_agent=root, plugins=list(plugins))


def describe(doc: ChatAgentDocument) -> Mapping[str, Any]:
    """:return: What the agent is, for listings: its ID, name, version and entry agent."""
    return {
        "id": doc.id,
        "name": doc.name,
        "description": doc.description,
        "version": doc.version,
        "entry": doc.entry.adk_name,
        "state_schema": json.loads(json.dumps(doc.state_schema)),
    }
