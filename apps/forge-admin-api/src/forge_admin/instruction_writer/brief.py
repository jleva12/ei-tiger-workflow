"""The brief the instruction writer works from: the agent being set up.

The web console's builders send it as they hold the agent (saved or not):
where it runs, its tools with the settings that say what they do, the agents
it hands off to and the one above it, and the values its instruction can put
in with ``{{ }}``. :func:`resolve` fills in what they only have IDs for, from
the organization's own records: an MCP server's tools (as last listed), a
knowledge base's name and description, and an agent's or workflow's name,
description and what it takes. :func:`render` writes it all out for the
model, each tool under the name the model will call it by
(``forge_agent_runtime.build``'s naming), so the instruction can name them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from forge_agent_runtime.names import adk_name
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.models import KnowledgeBase, McpServer
from forge_admin.models.knowledge import SYSTEM

logger = logging.getLogger(__name__)

ToolKind = Literal[
    "memory",
    "knowledge_base",
    "http_tool",
    "openapi",
    "mcp",
    "saved_agent",
    "adk_workflow",
    "code_execution",
    "sub_agent",
]

Short = Annotated[str, StringConstraints(max_length=200)]
Text = Annotated[str, StringConstraints(max_length=4_000)]
Ref = Annotated[str, StringConstraints(max_length=64)]

# How much of what's described goes to the model: a tool's description, a
# JSON Schema, an instruction's ask.
DESCRIPTION_CHARS = 600
SCHEMA_CHARS = 3_000
# The most of an MCP server's tools listed by name.
MOST_SERVER_TOOLS = 60


class BriefTool(BaseModel):
    """One of the agent's tools, with the settings that say what it does."""

    model_config = ConfigDict(extra="ignore")

    kind: ToolKind
    name: Short
    #: The tool's settings as the builder holds them, less anything that
    #: signs it in (the web console leaves those out).
    config: dict[str, Any] = Field(default_factory=dict)


class BriefAgent(BaseModel):
    """An agent it hands off to, or that hands off to it."""

    model_config = ConfigDict(extra="ignore")

    #: A workflow's sub-agent's ID: its answer is kept as ``state.<id>``.
    id: Ref | None = None
    name: Short
    description: Text = ""
    #: agent, sub_agent, saved_agent; llm, sequential, parallel, loop_agent.
    kind: Annotated[str, StringConstraints(max_length=32)] = "sub_agent"
    #: chat, task, single_turn; tool when it's called as one.
    mode: Annotated[str, StringConstraints(max_length=32)] | None = None
    #: An agent from the Agents page, by ID.
    agent: Ref | None = None


class BriefReference(BaseModel):
    """A value the instruction can put in with ``{{ }}``."""

    model_config = ConfigDict(extra="ignore")

    path: Annotated[str, StringConstraints(max_length=300)]
    type: Annotated[str, StringConstraints(max_length=200)] = "any"
    description: Annotated[str, StringConstraints(max_length=500)] | None = None


class Brief(BaseModel):
    """The agent the instruction is for."""

    model_config = ConfigDict(extra="ignore")

    #: A chat on the Agents page, or a step of a workflow.
    where: Literal["chat", "workflow"]
    role: Literal["chat_agent", "sub_agent", "workflow_agent", "workflow_sub_agent"]
    name: Short
    description: Text = ""
    #: The Agents page's agent, or the workflow, it's part of.
    container: Short = ""
    #: A sub-agent's hand-off mode; a workflow node's single_turn or task.
    mode: Annotated[str, StringConstraints(max_length=32)] | None = None
    ask_person: bool = False
    sees_only_given: bool = False
    output_schema: dict[str, Any] | None = None
    #: It has the tools that load and save the run's files.
    files: bool = False
    tools: list[BriefTool] = Field(default_factory=list, max_length=50)
    hand_offs: list[BriefAgent] = Field(default_factory=list, max_length=30)
    parent: BriefAgent | None = None
    references: list[BriefReference] = Field(default_factory=list, max_length=150)


