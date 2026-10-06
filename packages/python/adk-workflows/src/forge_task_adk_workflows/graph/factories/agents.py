"""
Agents: ADK's own, with their sub-agents built the same way inside them.

- ``llm``: an ``LlmAgent``. Its output schema is a Pydantic model
  (``schemas.to_model``), which ADK holds its answer to. Its answer is what it
  hands on (``steps.<id>.output``): JSON held to its output schema, else text.
- ``sequential``, ``parallel`` and ``loop_agent``: a ``SequentialAgent``,
  ``ParallelAgent`` and ``LoopAgent``. ADK's hand on nothing as graph nodes;
  Forge's hand on their answer: the last one of their agents gave (a
  parallel's: each sub-agent's, by its ID).
- ``saved``: another of the organization's agents, built the same way and run
  here whole, with what the node before handed on as its input.

An LLM agent's ``tools`` are the Agents builder's (an MCP server, a knowledge
base, an HTTP tool, an OpenAPI spec, an agent from the Agents page, a
workflow), each built the first time the agent asks the model
(``graph.agent_tools.LazyToolset``). An ``llm`` node whose ``source`` is
``agent`` is an agent from the Agents page instead, used whole
(``graph.agent_tools.agent_node``): sent its ``message`` (what it's handed
by default), its input schema's fields set from its ``inputs``.

An LLM agent's instruction is a Forge template: its ``{{ }}`` parts are
rendered from Forge's data each time it calls the model (an ADK instruction
provider), so ADK's own ``{key}`` state injection doesn't apply and a ``{``
reaches the model as it is. It reads what its node reads: the run's
``input``, ``previous`` (what its node was handed; a team's sub-agents share
their node's), ``steps``, ``state`` and a loop's item. An LLM sub-agent keeps
its answer in the session's state under its ID (``state.<id>``), for the
agents after it; a node's answer is its step's output only.
"""

import contextlib
import json
import re
import warnings
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar, Token
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.agents import (
    BaseAgent,
    LlmAgent,
    LoopAgent,
    ParallelAgent,
    SequentialAgent,
)
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.platform import time as platform_time
from google.adk.workflow import BaseNode
from google.genai import types
from pydantic import PrivateAttr

from forge_task_adk_workflows.graph.agent_tools import (
    AgentSource,
    LazyToolset,
    agent_node,
    failing_tools_first,
)
from forge_task_adk_workflows.graph.data import PRIVATE_STATE
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    config_of,
    items,
    label_of,
    schema_of,
    text,
    where_of,
)
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.schemas import held_to, to_model
from forge_task_adk_workflows.graph.uses import agent_ref
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.expressions import plain

# The model an LLM agent runs on when neither the build nor its settings
# name one: ADK's own default, and the assistant's.
DEFAULT_MODEL = "gemini-3.5-flash"
SUB_AGENT_KINDS = frozenset({"llm", "sequential", "parallel", "loop_agent"})
#: What an LLM agent's tools may be: the Agents builder's kinds.
TOOL_KINDS = frozenset({"mcp", "knowledge_base", "http_tool", "openapi", "saved_agent", "adk_workflow"})
# ADK would have a Workflow instead of a team agent, but a Workflow can't yet
# be an LLM agent's sub-agent.
_DEPRECATED = "[A-Za-z]+Agent is deprecated in favor of Workflow"
_FENCE = re.compile(r"```\w*\s*(.*?)\s*```", re.DOTALL)

#: How an agent's answer becomes data.
Parse = Callable[[str], Any]

# The agent node running now, and what it was handed: its agents' instructions
# read from them. Set while the node runs, so its sub-agents see it too.
_RUNNING: ContextVar[tuple[Context, Any] | None] = ContextVar("forge_agent_node", default=None)


