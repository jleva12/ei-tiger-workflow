"""Changes the assistant makes to a chat agent's document (``forge.chat_agent/v1``),
and what it finds wrong with one.

The assistant's ``edit_agent`` tool sends a batch of changes; they're applied
here, in order, to a copy of the document, then the whole document is held
to the format (``forge_agent_runtime.document.schema_problems``) before it's
saved as the agent's draft. A change that can't be made refuses the whole
batch, saying which change and why, so the draft never holds half of one.

The changes, each ``{"action": ..., ...}``:

- ``add_node``: ``kind``, ``name``, ``settings`` (over what a new node of the
  kind starts with, the node catalog's ``defaults``), and optionally ``id``
  (made from the kind's ID prefix when left out) and ``attach_to``
  (``{"agent", "way"}``: connects that agent's way out to it).
- ``update_node``: ``id``, and ``name`` and/or ``settings``: each setting
  given replaces the one it has (a list or an object whole).
- ``remove_node``: ``id``; its connections go too. The chat agent people
  talk to can't be removed.
- ``connect``: ``source`` (an agent), ``way`` (``tools``: it calls the
  target; ``agents``: it hands the conversation off to it; an intake
  agent's ``on_complete``: the workflows it starts) and ``target``. The way
  is worked out when only one fits.
- ``disconnect``: ``source``, and ``way`` and/or ``target`` to narrow it.
- ``update_agent``: ``name`` and/or ``description``, the agent's own on the
  Agents page.

A new node is placed on the canvas right of the agent that uses it, clear of
the others. :func:`problems_of` says what would stop the agent working: the
runtime's own checks (``parse_document``) and the builder's checks of each
node's settings against the organization's records.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

from forge_agent_runtime.catalog import CHAT_NODE_CATALOG
from forge_agent_runtime.document import (
    AGENT_LIKE,
    CALLED,
    HANDED_TO,
    HANDS_OFF,
    TOOLS,
    WAYS,
    DocumentError,
    parse_document,
    schema_problems,
    with_defaults,
)
from forge_agent_runtime.generative_ui import is_team_key
from forge_agent_runtime.intake import KIND as INTAKE
from forge_agent_runtime.intake import ON_COMPLETE
from forge_agent_runtime.names import RESERVED_NAMES, adk_name

from forge_admin.assistant.building_blocks import UI_COMPONENTS, known
from forge_admin.assistant.workflow_edits import EditRefused, place_new_nodes

ACTIONS = (
    "add_node",
    "update_node",
    "remove_node",
    "connect",
    "disconnect",
    "update_agent",
)
#: What each way out means, in words.
WAY_WORDS = {
    TOOLS: "uses as a tool",
    HANDS_OFF: "hands off to",
    ON_COMPLETE: "starts when it completes",
}
#: A node's ID, as the format takes it.
NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
#: A URL, or one a run's value or a setting fills in.
URLISH = re.compile(r"^(https?://\S+|\{\{|\$\{)", re.IGNORECASE)
#: The most changes one batch makes.
MAX_CHANGES = 60


@dataclass
class Edited:
    """A document with a batch of changes made."""

    document: dict[str, Any]
    #: What each change did, in words, in order.
    done: list[str] = field(default_factory=list)
    #: The nodes it added, by ID.
    added: list[str] = field(default_factory=list)


@cache
def format_schema() -> dict[str, Any]:
    """:return: The chat agent format's JSON Schema (``chat_agent.schema.json``)."""
    text = (
        resources.files("forge_agent_runtime")
        .joinpath("chat_agent.schema.json")
        .read_text("utf-8")
    )
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def settings_of_kind(kind: str) -> dict[str, Any]:
    """:return: The JSON Schema of a node kind's settings, with the
    definitions it refers to; ``{}`` for a kind the format doesn't know."""
    defs = format_schema()["$defs"]
    schema = defs.get(f"config_{kind}")
    if not isinstance(schema, dict):
        return {}
    wanted: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in wanted and name in defs:
                    wanted[name] = defs[name]
                    visit(defs[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return {**schema, **({"$defs": wanted} if wanted else {})}


def apply_changes(document: Mapping[str, Any], changes: Sequence[Any]) -> Edited:
    """
    :param document: The agent's document, as it's saved.
    :param changes: The changes, in order.
    :return: A copy with every change made, its new nodes placed.
    :raises EditRefused: One can't be made, or the document they make
        doesn't fit the format.
    """
    if not changes:
        raise EditRefused("There are no changes to make.")
    if len(changes) > MAX_CHANGES:
        raise EditRefused(
            f"At most {MAX_CHANGES} changes go in one edit: split them up."
        )
    doc = copy.deepcopy(dict(document))
    doc.setdefault("nodes", [])
    doc.setdefault("edges", [])
    edited = Edited(doc)
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise EditRefused(f"Change {index} isn't an object with an action.")
        action = change.get("action")
        if action not in ACTIONS:
            raise EditRefused(
                f"Change {index}: its action must be one of {', '.join(ACTIONS)}, not {action!r}."
            )
        try:
            edited.done.append(_DO[action](edited, change))
        except EditRefused as refused:
            raise EditRefused(f"Change {index} ({action}): {refused}") from None
    for node in _nodes(doc):
        node["outputs"] = list(WAYS.get(str(node.get("kind")), ()))
    place_new_nodes(doc, edited.added)
    problems = schema_problems(with_defaults(doc))
    if problems:
        raise EditRefused(
            "The agent these changes make doesn't fit Forge's format: "
            + "; ".join(_named(doc, problem) for problem in problems)
        )
    return edited


# ---------------------------------------------------------------- the changes


def _add_node(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    kind = _text(change.get("kind"))
    entry = CHAT_NODE_CATALOG.find(kind)
    if entry is None:
        known_kinds = ", ".join(each.kind for each in CHAT_NODE_CATALOG.kinds)
        raise EditRefused(
            f"there's no node kind {kind!r}; the kinds are {known_kinds}."
        )
    if kind == "agent":
        raise EditRefused(
            "an agent has one chat agent, where conversations start, and it's there "
            "already: add a sub_agent for another agent."
        )
    name = _text(change.get("name")).strip()
    if not name:
        raise EditRefused("give the node a name: what it does, in a few words.")
    _check_name(doc, kind, name, None)
    node_id = _text(change.get("id")).strip() or _new_id(doc, entry.id_prefix)
    if not NODE_ID.fullmatch(node_id):
        raise EditRefused(
            f"its id {node_id!r} isn't one: lowercase letters, digits and _, "
            "starting with a letter."
        )
    if _find(doc, node_id) is not None:
        raise EditRefused(f"the agent has a node {node_id!r} already: pick another id.")
    node = {
        "id": node_id,
        "kind": kind,
        "name": name,
        "config": {
            **copy.deepcopy(entry.defaults),
            **_settings(kind, change.get("settings")),
        },
        "outputs": list(WAYS.get(kind, ())),
    }
    doc["nodes"].append(node)
    edited.added.append(node_id)
    said = f"Added {_label(node)}"
    attach = change.get("attach_to")
    if attach is not None:
        if not isinstance(attach, dict):
            raise EditRefused(
                'its attach_to must be {"agent": <id>, "way": <way out>}.'
            )
        said += "; " + _connect(
            edited,
            {
                "source": attach.get("agent"),
                "way": attach.get("way"),
                "target": node_id,
            },
        )
    return said


def _update_node(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    node = _node(doc, change.get("id"))
    said: list[str] = []
    if change.get("name") is not None:
        name = _text(change.get("name")).strip()
        if not name:
            raise EditRefused("a node's name can't be empty.")
        _check_name(doc, node["kind"], name, node["id"])
        said.append(f"renamed it {name!r}")
        node["name"] = name
    if change.get("settings") is not None:
        settings = _settings(node["kind"], change.get("settings"))
        node["config"] = {**(node.get("config") or {}), **settings}
        said.append("set its " + ", ".join(sorted(settings)))
    if not said:
        raise EditRefused("give it a name or settings to change.")
    return f"Updated {_label(node)}: " + "; ".join(said)


def _remove_node(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    node = _node(doc, change.get("id"))
    if node.get("kind") == "agent":
        raise EditRefused(
            "the chat agent can't be removed: it's where every conversation starts."
        )
    doc["nodes"] = [each for each in _nodes(doc) if each is not node]
    gone = [
        edge
        for edge in _edges(doc)
        if node["id"] in (edge.get("source"), edge.get("target"))
    ]
    doc["edges"] = [edge for edge in _edges(doc) if edge not in gone]
    if isinstance(doc.get("layout"), dict):
        doc["layout"].pop(node["id"], None)
    if node["id"] in edited.added:
        edited.added.remove(node["id"])
    return f"Removed {_label(node)}" + (
        f" and its {len(gone)} connection(s)" if gone else ""
    )


def _fits(way: str, source: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    kind = target.get("kind")
    if way == TOOLS:
        return kind in CALLED and not (kind == INTAKE and source.get("kind") == INTAKE)
    if way == HANDS_OFF:
        return kind in HANDED_TO
    if way == ON_COMPLETE:
        return kind == "adk_workflow"
    return False


def _connect(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    source = _node(doc, change.get("source"))
    target = _node(doc, change.get("target"))
    if target.get("kind") == "agent":
        raise EditRefused(
            "nothing calls or hands off to the chat agent: conversations start there."
        )
    if source is target:
        raise EditRefused("a node can't use itself.")
    ways = WAYS.get(str(source.get("kind")), ())
    if not ways:
        raise EditRefused(
            f"{_label(source)} is a tool, not an agent: only agents use tools or "
            "hand off. Connect it from the agent that should use it."
        )
    way = _text(change.get("way")).strip()
    if not way:
        fitting = [each for each in ways if _fits(each, source, target)]
        if len(fitting) > 1:
            raise EditRefused(
                f"say how {_label(source)} uses {_label(target)}: way "
                + " or ".join(f"{each} ({WAY_WORDS[each]})" for each in fitting)
                + "."
            )
        way = fitting[0] if fitting else ways[0]
    if way not in ways:
        raise EditRefused(
            f"{_label(source)} has no way out {way!r}: its ways out are "
            + ", ".join(f"{each} ({WAY_WORDS[each]})" for each in ways)
            + "."
        )
    if not _fits(way, source, target):
        raise EditRefused(_misfit(way, source, target))
    if way == HANDS_OFF and target.get("kind") in ("sub_agent", INTAKE):
        other = next(
            (
                edge
                for edge in _edges(doc)
                if edge.get("source_output") == HANDS_OFF
                and edge.get("target") == target["id"]
                and edge.get("source") != source["id"]
            ),
            None,
        )
        if other is not None:
            parent = _find(doc, _text(other.get("source")))
            raise EditRefused(
                f"{_label(parent or {'id': other.get('source')})} hands off to "
                f"{_label(target)} already, and ADK lets only one agent hand off "
                "to a sub-agent: disconnect that, or have this one call it as a tool."
            )
    if any(
        each.get("source") == source["id"]
        and each.get("source_output") == way
        and each.get("target") == target["id"]
        for each in _edges(doc)
    ):
        return f"{_label(source)} already {WAY_WORDS[way]} {_label(target)}"
    doc["edges"].append(
        {
            "id": _edge_id(doc, source["id"], way, target["id"]),
            "source": source["id"],
            "source_output": way,
            "target": target["id"],
        }
    )
    return f"Connected: {_label(source)} {WAY_WORDS[way]} {_label(target)}"


def _misfit(way: str, source: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    if way == HANDS_OFF:
        return (
            f"{_label(target)} isn't an agent, so nothing hands off to it: use way "
            f"tools for {_label(source)} to call it."
        )
    if way == ON_COMPLETE:
        return f"an intake agent starts only workflows when it completes, not {_label(target)}."
    if target.get("kind") == INTAKE:
        return "an intake agent can't file requests to another intake agent."
    return f"{_label(target)} can't be called as a tool."


def _disconnect(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    source = _node(doc, change.get("source"))
    way = _text(change.get("way")).strip() or None
    target = _node(doc, change.get("target")) if change.get("target") else None
    gone = [
        edge
        for edge in _edges(doc)
        if edge.get("source") == source["id"]
        and (way is None or edge.get("source_output") == way)
        and (target is None or edge.get("target") == target["id"])
    ]
    if not gone:
        raise EditRefused(f"no connection of {_label(source)} matches it.")
    doc["edges"] = [edge for edge in _edges(doc) if edge not in gone]
    return "Disconnected: " + "; ".join(_edge_label(doc, edge) for edge in gone)


def _update_agent(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    said = []
    if change.get("name") is not None:
        name = _text(change.get("name")).strip()
        if not name:
            raise EditRefused("the agent's name can't be empty.")
        doc["name"] = name
        said.append(f"named it {name!r}")
    if change.get("description") is not None:
        doc["description"] = _text(change.get("description"))
        said.append("set its description")
    if not said:
        raise EditRefused("give it a name or a description.")
    return "Updated the agent: " + "; ".join(said)


_DO = {
    "add_node": _add_node,
    "update_node": _update_node,
    "remove_node": _remove_node,
    "connect": _connect,
    "disconnect": _disconnect,
    "update_agent": _update_agent,
}


# ---------------------------------------------------------------- what's wrong


@dataclass
class Findings:
    """What stops an agent working, and what would make it work better."""

    problems: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)


def problems_of(
    document: Mapping[str, Any],
    blocks: Mapping[str, Any] | None = None,
    *,
    agent_id: str | None = None,
) -> Findings:
    """
    :param document: The agent's document.
    :param blocks: The organization's building blocks
        (``building_blocks.building_blocks``), to check what nodes pick is
        still there; None checks only the document.
    :param agent_id: The agent's own ID: a saved agent can't be it.
    :return: Its problems (the runtime would refuse it, or a node misses a
        setting it needs) and advice (it runs, but could work better).
    """
    found = Findings()
    try:
        parse_document(dict(document))
    except DocumentError as error:
        found.problems.extend(error.problems)
    blocks = blocks or {}
    nodes = _nodes(dict(document))
    for node in nodes:
        _node_findings(node, blocks, agent_id, found)
    used = {edge.get("target") for edge in _edges(dict(document)) if edge.get("target")}
    for node in nodes:
        if node.get("kind") != "agent" and node["id"] not in used:
            found.advice.append(
                f"{_label(node)} isn't connected to any agent, so nothing uses it: "
                "connect it, or remove it."
            )
    return found


def _node_findings(
    node: Mapping[str, Any],
    blocks: Mapping[str, Any],
    agent_id: str | None,
    found: Findings,
) -> None:
    kind = node.get("kind")
    config = node.get("config") or {}
    label = _label(node)

    def problem(text: str) -> None:
        found.problems.append(f"{label}: {text}")

    def advice(text: str) -> None:
        found.advice.append(f"{label}: {text}")

    if kind in ("agent", "sub_agent"):
        if not _text(config.get("instruction")).strip():
            problem("it has no instruction: tell it what to do.")
        if not _text(config.get("description")).strip() and kind == "sub_agent":
            advice(
                "it has no description: the agent that uses it decides when to by it."
            )
    elif kind == "saved_agent":
        target = _text(config.get("agent"))
        agents = known(blocks, "agents")
        if not target:
            problem("pick the agent it uses.")
        elif target == agent_id:
            problem("an agent can't use itself.")
        elif agents is not None and target not in agents:
            problem("the organization has no such agent any more: pick another.")
    elif kind == "adk_workflow":
        target = _text(config.get("workflow"))
        workflows = known(blocks, "workflows")
        if not target:
            problem("pick the workflow it runs.")
        elif workflows is not None and target not in workflows:
            problem("the organization has no such workflow any more: pick another.")
        elif (
            workflows is not None
            and config.get("version") is None
            and workflows[target].get("published_version") is None
        ):
            problem(
                "that workflow isn't published yet, so it has no version to run: "
                'publish it, or set version to "draft" while testing.'
            )
    elif kind == "http_tool":
        if not adk_name(_text(node.get("name"))):
            problem(
                "its name is the tool's name: give it one that starts with a letter."
            )
        url = _text(config.get("url")).strip()
        if not url:
            problem("give it a URL.")
        elif not URLISH.match(url):
            problem("its URL must start with https:// (or http://).")
        if not _text(config.get("description")).strip():
            advice(
                "say what it does in its description: the model decides when to "
                "call it by that."
            )
        _credential(config, blocks, problem)
    elif kind == "openapi":
        if config.get("source", "url") == "url":
            if not _text(config.get("url")).strip():
                problem("give its spec's URL.")
        elif not _text(config.get("spec")).strip():
            problem("paste its spec: OpenAPI 3, JSON or YAML.")
        _credential(config, blocks, problem)
    elif kind == "mcp":
        server = _text(config.get("server"))
        servers = known(blocks, "mcp_servers")
        if server:
            if servers is not None and server not in servers:
                problem(
                    "the organization has no such MCP server any more: pick another."
                )
            elif servers is not None and servers[server].get("status") == "needs_auth":
                advice(
                    f"nobody has signed in to {servers[server].get('name')} yet: sign in "
                    "on the MCP servers page."
                )
        elif not _text(config.get("url")).strip():
            problem(
                "pick one of the organization's MCP servers, or give a server's URL."
            )
    elif kind == "knowledge_base":
        picked = [str(each) for each in config.get("knowledge_bases") or []]
        bases = known(blocks, "knowledge_bases")
        if not picked:
            problem("pick the knowledge bases it searches.")
        for base in picked:
            if bases is None:
                break
            record = bases.get(base)
            if record is None:
                problem(
                    "the organization has no such knowledge base any more: pick another."
                )
            elif record.get("searchable_documents") == 0:
                advice(
                    f"{record.get('name')} has no searchable documents yet: upload some "
                    "on the Knowledge bases page."
                )
    elif kind == "skills":
        picked = [str(each) for each in config.get("skills") or []]
        skills = known(blocks, "skills")
        if not picked:
            problem("pick the skills it has.")
        elif skills is not None and any(each not in skills for each in picked):
            problem("a skill it has was deleted from the vault: pick another.")
    elif kind == "rich_answers":
        picked = [str(each) for each in config.get("components") or []]
        if not picked:
            problem("pick the components it may use.")
        team = blocks.get(UI_COMPONENTS)
        if isinstance(team, list):
            usable = {str(record.get("key")) for record in team}
            gone = [key for key in picked if is_team_key(key) and key not in usable]
            if gone:
                problem(
                    f"{', '.join(gone)} isn't one of the organization's team components "
                    "it can use (published and enabled): pick another."
                )


def _credential(
    config: Mapping[str, Any],
    blocks: Mapping[str, Any],
    problem: Any,
) -> None:
    credential = _text(config.get("credential"))
    credentials = known(blocks, "credentials")
    if credential and credentials is not None and credential not in credentials:
        problem("the organization has no such credential any more: pick another.")


# ---------------------------------------------------------------- helpers


def _settings(kind: str, value: Any) -> dict[str, Any]:
    # Settings a node of the kind has, by the format; others refused by name.
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EditRefused("its settings must be an object of setting names to values.")
    allowed = set(settings_of_kind(kind).get("properties", {}))
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EditRefused(
            f"a {kind} node has no setting {', '.join(map(repr, unknown))}; "
            f"its settings are {', '.join(sorted(allowed))}."
        )
    return copy.deepcopy(value)


def _check_name(doc: dict[str, Any], kind: str, name: str, own: str | None) -> None:
    # An agent's or an HTTP tool's name is what ADK calls it by: one it can
    # take, and each agent's its own.
    if kind not in AGENT_LIKE and kind != "http_tool":
        return
    wanted = adk_name(name)
    if not wanted:
        raise EditRefused(f"{name!r} makes no name ADK takes: use letters in it.")
    if wanted in RESERVED_NAMES:
        raise EditRefused(
            f"{name!r} can't be its name: ADK keeps {wanted!r} for the person."
        )
    if kind not in AGENT_LIKE:
        return
    for node in _nodes(doc):
        if (
            node["id"] != own
            and node.get("kind") in AGENT_LIKE
            and adk_name(_text(node.get("name"))) == wanted
        ):
            raise EditRefused(
                f"{_label(node)} is called that already (or close to it): "
                "give this one another name."
            )


def _new_id(doc: dict[str, Any], prefix: str) -> str:
    taken = {node["id"] for node in _nodes(doc)}
    number = 1
    while f"{prefix}_{number}" in taken:
        number += 1
    return f"{prefix}_{number}"


def _edge_id(doc: dict[str, Any], source: str, way: str, target: str) -> str:
    base = f"{source}:{way}->{target}"
    taken = {edge.get("id") for edge in _edges(doc)}
    edge_id, number = base, 2
    while edge_id in taken:
        edge_id, number = f"{base}#{number}", number + 1
    return edge_id


def _node(doc: dict[str, Any], node_id: Any) -> dict[str, Any]:
    found = _find(doc, _text(node_id))
    if found is None:
        nodes = ", ".join(
            f"{node['id']} ({_text(node.get('name'))})" for node in _nodes(doc)
        )
        raise EditRefused(f"the agent has no node {node_id!r}; its nodes are {nodes}.")
    return found


def _find(doc: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((node for node in _nodes(doc) if node["id"] == node_id), None)


def _nodes(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        node
        for node in doc.get("nodes") or []
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]


def _edges(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [edge for edge in doc.get("edges") or [] if isinstance(edge, dict)]


def label(node: Mapping[str, Any]) -> str:
    """:return: A node as the assistant names it: its name, then its ID."""
    return f"{_text(node.get('name')) or node.get('id')} ({node.get('id')})"


_label = label


def _edge_label(doc: dict[str, Any], edge: Mapping[str, Any]) -> str:
    source = _find(doc, _text(edge.get("source")))
    target = _find(doc, _text(edge.get("target")))
    way = WAY_WORDS.get(_text(edge.get("source_output")), edge.get("source_output"))
    return (
        f"{_label(source) if source else edge.get('source')} {way} "
        f"{_label(target) if target else edge.get('target')}"
    )


def _named(doc: dict[str, Any], problem: str) -> str:
    # A format problem's path, with the node it's in named.
    def name(found: re.Match[str]) -> str:
        nodes = doc.get("nodes") or []
        index = int(found.group(1))
        node = (
            nodes[index]
            if index < len(nodes) and isinstance(nodes[index], dict)
            else {}
        )
        return f"node {node.get('id', index)}" + (" settings" if found.group(2) else "")

    return re.sub(r"nodes/(\d+)(/config)?", name, problem)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