# --------------------------------------------------------------------------
# What the organization's records add
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SavedAgent:
    """An agent from the Agents page, as its document has it."""

    name: str
    #: Its entry agent's ADK name: what it's called by, as a tool or a hand-off.
    adk_name: str
    description: str


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    input_schema: dict[str, Any] | None


@dataclass
class Resolved:
    """What the organization's records say about the IDs in a brief."""

    #: Its MCP servers, by ID: name, description and tools as last listed.
    servers: dict[str, McpServer] = field(default_factory=dict)
    knowledge_bases: dict[str, KnowledgeBase] = field(default_factory=dict)
    agents: dict[str, SavedAgent] = field(default_factory=dict)
    workflows: dict[str, Workflow] = field(default_factory=dict)


def _ids(values: Iterable[object]) -> set[str]:
    return {value for value in values if isinstance(value, str) and value}


async def resolve(
    brief: Brief,
    organization_id: str,
    session: AsyncSession,
    *,
    chat_agents: Any = None,
    workflows: Any = None,
) -> Resolved:
    """
    :param brief: The brief.
    :param organization_id: The organization: only its records are read.
    :param session: The request's database session.
    :param chat_agents: The chat agents' store (``ChatAgentStore``); None
        without MongoDB, when agents go by the names the brief gives.
    :param workflows: The workflows' store (``AgentStore``); likewise.
    :return: What its IDs are. One that can't be read is left out.
    """
    found = Resolved()
    servers = _ids(t.config.get("server") for t in brief.tools if t.kind == "mcp")
    if servers:
        rows = await session.scalars(
            select(McpServer).where(
                McpServer.organization_id == organization_id, McpServer.id.in_(servers)
            )
        )
        found.servers = {row.id: row for row in rows}
    bases = _ids(
        base
        for t in brief.tools
        if t.kind == "knowledge_base"
        for base in _list(t.config.get("knowledge_bases"))
    )
    if bases:
        rows = await session.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.organization_id == organization_id,
                KnowledgeBase.id.in_(bases),
            )
        )
        found.knowledge_bases = {row.id: row for row in rows}
    agents = _ids(
        [t.config.get("agent") for t in brief.tools if t.kind == "saved_agent"]
        + [a.agent for a in brief.hand_offs if a.kind == "saved_agent"]
    )
    for agent_id in sorted(agents)[:20] if chat_agents is not None else ():
        document = await _document(chat_agents, organization_id, agent_id)
        if document is not None:
            found.agents[agent_id] = _saved_agent(document)
    flows = _ids(
        t.config.get("workflow") for t in brief.tools if t.kind == "adk_workflow"
    )
    for workflow_id in sorted(flows)[:20] if workflows is not None else ():
        document = await _document(workflows, organization_id, workflow_id)
        if document is not None:
            found.workflows[workflow_id] = _workflow(document)
    return found


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


async def _document(
    store: Any, organization_id: str, document_id: str
) -> Mapping[str, Any] | None:
    """A chat agent's or workflow's document, or None when it can't be read."""
    try:
        record = await store.get(organization_id, document_id)
    except Exception:
        logger.warning(
            "Couldn't read %s for the instruction writer", document_id, exc_info=True
        )
        return None
    document = record.get("document") if isinstance(record, Mapping) else None
    return document if isinstance(document, Mapping) else None


def _saved_agent(document: Mapping[str, Any]) -> SavedAgent:
    """An Agents-page agent: its entry agent (the ``agent`` node) is what's called."""
    nodes = _list(document.get("nodes"))
    entry = next(
        (n for n in nodes if isinstance(n, Mapping) and n.get("kind") == "agent"), None
    )
    config = entry.get("config") if isinstance(entry, Mapping) else None
    entry_name = str(entry.get("name", "")) if isinstance(entry, Mapping) else ""
    description = config.get("description") if isinstance(config, Mapping) else ""
    return SavedAgent(
        name=str(document.get("name", "")),
        adk_name=adk_name(entry_name),
        description=str(description or document.get("description") or ""),
    )


