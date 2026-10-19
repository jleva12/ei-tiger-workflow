"""A chat agent's document as a Google ADK app.

The entry agent is the app's root ``LlmAgent``. What its ``agents`` way out
connects to are its sub-agents, handed the conversation as each one's mode
says (``chat`` takes over; ``task`` does a task and hands back;
``single_turn`` answers once), and its intake agents, each a task that
collects a designed record (:mod:`.intake`); what its ``tools`` way out
connects to are its tools, a sub-agent there called like a tool
(``AgentTool``, which owns the toolsets of the agents it runs:
:class:`.tools.CalledAgent`). Sub-agents are built the same way, with tools and
sub-agents of their own.

Every LLM agent's instruction is a template filled in before each model call
(:mod:`.templating`), and runs on the model its settings or the chat pick
(:mod:`.models`). An agent whose node has an access rule checks it for
whoever the call is for as it starts, together with the rules of the agents
above it, and agents handing off to it or calling it check it first
(:mod:`.access`). A saved agent runs in the organization of the agent using
it, under the name of the node it's placed at, and a published agent never
runs a workflow's draft.

ADK calls agents by name, as tools and in hand-offs, and their guards are
kept by name, so no two agents in the whole tree, saved agents' included,
may have the same one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.load_memory_tool import load_memory_tool
from google.adk.tools.openapi_tool import OpenAPIToolset
from google.adk.tools.preload_memory_tool import preload_memory_tool
from google.genai import types

from forge_agent_runtime.access import (
    Guarded,
    Rule,
    RuleError,
    agent_guard,
    all_of,
    hand_off_guard,
    node_rule,
    task_guard,
)
from forge_agent_runtime.code_execution import CodeTool
from forge_agent_runtime.document import (
    AGENT_LIKE,
    CALLED,
    DEFAULTS,
    HANDS_OFF,
    INTAKE,
    RESERVED_NAMES,
    TOOLS,
    ChatAgentDocument,
    DocumentError,
    Node,
    parse_document,
)
from forge_agent_runtime.generative_ui import (
    CompiledComponent,
    ComponentError,
    compile_component,
    is_team_key,
)
from forge_agent_runtime.http_templates import upgraded_http_tool
from forge_agent_runtime.intake import ON_COMPLETE, Completion, IntakeDesign, IntakeEngine, IntakeRunsPlugin
from forge_agent_runtime.models import model_selection
from forge_agent_runtime.names import adk_name
from forge_agent_runtime.openapi import (
    SpecError,
    fetch_spec,
    guarded_client,
    spec_toolset,
)
from forge_agent_runtime.present import ACTING, PresentHistoryPlugin, PresentTool
from forge_agent_runtime.refs import AgentRef
from forge_agent_runtime.requests import Intake, RequestDesk, request_tools
from forge_agent_runtime.services import WORKFLOW_WAIT_MARGIN, RequestAuth, RuntimeServices
from forge_agent_runtime.skills import SkillBundle, skill_toolset
from forge_agent_runtime.tasks import TaskCallsPlugin
from forge_agent_runtime.templating import instruction_provider
from forge_agent_runtime.tools import (
    CalledAgent,
    GuardedTools,
    HttpTool,
    KnowledgeBaseTool,
    LentToolset,
    MissingSetting,
    SystemDesignTool,
    WorkflowTool,
    WorkflowTools,
    fill_references,
    header_dict,
    http_client,
)
from forge_agent_runtime.whole_state import WholeStatePlugin
from forge_common.adk.ask_person import ASK_PERSON_INSTRUCTION, ask_person, repair_input_schemas
from forge_common.adk.attachments import AttachmentsPlugin
from forge_common.adk.context_window import ContextWindowPlugin, windows_from
from forge_common.adk.follow_ups import FollowUpsPlugin
from forge_common.adk.models import ProviderModels


class BuildError(ValueError):
    """An agent ADK can't build as it is, and why."""


def _listed(callbacks: Any) -> list[Any]:
    """:return: An agent's callbacks of one kind as a list: ADK takes one, a list, or None."""
    if callbacks is None:
        return []
    return list(callbacks) if isinstance(callbacks, list) else [callbacks]


def _one_or_list(callbacks: list[Any]) -> Any:
    """:return: Callbacks as ADK takes them: None for none, one alone, or a list."""
    return callbacks[0] if len(callbacks) == 1 else callbacks or None


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


#: What a tool item may be outside a chat agent's canvas (a workflow's LLM agent's tools):
#: any kind an agent calls but an agent, which is only ever on a canvas.
TOOL_KINDS = CALLED - AGENT_LIKE