def answer_parser(config: dict[str, Any]) -> Parse:
    """
    :param config: An LLM agent's settings.
    :return: How its answer (its message's text) becomes what it hands on:
        JSON held to its output schema when it has one, else the text.
    """
    schema = schema_of(config.get("output_schema"))
    if schema is None:
        return _as_text
    model = to_model(schema)

    def parse(answer: str) -> Any:
        fenced = _FENCE.fullmatch(answer.strip())
        try:
            return held_to(model, json.loads(fenced.group(1) if fenced else answer))
        except ValueError:  # not JSON, or not its schema's
            return answer

    return parse


def _as_text(answer: str) -> str:
    return answer


def llm(node: dict[str, Any], ctx: BuildContext) -> BaseNode:
    config = config_of(node)
    if config.get("source") == "agent":
        return _from_agents(node, ctx)
    return _llm(
        node,
        where_of(node),
        label_of(node),
        ctx,
        mode=config.get("mode") or "single_turn",
        step=text(node.get("id")),
    )


def _from_agents(node: dict[str, Any], ctx: BuildContext) -> BaseNode:
    """An ``llm`` node that is an agent from the Agents page."""
    config = config_of(node)
    where = where_of(node)
    agent = text(config.get("agent"))
    if not agent:
        raise AgentBuildError(f"{where}: choose the agent from the Agents page it uses.")
    version = config.get("version")
    if version is not None and version != "draft" and not (isinstance(version, int) and version > 0):
        raise AgentBuildError(f"{where}: its version is a published one's number, draft, or none for the latest.")
    inputs = config.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    return agent_node(
        name=adk_name(text(node.get("name"))),
        label=label_of(node),
        step=text(node.get("id")),
        source=AgentSource(
            ref=agent_ref(agent, version),
            message=text(config.get("message")),
            inputs={str(key): text(value) for key, value in inputs.items()},
        ),
        services=ctx.services,
        data=ctx.data,
        parse=answer_parser(config),
    )


def sequential(node: dict[str, Any], ctx: BuildContext) -> BaseAgent:
    return _team("sequential", node, where_of(node), label_of(node), ctx, text(node.get("id")))


def parallel(node: dict[str, Any], ctx: BuildContext) -> BaseAgent:
    return _team("parallel", node, where_of(node), label_of(node), ctx, text(node.get("id")))


def loop_agent(node: dict[str, Any], ctx: BuildContext) -> BaseAgent:
    return _team("loop_agent", node, where_of(node), label_of(node), ctx, text(node.get("id")))


def saved(node: dict[str, Any], ctx: BuildContext) -> Workflow:
    where = where_of(node)
    config = config_of(node)
    agent_id = text(config.get("agent"))
    if not agent_id:
        raise AgentBuildError(f"{where}: choose the agent it runs.")
    assert ctx.saved is not None
    # Which of its versions: ag_x (its latest published), ag_x@3, ag_x@draft.
    return ctx.saved(agent_ref(agent_id, config.get("version")), adk_name(text(node.get("name"))), where)


def _llm(
    item: dict[str, Any],
    where: str,
    label: str,
    ctx: BuildContext,
    *,
    mode: str | None,
    step: str | None,
) -> LlmAgent:
    config = config_of(item)
    services = ctx.services
    model = config.get("model")
    named = text(model.get("name")) if isinstance(model, dict) else ""
    settings: dict[str, Any] = {
        "name": adk_name(text(item.get("name"))),
        "description": text(config.get("description")),
        "instruction": _instruction(text(config.get("instruction")), label, ctx),
        "model": services.model or named or DEFAULT_MODEL,
        "include_contents": config.get("include_contents") or "default",
        "disallow_transfer_to_parent": bool(config.get("disallow_transfer_to_parent")),
        "disallow_transfer_to_peers": bool(config.get("disallow_transfer_to_peers")),
        "sub_agents": _sub_agents(config, where, label, ctx, step),
        "tools": _tools(config, where, ctx, step),
    }
    if mode:
        settings["mode"] = mode
    else:
        # A sub-agent: its answer is kept in the state under its ID.
        settings["output_key"] = text(item.get("id"))
    output_schema = schema_of(config.get("output_schema"))
    if output_schema:
        settings["output_schema"] = to_model(output_schema)
    if config.get("max_output_tokens") is not None:
        settings["generate_content_config"] = types.GenerateContentConfig(max_output_tokens=config["max_output_tokens"])
    # The thinking level is the model callbacks' part.
    callback = services.model_callbacks(config) if services.model_callbacks is not None else None
    if settings["tools"]:
        settings["before_model_callback"] = failing_tools_first(settings["tools"], callback)
    elif callback is not None:
        settings["before_model_callback"] = callback
    return ForgeLlmAgent(**settings)