def _workflow(document: Mapping[str, Any]) -> Workflow:
    start = next(
        (
            n
            for n in _list(document.get("nodes"))
            if isinstance(n, Mapping) and n.get("kind") == "start"
        ),
        None,
    )
    config = start.get("config") if isinstance(start, Mapping) else None
    schema = config.get("input_schema") if isinstance(config, Mapping) else None
    return Workflow(
        name=str(document.get("name", "")),
        description=str(document.get("description", "")),
        input_schema=schema if isinstance(schema, dict) and schema else None,
    )


# --------------------------------------------------------------------------
# Writing it out
# --------------------------------------------------------------------------


def _clip(text: object, most: int = DESCRIPTION_CHARS) -> str:
    said = " ".join(str(text or "").split())
    return said if len(said) <= most else said[: most - 1] + "…"


def _schema(schema: object) -> str:
    text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= SCHEMA_CHARS else text[: SCHEMA_CHARS - 1] + "…"


def _fields(schema: object) -> str:
    """A JSON Schema's top-level fields: ``name (type, required): description``."""
    if not isinstance(schema, Mapping):
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return ""
    required = set(_list(schema.get("required")))
    said = []
    for name, spec in list(properties.items())[:20]:
        spec = spec if isinstance(spec, Mapping) else {}
        kind = spec.get("type", "any")
        need = ", required" if name in required else ""
        about = (
            f": {_clip(spec.get('description'), 160)}"
            if spec.get("description")
            else ""
        )
        said.append(f"`{name}` ({kind}{need}){about}")
    return "; ".join(said)


def _called(name: str, fallback: str) -> str:
    return adk_name(name) or fallback


def _confirm(config: Mapping[str, Any]) -> str:
    return (
        " A person confirms each call before it's made."
        if config.get("confirm")
        else ""
    )


