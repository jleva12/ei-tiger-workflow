"""A chat agent as JSON (``forge.chat_agent/v1``), as the Forge builder writes it.

``nodes`` are the agent people chat with (kind ``agent``, exactly one: the
entry point), its sub-agents and its tools, each with its settings
(``config``); ``edges`` join an agent's way out (``tools`` or ``agents``) to
what it calls or hands off to. ``layout`` is the builder's and ignored here.

An exported agent may carry two more fields: ``version`` (the published
version it is, or ``"draft"``) and ``dependencies``, the saved agents it uses
as documents of their own, by ``"<id>@<version>"``.

:func:`load_document` reads one, fills in settings older documents lack, and
checks it against the format's JSON Schema (``chat_agent.schema.json``,
generated from the builder) and the rules ADK builds by.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from forge_agent_runtime.names import RESERVED_NAMES, adk_name

FORMAT: Literal["forge.chat_agent/v1"] = "forge.chat_agent/v1"
#: An agent's ways out: what it can call, and who it hands off to.
TOOLS = "tools"
HANDS_OFF = "agents"

Kind = Literal[
    "agent",
    "sub_agent",
    "saved_agent",
    "adk_workflow",
    "memory",
    "knowledge_base",
    "http_tool",
    "openapi",
    "mcp",
]
AGENT_LIKE = frozenset({"agent", "sub_agent"})
#: What can be handed off to: an agent.
HANDED_TO = frozenset({"sub_agent", "saved_agent"})

_LLM: dict[str, Any] = {
    "description": "",
    "instruction": "",
    "model": {"provider": "", "name": ""},
    "thinking_level": "",
    "max_output_tokens": None,
}

#: Each kind's settings with nothing set, as the builder makes a new node's.
DEFAULTS: dict[str, dict[str, Any]] = {
    "agent": {**_LLM, "state_schema": {}},
    "sub_agent": {
        **_LLM,
        "mode": "chat",
        "disallow_transfer_to_parent": False,
        "disallow_transfer_to_peers": False,
        "include_contents": "default",
    },
    "saved_agent": {"agent": ""},
    "adk_workflow": {"workflow": "", "version": None},
    "memory": {"mode": "on_demand"},
    "knowledge_base": {"knowledge_bases": [], "description": "", "max_results": 5},
    "http_tool": {
        "description": "",
        "method": "GET",
        "url": "",
        "headers": [],
        "parameters": {},
        "confirm": False,
    },
    "openapi": {"source": "url", "url": "", "spec": "", "operations": "", "confirm": False},
    "mcp": {
        "server": "",
        "transport": "streamable_http",
        "url": "",
        "headers": [],
        "tools": "",
        "confirm": False,
    },
}


class DocumentError(ValueError):
    """A document that isn't a chat agent ADK can build, with each problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems) if problems else "It isn't a chat agent.")
        self.problems = problems