def _tools(config: dict[str, Any], where: str, ctx: BuildContext, step: str | None) -> list[LazyToolset]:
    """An LLM agent's tools, each built when it's first used; checked here for what they name."""
    tools: list[LazyToolset] = []
    ids: set[str] = set()
    for tool in items(config.get("tools")):
        if not isinstance(tool, dict):
            continue
        kind, name = text(tool.get("kind")), text(tool.get("name")) or text(tool.get("id"))
        at = f"{where}, tool '{name}'"
        if kind not in TOOL_KINDS:
            raise AgentBuildError(f"{at}: Forge has no kind of tool {kind!r}.")
        tool_id = text(tool.get("id"))
        if not tool_id or tool_id in ids:
            raise AgentBuildError(f"{at}: every tool needs an ID of its own.")
        ids.add(tool_id)
        settings = config_of(tool)
        if kind == "http_tool" and not adk_name(name):
            raise AgentBuildError(f"{at}: the model calls it by its name, which needs a letter.")
        needs = {
            "saved_agent": ("agent", "choose the agent it calls"),
            "adk_workflow": ("workflow", "choose the workflow it runs"),
            "knowledge_base": ("knowledge_bases", "choose the knowledge bases it searches"),
            "http_tool": ("url", "give the URL it calls"),
        }.get(kind)
        if needs is not None and not settings.get(needs[0]):
            raise AgentBuildError(f"{at}: {needs[1]}.")
        if kind == "mcp" and not (settings.get("server") or settings.get("url")):
            raise AgentBuildError(f"{at}: choose one of the organization's MCP servers, or give its URL.")
        tools.append(LazyToolset(tool, where=at, step=step, services=ctx.services))
    return tools


def _instruction(template: str, label: str, ctx: BuildContext) -> Callable[[ReadonlyContext], str]:
    # An LLM agent's instruction, rendered from Forge's data for each call.
    evaluator = ctx.services.evaluator

    def render(readonly: ReadonlyContext) -> str:
        running = _RUNNING.get()
        state = {key: value for key, value in readonly.state.items() if not key.startswith(PRIVATE_STATE)}
        if running is None:
            data: dict[str, Any] = {"input": state.get("input"), "previous": None, "steps": {}}
            data.update(ctx.bindings)
        else:
            data = ctx.data(*running)
        data["state"] = plain(state)
        try:
            return evaluator.render(template, data, what="Its instruction")
        except StepFailed as error:
            raise RunFailed(f"{label} failed: {error.message}") from None

    return render


def _team(
    kind: str, item: dict[str, Any], where: str, label: str, ctx: BuildContext, step: str | None = None
) -> BaseAgent:
    config = config_of(item)
    settings: dict[str, Any] = {
        "name": adk_name(text(item.get("name"))),
        "description": text(config.get("description")),
        "sub_agents": _sub_agents(config, where, label, ctx, step),
    }
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", _DEPRECATED)
        if kind == "sequential":
            team: _Answering = SequentialTeam(**settings)
        elif kind == "parallel":
            team = ParallelTeam(**settings)
        else:
            team = LoopTeam(**settings, max_iterations=config.get("max_iterations"))
    team._answers = {
        name: (sub_id, parse)
        for sub_agent in items(config.get("sub_agents"))
        if isinstance(sub_agent, dict)
        for sub_id in [text(sub_agent.get("id"))]
        for name, parse in _parsers(sub_agent).items()
    }
    return team