def _tool_lines(tool: BriefTool, found: Resolved) -> list[str]:
    """How a tool reads to the model: the names it calls and what each does."""
    c = tool.config
    match tool.kind:
        case "memory":
            if c.get("mode") == "every_turn":
                return [
                    f'- Memory ("{tool.name}"): what\'s relevant from earlier conversations '
                    "with the same person comes with each message by itself; nothing to call."
                ]
            return [
                "- `load_memory(query)`: recalls what was said in earlier conversations "
                "with the same person."
            ]
        case "knowledge_base":
            name = _called(tool.name, "search_knowledge_base")
            bases = [
                found.knowledge_bases.get(b)
                for b in _ids(_list(c.get("knowledge_bases")))
            ]
            known = [b for b in bases if b is not None]
            about = "; ".join(
                f'"{b.name}"'
                + (f" ({_clip(b.description, 200)})" if b.description else "")
                for b in known
            )
            lines = [
                f"- `{name}(query)`: searches the knowledge base(s) {about or '(none picked yet)'}"
                + (
                    f". Its description: {_clip(c.get('description'))}"
                    if c.get("description")
                    else ""
                )
                + ". It answers passages with refs to cite."
            ]
            if any(b.kind == SYSTEM for b in known):
                lines.append(
                    f"- `{name[:49]}_system_design(focus?)`: reads the system design "
                    "(applications, their parts and how they connect) of those knowledge bases."
                )
            return lines
        case "http_tool":
            name = adk_name(tool.name) or "(needs a name with a letter)"
            args = _fields(c.get("parameters"))
            return [
                f"- `{name}`: {_clip(c.get('description')) or 'calls an HTTP endpoint'} "
                f"({_clip(c.get('method'), 10) or 'GET'} {_clip(c.get('url'), 200)})."
                + (f" Arguments: {args}." if args else "")
                + " It answers the response's status and body."
                + _confirm(c)
            ]
        case "openapi":
            operations = [
                o for o in _list(c.get("operations")) if isinstance(o, Mapping)
            ]
            if not operations:
                wanted = ", ".join(
                    f"`{o}`" for o in _list(c.get("only")) if isinstance(o, str)
                )
                return [
                    f'- OpenAPI tool "{tool.name}"'
                    + (f" at {_clip(c.get('url'), 200)}" if c.get("url") else "")
                    + (
                        f": its operations {wanted}"
                        if wanted
                        else ": its operations (not read)"
                    )
                    + ", each a tool named after its operation ID in snake_case."
                    + _confirm(c)
                ]
            return [
                f"- `{_clip(o.get('name'), 60)}`: {_clip(o.get('summary'), 200)} "
                f"({_clip(o.get('method'), 10).upper()} {_clip(o.get('path'), 200)})."
                + _confirm(c)
                for o in operations
            ]
        case "mcp":
            server = found.servers.get(str(c.get("server") or ""))
            wanted = {
                t.strip() for t in str(c.get("tools") or "").split(",") if t.strip()
            }
            unknown = (
                "; their names aren't known here, so refer to them by what they're "
                "for, not by name"
            )
            if server is None:
                # One set up by URL: what it's called on the canvas, not where it is.
                named = (
                    ": " + ", ".join(f"`{t}`" for t in sorted(wanted))
                    if wanted
                    else unknown
                )
                return [
                    f'- The tools of the MCP server "{tool.name}"{named}.{_confirm(c)}'
                ]
            listed = [
                t
                for t in server.tools or []
                if isinstance(t, Mapping) and t.get("name")
            ]
            if wanted:
                listed = [t for t in listed if t["name"] in wanted]
            head = f'- MCP server "{server.name}"' + (
                f" ({_clip(server.description, 200)})" if server.description else ""
            )
            if not listed:
                return [f"{head}: its tools{unknown}.{_confirm(c)}"]
            return [head + ":" + _confirm(c)] + [
                f"  - `{t['name']}`: {_clip(t.get('description') or t.get('title'), 300)}"
                for t in listed[:MOST_SERVER_TOOLS]
            ]
        case "saved_agent":
            saved = found.agents.get(str(c.get("agent") or ""))
            name = (saved.adk_name if saved else "") or _called(tool.name, "agent")
            about = saved.description if saved else ""
            title = f' (the Agents page\'s "{saved.name}")' if saved else ""
            return [
                f"- `{name}(request)`{title}: another agent, called with a request; "
                f"it answers back. {_clip(about) or ''}".rstrip()
            ]
        case "sub_agent":
            return [
                f"- `{_called(tool.name, 'agent')}(request)`: a sub-agent, called with a "
                f"request; it answers back. {_clip(c.get('description'))}".rstrip()
            ]
        case "adk_workflow":
            flow = found.workflows.get(str(c.get("workflow") or ""))
            name = (
                adk_name(tool.name) or adk_name(flow.name if flow else "") or "workflow"
            )
            args = _fields(flow.input_schema) if flow else ""
            about = (flow.description if flow else "") or (
                f'runs the workflow "{flow.name}"' if flow else "runs a workflow"
            )
            return [
                f"- `{name}`: {_clip(about)}."
                + (f" Its input: {args}." if args else "")
                + " It answers with the workflow's result."
            ]
        case "code_execution":
            name = _called(tool.name, "run_code")
            return [
                f"- `{name}(code)`: runs a whole Python 3 program, fresh each call, "
                "and answers what it printed and its errors." + _confirm(c)
            ]
        case _:
            return []


_WHERE = {
    "chat_agent": "the chat agent people talk to, on Forge's Agents page",
    "sub_agent": "a sub-agent of a chat agent on Forge's Agents page",
    "workflow_agent": "an LLM agent step in a Forge workflow",
    "workflow_sub_agent": "a sub-agent inside an LLM agent step of a Forge workflow",
}

_MODES = {
    "chat": "when handed off to, it takes over the conversation with the person",
    "task": "when handed off to, it does one task (asking the person if it must) and hands back",
    "single_turn": "it answers once with what it's given, and hands back",
    "tool": "it's called like a tool, with a request, and answers the agent that called it",
}