@dataclass(frozen=True)
class _Named:
    """
    An agent's ADK name, which agent it is (``key``: its document and node),
    what the person is told it's called (``title``), and where it is
    (``where``: `` in 'Billing'`` inside a saved agent placed as Billing).
    """

    name: str
    key: str
    title: str
    where: str = ""

    @property
    def label(self) -> str:
        """How build errors name it."""
        return f"{self.title!r}{self.where}"


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
        guarded: dict[str, Guarded] | None = None,
        above: Rule | None = None,
        names: dict[str, _Named] | None = None,
        where: str = "",
    ) -> None:
        self.doc = doc
        self.models = models
        self.services = services
        self.http = http
        # A saved agent runs as part of the agent using it, in its organization
        # (``organization_id``); the agent at the root is in its own.
        self.organization_id = (
            organization_id if organization_id is not None or doc is None else doc.organization_id
        )
        self.dependencies = doc.dependencies if doc is not None else dict(dependencies or {})
        # A published version runs only what's published: never a workflow's draft.
        self.published = doc is not None and isinstance(doc.version, int)
        # The rules of the agents above the one being built, which hand off
        # to it or call it: using it takes those too.
        self.above = above
        # The skills it carries, by ID; the rest are the organization's, found by services.skills.
        self.skills = doc.skills if doc is not None else {}
        # Likewise the team UI components it carries, by key (services.ui_components).
        self.ui_components = doc.ui_components if doc is not None else {}
        # The saved agents being built around this one: one using itself would never end.
        self.chain = (*chain, doc.id) if doc is not None else chain
        # The agents with access rules in the whole tree, saved agents' too,
        # by ADK name: what every agent's hand-offs and agent calls are checked against.
        self.guarded: dict[str, Guarded] = guarded if guarded is not None else {}
        self.hand_offs = hand_off_guard(self.guarded, services.access, organization_id=self.organization_id)
        # The whole tree's agents by ADK name, saved agents' too (:meth:`claim`),
        # and where this builder's nodes are, for errors: `` in 'Billing'``
        # for a saved agent placed as Billing.
        self.names: dict[str, _Named] = names if names is not None else {}
        self.where = where

    def named(self, node: Node) -> _Named:
        """:return: What one of this document's nodes is called, and which it is."""
        doc = f"{self.doc.id}@{self.doc.version}" if self.doc is not None else ""
        return _Named(node.adk_name, f"{doc}/{node.id}", node.name, self.where)

    def claim(self, named: _Named) -> str:
        """
        Give an agent its ADK name. The same agent may be built twice (a
        saved agent two others use); two that aren't can't share one.

        :return: The name.
        :raises BuildError: It isn't a name ADK takes, or another agent has it.
        """
        if not named.name:
            raise BuildError(f"{named.label} needs a letter in its name for ADK to call it by.")
        if named.name in RESERVED_NAMES:
            raise BuildError(
                f"{named.label} can't be called {named.name!r}: ADK keeps that name for the person."
            )
        taken = self.names.setdefault(named.name, named)
        if taken.key != named.key:
            raise BuildError(
                f"{taken.label} and {named.label} have the same ADK name, {named.name!r}: rename one of them."
            )
        return named.name

    def rule(self, node: Node) -> Rule | None:
        """
        :return: The access rule on an agent's node, with any the runtime
            holds it to besides (``required_rules``); None when anyone may use it.
        """
        extra = self.doc.required_rules.get(node.id) if self.doc is not None else None
        try:
            return node_rule(node.config, extra, where=f"{node.name!r}'s access")
        except RuleError as exc:
            raise BuildError(str(exc)) from exc

    @contextmanager
    def under(self, rule: Rule | None) -> Iterator[Rule | None]:
        """
        While what an agent hands off to and calls is built: the rules above
        them, the agent's own with those above it.

        :return: Those rules, which using the agent itself takes.
        """
        outer = self.above
        self.above = all_of(outer, rule)
        try:
            yield self.above
        finally:
            self.above = outer

    def guard(self, agent: LlmAgent, label: str, rule: Rule | None) -> LlmAgent:
        """
        Make an agent check a rule as it starts, answering what it needs when
        the caller doesn't hold it; agents handing off to it, or calling it,
        check that and any rule it has already.
        """
        if rule is None:
            return agent
        check = agent_guard(Guarded(label, rule), self.services.access, organization_id=self.organization_id)
        agent.before_agent_callback = [check, *_listed(agent.before_agent_callback)]
        known = self.guarded.get(agent.name)
        self.guarded[agent.name] = Guarded(label, all_of(rule, known.rule if known else None) or rule)
        return agent

    async def attached(self, node: Node) -> list[Any]:
        """What an agent's ``tools`` way out leads to, as its ADK tools."""
        assert self.doc is not None, "An agent is built from its document."
        tools: list[Any] = []
        targets = self.doc.targets(node.id, TOOLS)
        skills = [target for target in targets if target.kind == "skills"]
        rich = [target for target in targets if target.kind == "rich_answers"]
        intakes = [target for target in targets if target.kind == INTAKE]
        for target in targets:
            if target.kind not in ("skills", "rich_answers", INTAKE):
                tools.extend(await self.tool(target))
        if skills:
            # ADK's skill tools (load_skill, …) are an agent's once: its Skills
            # nodes are one toolset of all their skills.
            tools.append(await self.skill_tools(skills))
        if rich:
            # So is present: its Rich answers nodes offer all their components.
            tools.append(await self.present(rich))
        if intakes:
            # Intakes it uses as tools: it files the person's requests to them
            # itself, all with the same tools (``requests``).
            desk = RequestDesk(
                [
                    Intake(
                        id=target.id,
                        title=target.name,
                        description=str(target.config.get("description") or ""),
                        engine=await self.intake_engine(target, IntakeDesign.of(target.name, target.config)),
                    )
                    for target in intakes
                ],
                store=self.services.intake_requests,
                organization_id=self.organization_id,
            )
            tools.extend(request_tools(desk))
        return tools

    async def agent(self, node: Node, *, handed_to: bool, named: _Named | None = None) -> LlmAgent:
        """
        :param named: What it's called, when not after its own node: a saved
            agent's entry, after the node it's placed at.
        """
        assert self.doc is not None, "An agent is built from its document."
        config = node.config
        named = named or self.named(node)
        name = self.claim(named)
        what = f"{node.name}'s instruction"
        with self.under(self.rule(node)) as rule:
            tools = await self.attached(node)
            sub_agents: list[Any] = [
                await self.handed(target) for target in self.doc.targets(node.id, HANDS_OFF)
            ]
        options: dict[str, Any] = {}
        # Tasks it gives agents the caller may not use say what they need instead.
        tasks = [a.name for a in sub_agents if getattr(a, "mode", None) in ("task", "single_turn")]
        after: list[Any] = [
            task_guard(self.guarded, tasks, self.services.access, organization_id=self.organization_id)
        ]
        template = str(config.get("instruction") or "")
        if config.get("ask_person") is True and self.services.ask_person:
            # It may stop to ask the person, and the chat shows a form for the answer.
            tools.append(ask_person)
            after.append(repair_input_schemas)
            template = f"{template}\n\n{ASK_PERSON_INSTRUCTION}" if template else ASK_PERSON_INSTRUCTION
        tokens = config.get("max_output_tokens")
        if isinstance(tokens, int) and tokens > 0:
            options["generate_content_config"] = types.GenerateContentConfig(max_output_tokens=tokens)
        if node.kind == "sub_agent":
            if handed_to:
                options["mode"] = config.get("mode") or "chat"
                options["disallow_transfer_to_parent"] = bool(config.get("disallow_transfer_to_parent"))
                options["disallow_transfer_to_peers"] = bool(config.get("disallow_transfer_to_peers"))
            options["include_contents"] = config.get("include_contents") or "default"
        agent = LlmAgent(
            name=name,
            description=str(config.get("description") or ""),
            instruction=instruction_provider(template, what=what),
            model=self.models,
            before_model_callback=model_selection(self.models, config, timeout=self.services.model_timeout),
            before_tool_callback=[self.hand_offs],
            after_model_callback=_one_or_list(after if tasks else after[1:]),
            tools=tools,
            sub_agents=sub_agents,
            **options,
        )
        return self.guard(agent, named.title, rule)

    async def handed(self, node: Node) -> LlmAgent:
        """An agent handed the conversation: a sub-agent, an intake agent, or a saved agent's entry."""
        if node.kind == "saved_agent":
            return await self.saved(node, handed_to=True)
        if node.kind == INTAKE:
            return await self.intake(node)
        return await self.agent(node, handed_to=True)

    async def intake(self, node: Node) -> LlmAgent:
        """
        An intake agent: a task (``mode="task"``) an agent hands off with what
        it knows, that talks with the person until its record is done, then
        hands it back. Its engine (:class:`.intake.IntakeEngine`) keeps the
        record, tells the model where it stands before each call, and decides
        when it may finish. What its ``tools`` way out leads to, it may use
        too: Rich answers to read the record back as a table, say; the
        workflows its ``on_complete`` way leads to start when it completes.
        """
        assert self.doc is not None, "An agent is built from its document."
        config = node.config
        name = self.claim(self.named(node))
        asks = config.get("ask_person") is True and self.services.ask_person
        with self.under(self.rule(node)) as rule:
            attached = await self.attached(node)
        # Rich answers on its Tools way: it reads the summary back as a table,
        # and with controls among their components, it may ask with a form too.
        shown = next((tool for tool in attached if isinstance(tool, PresentTool)), None)
        design = IntakeDesign.of(
            node.name,
            {
                **config,
                "ask_person": asks,
                "presents": shown is not None,
                "present_forms": shown is not None and any(name in ACTING for name in shown.components),
            },
        )
        engine = await self.intake_engine(node, design)
        tools: list[Any] = [engine.tool(), *attached]
        after: list[Any] = [engine.after_model]
        template = str(config.get("instruction") or "")
        if asks:
            # Choices, dates and yes or no may be asked with a form.
            tools.append(ask_person)
            after.append(repair_input_schemas)
            template = f"{template}\n\n{ASK_PERSON_INSTRUCTION}" if template else ASK_PERSON_INSTRUCTION
        options: dict[str, Any] = {}
        tokens = config.get("max_output_tokens")
        if isinstance(tokens, int) and tokens > 0:
            options["generate_content_config"] = types.GenerateContentConfig(max_output_tokens=tokens)
        agent = LlmAgent(
            name=name,
            description=engine.description(str(config.get("description") or "")),
            instruction=instruction_provider(template, what=f"{node.name}'s instruction"),
            model=self.models,
            mode="task",
            input_schema=engine.input_schema(),
            output_schema=engine.output_schema(),
            before_model_callback=[
                model_selection(self.models, config, timeout=self.services.model_timeout),
                engine.before_model,
            ],
            after_model_callback=after,
            before_tool_callback=[self.hand_offs, engine.before_tool],
            tools=tools,
            # It hands back by finishing its task, never by transferring.
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
            **options,
        )
        return self.guard(agent, node.name, rule)

    async def intake_engine(self, node: Node, design: IntakeDesign) -> IntakeEngine:
        """An intake node's engine: its design, and the workflows on its ``on_complete`` way."""
        assert self.doc is not None, "An agent is built from its document."
        return IntakeEngine(
            design,
            node.adk_name,
            completions=[await self.completion(target) for target in self.doc.targets(node.id, ON_COMPLETE)],
            runner=self.services.workflows,
            organization_id=self.organization_id,
        )

    async def completion(self, node: Node) -> Completion:
        """A workflow an intake agent starts when it completes: its Workflow node on the ``on_complete`` way."""
        workflow_id = str(node.config.get("workflow") or "")
        if not workflow_id:
            raise BuildError(f"{node.name!r} doesn't say which workflow it runs.")
        runner = self.services.workflows
        if runner is None:
            raise BuildError(f"{node.name!r} runs a workflow, which only the hosted runtime can.")
        ref = self.workflow_ref(node, workflow_id)
        try:
            name, _, schema = await runner.describe(ref, organization_id=self.organization_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r}: {exc}") from exc
        # The record's values its start takes; all of them when it declares none.
        properties = schema.get("properties") if isinstance(schema, dict) else None
        return Completion(
            name=name or node.name,
            ref=ref,
            wait=self.services.wait_for_workflow(node.config.get("wait_seconds")),
            takes=frozenset(properties) if isinstance(properties, dict) and properties else None,
        )

    async def saved(self, node: Node, *, handed_to: bool) -> LlmAgent:
        """
        A saved agent placed at ``node``: its document's entry agent, named
        after the node, as a tool, a hand-off and a guard; the agents it
        hands off to and calls keep their own names.
        """
        agent_id = str(node.config.get("agent") or "")
        if not agent_id:
            raise BuildError(f"{node.name!r} doesn't say which agent it uses.")
        if agent_id in self.chain:
            raise BuildError(
                f"{node.name!r} uses {agent_id}, which uses this agent: they'd run each other forever."
            )
        doc = await self._resolve(agent_id, node)
        with self.under(self.rule(node)) as rule:
            inner = _Builder(
                doc,
                models=self.models,
                services=self.services,
                http=self.http,
                chain=self.chain,
                # It runs in this agent's organization, whatever its document says.
                organization_id=self.organization_id,
                guarded=self.guarded,
                above=self.above,
                names=self.names,
                where=f" in {node.name!r}{self.where}",
            )
            entry = await inner.agent(doc.entry, handed_to=handed_to, named=self.named(node))
        # Its own entry's rule holds, and so does the rule on this node.
        return self.guard(entry, node.name, rule)

    async def _resolve(self, agent_id: str, node: Node) -> ChatAgentDocument:
        # An exported agent carries what it uses; the hosted runtime finds the rest.
        for key, raw in self.dependencies.items():
            if key.split("@", 1)[0] == agent_id:
                try:
                    # Its skills and team components are carried with the agent using it.
                    carried = {**self.skills, **(raw.get("skills") or {})}
                    components = {**self.ui_components, **(raw.get("ui_components") or {})}
                    doc = parse_document(
                        {
                            **raw,
                            "dependencies": self.dependencies,
                            "skills": carried,
                            "ui_components": components,
                        }
                    )
                except DocumentError as exc:
                    raise BuildError(f"{node.name!r} uses {agent_id}, which can't be built: {exc}") from exc
                # One carried without an organization runs in this agent's; one of another's never does.
                if (
                    self.organization_id is not None
                    and doc.organization_id is not None
                    and doc.organization_id != self.organization_id
                ):
                    raise BuildError(
                        f"{node.name!r} uses {agent_id}, which isn't one of this organization's agents."
                    )
                return doc
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

    def _fill(self, text: str, *, where: str) -> str:
        """``${NAME}`` in a tool's setting, filled in from the environment."""
        services = self.services
        return fill_references(text, services.environment, where=where, note=services.environment_note)

    def _credential(self, node: Node) -> str:
        """
        :return: The organization's credential a tool signs its requests
            with; empty for none.
        :raises BuildError: It names one, and nothing here has credentials.
        """
        credential = str(node.config.get("credential") or "")
        if credential and self.services.credentials is None:
            raise BuildError(
                f"{node.name!r} uses one of the organization's credentials, which only the hosted runtime has."
            )
        return credential

    async def tool(self, node: Node) -> list[BaseTool | BaseToolset]:
        config = node.config
        guarded = self.services.tool_timeout
        match node.kind:
            case "sub_agent":
                return [CalledAgent(await self.agent(node, handed_to=False))]
            case "saved_agent":
                return [CalledAgent(await self.saved(node, handed_to=False))]
            case "memory":
                return [preload_memory_tool if config.get("mode") == "every_turn" else load_memory_tool]
            case "http_tool":
                name = adk_name(node.name)
                tool = HttpTool(
                    name=name,
                    description=str(config.get("description") or ""),
                    method=str(config.get("method") or "GET"),
                    url=str(config.get("url") or ""),
                    body=str(config.get("body") or ""),
                    query=config.get("query") or [],
                    headers=header_dict(
                        config.get("headers"),
                        self.services.environment,
                        where=f"{node.name!r}'s headers",
                        note=self.services.environment_note,
                    ),
                    parameters=config.get("parameters") or {},
                    confirm=bool(config.get("confirm")),
                    services=self.services,
                    http=self.http,
                    credential=self._credential(node),
                    organization_id=self.organization_id,
                    label=node.name,
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
                return [GuardedTools(await self.knowledge_base(node), timeout_seconds=guarded)]
            case "adk_workflow":
                return [await self.workflow(node)]
            case "code_execution":
                executor = self.services.code_executor
                if executor is None:
                    raise BuildError(f"{node.name!r} runs code, and code execution is off here.")
                runs = CodeTool(name=adk_name(node.name) or "run_code", executor=executor)
                return [GuardedTools([runs], confirm=bool(config.get("confirm")), timeout_seconds=guarded)]
            case "skills":
                return [await self.skill_tools([node])]
            case "rich_answers":
                return [await self.present([node])]
        raise BuildError(f"{node.name!r} ({node.kind}) can't be a tool.")

    async def present(self, nodes: list[Node]) -> PresentTool:
        """
        The ``present`` tool of these Rich answers nodes: the components they
        pick, together; base ones by name, team ones by key (``org.…``),
        those the document carries, then the organization's.
        """
        picked = [name for node in nodes for name in node.config.get("components") or []]
        names = ", ".join(repr(node.name) for node in nodes)
        keys = list(dict.fromkeys(name for name in picked if is_team_key(name)))
        team = await self._ui_components(keys, names) if keys else []
        try:
            return PresentTool([name for name in picked if not is_team_key(name)], team)
        except ValueError as exc:
            raise BuildError(f"{names}: {exc}.") from exc

    async def _ui_components(self, keys: list[str], names: str) -> list[CompiledComponent]:
        """:return: The team components by key, compiled: those the document carries, then the organization's."""
        carried = {key: self.ui_components[key] for key in keys if key in self.ui_components}
        rest = [key for key in keys if key not in carried]
        found: dict[str, Any] = dict(carried)
        if rest:
            library = self.services.ui_components
            if library is None:
                raise BuildError(
                    f"{names} picks the organization's team components ({', '.join(rest)}), "
                    "which only the hosted runtime has."
                )
            try:
                looked_up = await library(rest, organization_id=self.organization_id)
            except LookupError as exc:
                raise BuildError(f"{names}: {exc}") from exc
            found.update(zip(rest, looked_up, strict=True))
        compiled: list[CompiledComponent] = []
        for key in keys:
            try:
                component = compile_component(found[key])
            except ComponentError as exc:
                raise BuildError(f"{names}: the team component {key} can't be drawn: {exc}") from exc
            if component.key != key:
                raise BuildError(f"{names}: what it carries as {key} is {component.key}.")
            compiled.append(component)
        return compiled

    async def skill_tools(self, nodes: list[Node]) -> BaseToolset:
        """
        One toolset of the skills these Skills nodes name: ADK's
        ``SkillToolset``, which tells the model each skill's name and
        description and has it load one when a task calls for it. Their
        scripts run where one of them says so and code execution is on.
        """
        ids: list[str] = []
        for node in nodes:
            for skill_id in node.config.get("skills") or []:
                if skill_id and str(skill_id) not in ids:
                    ids.append(str(skill_id))
        names = ", ".join(repr(node.name) for node in nodes)
        if not ids:
            raise BuildError(f"{names} doesn't say which skills the agent has.")
        bundles = await self._skills(ids, names)
        seen: dict[str, str] = {}
        for bundle in bundles:
            if bundle.name in seen:
                raise BuildError(
                    f"{names} has two skills called {bundle.name!r}: the model tells them apart by name."
                )
            seen[bundle.name] = bundle.name
        executor = None
        if any(node.config.get("run_scripts") for node in nodes):
            executor = self.services.code_executor
            if executor is None:
                raise BuildError(f"{names} runs its skills' scripts, and code execution is off here.")
        return skill_toolset(bundles, code_executor=executor)

    async def _skills(self, ids: list[str], names: str) -> list[SkillBundle]:
        """:return: The skills by ID: those the document carries, then the organization's."""
        carried = {skill_id: self.skills[skill_id] for skill_id in ids if skill_id in self.skills}
        rest = [skill_id for skill_id in ids if skill_id not in carried]
        found: dict[str, SkillBundle] = {}
        try:
            for skill_id, raw in carried.items():
                found[skill_id] = SkillBundle.model_validate(raw)
        except ValueError as exc:
            raise BuildError(f"{names}: a skill it carries isn't one: {exc}") from exc
        if rest:
            library = self.services.skills
            if library is None:
                raise BuildError(
                    f"{names} uses the organization's skills, which only the hosted runtime has."
                )
            try:
                looked_up = await library(rest, organization_id=self.organization_id)
            except LookupError as exc:
                raise BuildError(f"{names}: {exc}") from exc
            found.update(zip(rest, looked_up, strict=True))
        return [found[skill_id] for skill_id in ids]

    async def openapi(self, node: Node) -> OpenAPIToolset:
        config = node.config
        credential = self._credential(node)
        operations = _operations(config.get("operations"))
        try:
            if config.get("source") == "inline":
                text = str(config.get("spec") or "").strip()
                if not text:
                    raise BuildError(f"{node.name!r} has no OpenAPI spec pasted in.")
            else:
                url = self._fill(str(config.get("url") or ""), where=f"{node.name!r}")
                if not url:
                    raise BuildError(f"{node.name!r} has no spec URL.")
                text = await fetch_spec(
                    url,
                    services=self.services,
                    http=self.http,
                    auth=await self._spec_auth(credential, url),
                )
            return await spec_toolset(
                text,
                operations=operations,
                client=guarded_client(
                    self.services,
                    credential=credential,
                    organization_id=self.organization_id,
                ),
            )
        except SpecError as exc:
            raise BuildError(f"{node.name!r}'s OpenAPI spec: {exc}") from exc

    async def _spec_auth(self, credential: str, url: str) -> RequestAuth | None:
        """What fetching a spec sends: the tool's credential, when it may go there."""
        provider = self.services.credentials
        if not credential or provider is None:
            return None
        try:
            return await provider(credential, organization_id=self.organization_id, url=url)
        except LookupError:
            # Not where it's sent (a spec served elsewhere): fetched without it.
            return None

    async def mcp(self, node: Node) -> BaseToolset:
        """
        An MCP tool's toolset: a server by URL, or one of the organization's.
        Either way it connects as HTTP tools' requests do (:mod:`.mcp_guard`):
        never to the runtime's own network, unless the deployment allows it.
        """
        from forge_agent_runtime.http_guard import network_policy
        from forge_common.adk import ToolFailure

        config = node.config
        # Whoever makes the toolset, its sessions check its tools' results
        # against their output schemas on the bounded engine: the client's own
        # check can freeze the process.
        from forge_common.mcp import bound_tool_results

        bound_tool_results()
        try:
            from google.adk.tools.mcp_tool import (
                McpToolset,
                SseConnectionParams,
                StreamableHTTPConnectionParams,
            )

            from forge_agent_runtime.mcp_guard import McpGuardError, check_server_url, guarded_mcp_toolset
        except ImportError as exc:  # ADK's MCP support needs the mcp package.
            raise BuildError(
                f"{node.name!r} is an MCP tool, which needs the mcp package: pip install mcp"
            ) from exc
        policy = network_policy(self.services)
        if config.get("server"):
            if self.services.mcp_servers is None:
                raise BuildError(
                    f"{node.name!r} uses one of the organization's MCP servers, which only the hosted runtime has."
                )
            try:
                made = await self.services.mcp_servers(config, organization_id=self.organization_id)
                return guarded_mcp_toolset(made, policy)
            except (LookupError, McpGuardError) as exc:
                raise BuildError(f"{node.name!r}: {exc}") from exc
        url = self._fill(str(config.get("url") or ""), where=f"{node.name!r}'s URL")
        if not url:
            raise BuildError(f"{node.name!r} has no server URL.")
        try:
            await check_server_url(url, policy)
        except ToolFailure as exc:
            raise BuildError(f"{node.name!r}'s URL: {exc}") from exc
        headers = header_dict(
            config.get("headers"),
            self.services.environment,
            where=f"{node.name!r}'s headers",
            note=self.services.environment_note,
        )
        params = (
            SseConnectionParams(url=url, headers=headers)
            if config.get("transport") == "sse"
            else StreamableHTTPConnectionParams(url=url, headers=headers)
        )
        return guarded_mcp_toolset(
            McpToolset(
                connection_params=params,
                tool_filter=_names(config.get("tools")),
                require_confirmation=bool(config.get("confirm")),
            ),
            policy,
        )

    async def knowledge_base(self, node: Node) -> list[BaseTool | BaseToolset]:
        """The search, and beside it, when a knowledge base it searches is a
        system design one, the tool that reads their design."""
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
        name = adk_name(node.name) or "search_knowledge_base"
        tools: list[BaseTool | BaseToolset] = [
            KnowledgeBaseTool(
                name=name,
                description=description,
                knowledge_base_ids=ids,
                organization_id=self.organization_id,
                max_results=max(1, min(max_results, 20)),
                knowledge_bases=searcher,
                knowledge_base_names=[searched for searched, _ in described],
            )
        ]
        try:
            designed = await searcher.design(ids, organization_id=self.organization_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r}: {exc}") from exc
        if designed is not None:
            systems = [str(s.get("knowledge_base") or "") for s in designed.get("systems") or ()]
            tools.append(
                SystemDesignTool(
                    # Model APIs take names of at most 64 characters.
                    name=f"{name[: 64 - len(DESIGN_SUFFIX)]}{DESIGN_SUFFIX}",
                    description=_design_description(systems, name),
                    knowledge_base_ids=ids,
                    organization_id=self.organization_id,
                    knowledge_bases=searcher,
                )
            )
        return tools

    def workflow_ref(self, node: Node, workflow_id: str) -> str:
        """
        :return: Which of a workflow's versions a node runs: ``ag_x`` (its
            latest published), ``ag_x@3``, ``ag_x@draft``.
        :raises BuildError: A published agent's node names a draft, which
            whoever edits the workflow changes under it.
        """
        ref = AgentRef.of(workflow_id, node.config.get("version"))
        if self.published and ref.version == "draft":
            raise BuildError(
                f"{node.name!r} runs {workflow_id}'s draft, which a published agent can't: "
                "pick one of its published versions."
            )
        return str(ref)

    async def workflow(self, node: Node) -> BaseToolset:
        workflow_id = str(node.config.get("workflow") or "")
        if not workflow_id:
            raise BuildError(f"{node.name!r} doesn't say which workflow it runs.")
        runner = self.services.workflows
        if runner is None:
            raise BuildError(f"{node.name!r} runs a workflow, which only the hosted runtime can.")
        ref = self.workflow_ref(node, workflow_id)
        try:
            name, description, schema = await runner.describe(ref, organization_id=self.organization_id)
        except LookupError as exc:
            raise BuildError(f"{node.name!r}: {exc}") from exc
        # Waited for as long as its node says, at most the deployment's cap;
        # the call may take that and a margin, so a slow run answers "still
        # running" with its ID rather than failing as too slow.
        wait = self.services.wait_for_workflow(node.config.get("wait_seconds"))
        tool = WorkflowTool(
            name=adk_name(node.name) or adk_name(name) or "workflow",
            description=description or f"Runs the workflow {name}.",
            workflow_id=ref,
            organization_id=self.organization_id,
            schema=schema,
            runner=runner,
            wait=wait,
        )
        timeout = self.services.tool_timeout
        if timeout is not None:
            timeout = max(timeout, wait + WORKFLOW_WAIT_MARGIN)
        return WorkflowTools(tool, timeout_seconds=timeout)


#: How a knowledge base tool asks for citations: chat UIs draw "[KQM4821]"
#: as a numbered source showing the passage (``@forge-ui`` ``sources``).
CITE_PASSAGES = (
    "Each passage has a ref: cite the passages an answer uses by their refs in square brackets "
    "right after what they support, e.g. [KQM4821] or [KQM4821, BTR0042], and only refs this "
    "tool returned."
)


#: What the tool reading a knowledge base tool's system designs is called
#: after it.
DESIGN_SUFFIX = "_system_design"


def _design_description(systems: list[str], search: str) -> str:
    """:return: What the tool reading system designs says it does."""
    named = ", ".join(s for s in systems if s) or "the system"
    return (
        f"Reads the system design of {named}: its applications (code repositories), the other parts "
        "of the system that aren't code here (databases, queues and topics, caches, storage, external "
        "systems, services, clients), the groups they're in and what each group is for, and how they "
        "all connect (calls, depends on, sends events to, reads from, writes to, publishes to, "
        "consumes from…), as people drew it. Use it for how the system fits together, what a part "
        "talks to and where data and messages go, before or beside searching the code with "
        f"{search}. Give focus to read one part (or a group) and what it connects to."
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


def _one_each(agent: BaseAgent) -> None:
    """
    :param agent: An agent and the agents it hands off to: one tree, where
        ADK finds who the conversation is with, and who it's handed to, by
        name. Each agent called as a tool runs on a tree of its own,
        checked the same way.
    :raises BuildError: Two agents in a tree have the same name: the copies
        of one agent, built twice by saved agents that both use it.
    """
    seen: set[str] = set()
    called: list[BaseAgent] = []

    def visit(at: BaseAgent) -> None:
        if at.name in seen:
            raise BuildError(
                f"{at.name!r} is reached twice, by two saved agents that both use it: "
                "ADK hands a conversation to an agent by its name, so it can be in one place only."
            )
        seen.add(at.name)
        for tool in getattr(at, "tools", None) or ():
            owner = tool.toolset if isinstance(tool, LentToolset) else tool
            if isinstance(owner, CalledAgent):
                called.append(owner.tool.agent)
        for sub_agent in at.sub_agents:
            visit(sub_agent)

    visit(agent)
    for each in called:
        _one_each(each)


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
    builder = _Builder(doc, models=models, services=services or RuntimeServices(), http=http or http_client())
    try:
        root = await builder.agent(doc.entry, handed_to=False)
        _one_each(root)
        return root
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
    if kind == "http_tool":
        config = upgraded_http_tool(config)
    node = Node(id=adk_name(name) or kind, kind=kind, name=name, config={**DEFAULTS[kind], **config})  # type: ignore[arg-type]
    builder = _Builder(
        None,
        models=models,
        services=services or RuntimeServices(),
        http=http or http_client(),
        organization_id=organization_id,
        dependencies=dependencies,
    )
    try:
        tools = await builder.tool(node)
        for tool in tools:
            if isinstance(tool, CalledAgent):
                _one_each(tool.tool.agent)
        return tools
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
        Its agents' context windows are kept in the state and compacted
        mid-turn as well (``forge_common.adk.context_window``, at the models'
        windows), and runs that ask get follow-up questions after a reply
        (``forge_common.adk.follow_ups``), files a chat attached are
        given to the model with their bare types and names
        (``forge_common.adk.attachments``), and the calls handing off a task
        are tagged for chats to leave open (:mod:`.tasks`), and what tool
        calls made together wrote to intakes' records and requests is kept
        whole (:mod:`.whole_state`), and the trees ``present`` drew are left
        out of model requests (:mod:`.present`), unless one of these does that.
    :return: The app, its root the entry agent.
    :raises BuildError: Something it uses can't be found or set up.
    """
    root = await build_root_agent(doc, models=models, services=services, http=http)
    if not any(isinstance(plugin, ContextWindowPlugin) for plugin in plugins):
        plugins = [ContextWindowPlugin(window=windows_from(models)), *plugins]
    if not any(isinstance(plugin, FollowUpsPlugin) for plugin in plugins):
        plugins = [*plugins, FollowUpsPlugin()]
    # Drawn trees are the chat's: no agent's model requests carry them (.present).
    if not any(isinstance(plugin, PresentHistoryPlugin) for plugin in plugins):
        plugins = [PresentHistoryPlugin(), *plugins]
    # First, so the others see the files a person attached as the model will.
    if not any(isinstance(plugin, AttachmentsPlugin) for plugin in plugins):
        plugins = [AttachmentsPlugin(), *plugins]
    # Hand-offs to tasks stay open in chats until the task hands back (.tasks).
    if not any(isinstance(plugin, TaskCallsPlugin) for plugin in plugins):
        plugins = [*plugins, TaskCallsPlugin()]
    # Records and requests calls made together wrote, kept whole (.whole_state).
    if not any(isinstance(plugin, WholeStatePlugin) for plugin in plugins):
        plugins = [*plugins, WholeStatePlugin()]
    # Where an intake's runs got to, for the agent it handed back to (.intake).
    if not any(isinstance(plugin, IntakeRunsPlugin) for plugin in plugins):
        plugins = [*plugins, IntakeRunsPlugin()]
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