def _parsers(item: dict[str, Any]) -> dict[str, Parse]:
    # How the answers of an agent and those inside it become data, by ADK name.
    config = config_of(item)
    found: dict[str, Parse] = {}
    if item.get("kind") == "llm":
        found[adk_name(text(item.get("name")))] = answer_parser(config)
    for sub_agent in items(config.get("sub_agents")):
        if isinstance(sub_agent, dict):
            found.update(_parsers(sub_agent))
    return found


def _sub_agents(
    config: dict[str, Any], where: str, label: str, ctx: BuildContext, step: str | None = None
) -> list[BaseAgent]:
    agents: list[BaseAgent] = []
    for agent in items(config.get("sub_agents")):
        if not isinstance(agent, dict):
            continue
        name = text(agent.get("name")) or text(agent.get("id"))
        at = f"{where}, sub-agent '{name}'"
        inner = f"{label}, sub-agent {name} ({text(agent.get('id'))})"
        kind = agent.get("kind")
        if kind not in SUB_AGENT_KINDS:
            raise AgentBuildError(f"{at}: Forge has no kind of sub-agent {kind!r}.")
        try:
            if kind == "llm":
                agents.append(_llm(agent, at, inner, ctx, mode=None, step=step))
            else:
                agents.append(_team(kind, agent, at, inner, ctx, step))
        except AgentBuildError:
            raise
        except ValueError as error:
            raise AgentBuildError(f"{at}: ADK refuses it: {error}") from None
    return agents


def _stamped(event: Any) -> Any:
    # ADK stamps a model's answer when it asks the model, not when the answer
    # comes: stamped as it's handed on instead, a run's steps show how long
    # the agent took.
    if isinstance(event, Event):
        event.timestamp = max(event.timestamp, platform_time.get_time())
    return event


def _reset(token: Token[tuple[Context, Any] | None]) -> None:
    # A generator closed from another context leaves that context's alone.
    with contextlib.suppress(ValueError):
        _RUNNING.reset(token)


class ForgeLlmAgent(LlmAgent):
    """ADK's ``LlmAgent``; as a graph node, what it's handed is what its
    instruction reads as ``previous``, and its sub-agents' too."""

    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        token = _RUNNING.set((ctx, node_input))
        try:
            async for event in super()._run_impl(ctx=ctx, node_input=node_input):
                yield _stamped(event)
        finally:
            _reset(token)


class _Answering(BaseAgent):
    """A team agent that, as a graph node, hands on its agents' answer, and
    whose agents' instructions read what it was handed as ``previous``."""

    # Its agents' ADK names: the ID of the sub-agent each is (or is in), and
    # how its answer becomes data.
    _answers: dict[str, tuple[str, Parse]] = PrivateAttr(default_factory=dict)
    _parallel: bool = False

    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        answers: dict[str, Any] = {}
        last: Any = None
        token = _RUNNING.set((ctx, node_input))
        try:
            async for event in super()._run_impl(ctx=ctx, node_input=node_input):
                answer = self._answer(event)
                if answer is not None:
                    sub_id, value = answer
                    answers[sub_id] = last = value
                yield _stamped(event)
        finally:
            _reset(token)
        output = answers if self._parallel else last
        if output is not None and output != {}:
            yield Event(output=output)

    def _answer(self, event: Event) -> tuple[str, Any] | None:
        known = self._answers.get(event.author)
        content = event.content
        if known is None or event.partial or not content or content.role != "model":
            return None
        parts = content.parts or []
        if any(part.function_call for part in parts):
            return None
        answer = "".join(part.text for part in parts if part.text and not part.thought)
        if not answer.strip():
            return None
        sub_id, parse = known
        return sub_id, parse(answer)


with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    class SequentialTeam(_Answering, SequentialAgent):
        """ADK's ``SequentialAgent``, handing on the last answer."""

    class ParallelTeam(_Answering, ParallelAgent):
        """ADK's ``ParallelAgent``, handing on each sub-agent's answer by its ID."""

        _parallel: bool = True

    class LoopTeam(_Answering, LoopAgent):
        """ADK's ``LoopAgent``, handing on the last answer."""