_WORKFLOW_MODES = {
    "single_turn": "it runs once with what it's given and answers; no person is there to ask",
    "task": "it works until its task is done, and may ask back if it must",
}


def _hand_off_line(agent: BriefAgent, brief: Brief, found: Resolved) -> str:
    saved = found.agents.get(agent.agent or "") if agent.kind == "saved_agent" else None
    name = (saved.adk_name if saved else "") or _called(agent.name, "agent")
    about = _clip(agent.description or (saved.description if saved else ""), 300)
    if brief.where == "workflow":
        kind = {
            "sequential": "a team that runs its agents in order",
            "parallel": "a team that runs its agents at once",
            "loop_agent": "a team that runs its agents in a loop",
        }.get(agent.kind, "an LLM agent")
        kept = f"; its answer is kept as `state.{agent.id}`" if agent.id else ""
        return (
            f'- `{name}` ({kind}): reached with `transfer_to_agent(agent_name="{name}")`'
            f"{kept}. {about}".rstrip()
        )
    mode = agent.mode or "chat"
    how = {
        "chat": f'`transfer_to_agent(agent_name="{name}")` hands it the conversation',
        "task": f"the tool `{name}(request)` gives it a task; it hands back when done",
        "single_turn": f"the tool `{name}(request)` asks it once; it answers back",
    }.get(mode, f'`transfer_to_agent(agent_name="{name}")`')
    title = f' (the Agents page\'s "{saved.name}")' if saved else ""
    return f"- `{name}`{title}: {how}. {about}".rstrip()


def render(brief: Brief, found: Resolved) -> str:
    """The brief as the model reads it."""
    lines = ["# Its setup", ""]
    adk = adk_name(brief.name)
    lines.append(f"- Name: {brief.name}" + (f" (ADK calls it `{adk}`)" if adk else ""))
    if brief.description.strip():
        lines.append(f"- What it's for: {_clip(brief.description, 1_000)}")
    where = _WHERE[brief.role]
    if brief.container:
        where += f', part of "{brief.container}"'
    lines.append(f"- Where it runs: {where}.")
    if brief.where == "workflow" and brief.mode in _WORKFLOW_MODES:
        lines.append(f"- How it runs: {_WORKFLOW_MODES[brief.mode]}.")
    elif brief.role == "sub_agent" and brief.mode in _MODES:
        lines.append(f"- How it's used: {_MODES[brief.mode]}.")
    if brief.sees_only_given:
        lines.append("- It sees only what it's given, not the conversation so far.")
    if brief.ask_person:
        lines.append(
            "- It can stop to ask the person something only they can tell it "
            "(Forge adds how to ask by itself)."
        )
    elif brief.where == "workflow":
        lines.append("- No person is there to answer questions while it runs.")
    if brief.output_schema:
        lines.append(
            "- Its answer is held to this JSON Schema (Forge enforces it): "
            f"{_schema(brief.output_schema)}"
        )
    if brief.parent is not None:
        lines.append(
            f"- The agent above it: {brief.parent.name}"
            + (
                f" ({_clip(brief.parent.description, 300)})"
                if brief.parent.description
                else ""
            )
            + "."
        )

    lines += ["", "## Its tools, by the names it calls them", ""]
    tools = [line for tool in brief.tools for line in _tool_lines(tool, found)]
    if brief.files:
        tools += [
            "- `load_artifacts`: loads the run's files by name (Forge lists them for it).",
            "- `save_file(name, content)`: saves a .txt, .md, .csv, .json, .html or .xml "
            "file to the run.",
        ]
    lines += tools or ["- None yet."]

    lines += ["", "## Agents it hands off to", ""]
    lines += [_hand_off_line(a, brief, found) for a in brief.hand_offs] or ["- None."]

    lines += ["", "## Values it can put in with {{ }}", ""]
    if brief.references:
        for ref in brief.references:
            about = f": {_clip(ref.description, 200)}" if ref.description else ""
            lines.append(f"- `{{{{ {ref.path} }}}}` ({ref.type}){about}")
    else:
        lines.append("- None.")
    return "\n".join(lines)