class Node(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Kind
    name: str
    config: dict[str, Any]
    outputs: list[str] = Field(default_factory=list)

    @property
    def adk_name(self) -> str:
        return adk_name(self.name)


class Edge(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    source: str
    source_output: str
    target: str


class ChatAgentDocument(BaseModel):
    """A chat agent. Fields the format doesn't name are kept, for exports."""

    model_config = ConfigDict(extra="allow")

    format: Literal["forge.chat_agent/v1"] = FORMAT
    id: str
    name: str
    description: str = ""
    organization_id: str | None = None
    nodes: list[Node]
    edges: list[Edge]
    layout: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    #: The published version it is, or "draft"; None when it doesn't say.
    version: int | Literal["draft"] | None = None
    #: The saved agents it uses, by "<id>@<version>", for running it on its own.
    dependencies: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        return next(node for node in self.nodes if node.id == node_id)

    @property
    def entry(self) -> Node:
        """The agent people chat with: where every conversation starts."""
        return next(node for node in self.nodes if node.kind == "agent")

    def targets(self, node_id: str, output: str) -> list[Node]:
        """What a node's way out leads to, in the order it was connected."""
        by_id = {node.id: node for node in self.nodes}
        return [
            by_id[edge.target]
            for edge in self.edges
            if edge.source == node_id and edge.source_output == output and edge.target in by_id
        ]

    @property
    def state_schema(self) -> dict[str, Any]:
        """The JSON Schema of the state the chat sends besides model and thinking_level."""
        schema = self.entry.config.get("state_schema")
        return schema if isinstance(schema, dict) else {}

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


@cache
def _validator() -> Draft202012Validator:
    text = resources.files("forge_agent_runtime").joinpath("chat_agent.schema.json").read_text("utf-8")
    return Draft202012Validator(json.loads(text))


def with_defaults(raw: Mapping[str, Any]) -> dict[str, Any]:
    """
    :return: A copy whose nodes have every setting their kind has, the
        missing ones (older documents') at their defaults.
    """
    doc = copy.deepcopy(dict(raw))
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        defaults = DEFAULTS.get(str(node.get("kind")))
        config = node.get("config")
        if defaults is not None and isinstance(config, dict):
            node["config"] = {**copy.deepcopy(defaults), **config}
        node.setdefault("outputs", [TOOLS, HANDS_OFF] if node.get("kind") in AGENT_LIKE else [])
    return doc


def schema_problems(raw: Mapping[str, Any], *, limit: int = 8) -> list[str]:
    """:return: Where a document breaks the format's JSON Schema; empty when it doesn't."""
    out = []
    for error in sorted(_validator().iter_errors(raw), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(part) for part in error.absolute_path) or "the document"
        out.append(f"{where}: {error.message}")
        if len(out) == limit:
            break
    return out


def structure_problems(doc: ChatAgentDocument) -> list[str]:
    """
    :return: What stops ADK building the agent: not one entry agent, edges
        that don't fit, names ADK can't use or uses twice, a sub-agent with
        two parents, agents that hand off to each other in a circle.
    """
    out: list[str] = []
    entries = [node for node in doc.nodes if node.kind == "agent"]
    if len(entries) != 1:
        out.append(f"It has {len(entries)} chat agents; it needs exactly one, where conversations start.")
    by_id = {node.id: node for node in doc.nodes}
    if len(by_id) != len(doc.nodes):
        out.append("Two nodes have the same ID.")

    parents: dict[str, list[str]] = {}
    for edge in doc.edges:
        source, target = by_id.get(edge.source), by_id.get(edge.target)
        if source is None or target is None:
            out.append(f"Edge {edge.id} joins a node that isn't there.")
            continue
        if source.kind not in AGENT_LIKE or edge.source_output not in (TOOLS, HANDS_OFF):
            out.append(f"Edge {edge.id} leaves {source.name!r}, which has no {edge.source_output!r} way out.")
            continue
        if target.kind == "agent":
            out.append(f"Edge {edge.id} leads to the chat agent, which nothing calls or hands off to.")
        elif edge.source_output == HANDS_OFF and target.kind not in HANDED_TO:
            out.append(f"{source.name!r} hands off to {target.name!r}, which isn't an agent.")
        elif edge.source_output == HANDS_OFF and target.kind == "sub_agent":
            parents.setdefault(target.id, []).append(source.id)

    for child, sources in parents.items():
        if len(sources) > 1:
            out.append(f"{by_id[child].name!r} is handed off to by {len(sources)} agents; ADK allows one.")

    names: dict[str, str] = {}
    for node in doc.nodes:
        if node.kind not in AGENT_LIKE and node.kind != "http_tool":
            continue
        name = node.adk_name
        if not name:
            out.append(f"{node.name!r} needs a letter in its name for ADK to call it by.")
        elif name in RESERVED_NAMES:
            out.append(f"{node.name!r} can't be called {name!r}: ADK keeps that name for the person.")
        elif node.kind in AGENT_LIKE and name in names:
            out.append(f"{node.name!r} and {names[name]!r} have the same ADK name, {name!r}.")
        elif node.kind in AGENT_LIKE:
            names[name] = node.name
        if node.kind == "sub_agent" and node.config.get("mode") == "task" and doc.targets(node.id, HANDS_OFF):
            out.append(f"{node.name!r} does a task and hands back, so it can't hand off to anyone.")

    # Agents handing off to or calling each other in a circle would never end.
    def visit(node_id: str, path: tuple[str, ...]) -> None:
        if node_id in path:
            circle = " → ".join(by_id[n].name for n in (*path[path.index(node_id) :], node_id))
            out.append(f"{circle} call or hand off to each other in a circle.")
            return
        for output in (TOOLS, HANDS_OFF):
            for target in doc.targets(node_id, output):
                if target.kind in AGENT_LIKE:
                    visit(target.id, (*path, node_id))

    if len(entries) == 1:
        visit(entries[0].id, ())
    return out


def parse_document(raw: Mapping[str, Any]) -> ChatAgentDocument:
    """
    :param raw: A document's JSON value.
    :return: The document, with settings older ones lack at their defaults.
    :raises DocumentError: It breaks the format, or ADK couldn't build it.
    """
    if not isinstance(raw, Mapping):
        raise DocumentError(["A chat agent is a JSON object."])
    if raw.get("format") != FORMAT:
        raise DocumentError([f'This isn\'t a Forge chat agent: its "format" should be "{FORMAT}".'])
    filled = with_defaults(raw)
    problems = schema_problems(filled)
    if problems:
        raise DocumentError(problems)
    doc = ChatAgentDocument.model_validate(filled)
    problems = structure_problems(doc)
    if problems:
        raise DocumentError(problems)
    return doc


def load_document(source: str | Path | Mapping[str, Any]) -> ChatAgentDocument:
    """
    :param source: A path to the JSON file, its text, or its parsed value.
    :return: The document.
    :raises DocumentError: It isn't a chat agent ADK can build.
    """
    if isinstance(source, Mapping):
        return parse_document(source)
    text = str(source)
    if isinstance(source, Path) or not text.lstrip().startswith("{"):
        text = Path(text).read_text("utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DocumentError([f"It isn't JSON: {exc}"]) from exc
    return parse_document(raw)
