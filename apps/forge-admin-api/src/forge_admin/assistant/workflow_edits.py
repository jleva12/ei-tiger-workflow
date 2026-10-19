"""Changes the workflow assistant makes to an ADK workflow's document.

The assistant's ``edit_workflow`` tool sends a batch of changes; they're
applied here, in order, to a copy of the document, then the whole document
is held to the format (``documents.schema_problems``) before it's saved as
the workflow's draft. A change that can't be made refuses the whole batch,
saying which change and why, so the draft never holds half of one.

The changes, each ``{"action": ..., ...}``:

- ``add_node``: ``kind``, ``name``, ``settings`` (over what a new node of the
  kind starts with, its catalog's ``defaults``), and optionally ``id`` (made
  from the kind's ID prefix when left out) and ``after`` (``{"node", "way"}``:
  connects that node's way to it).
- ``update_node``: ``id``, and ``name`` and/or ``settings``: each setting
  given replaces the one it has (a list or an object whole).
- ``remove_node``: ``id``; its edges go too. The start can't be removed.
- ``connect``: ``source``, ``way`` (one of the source's ways out; its only
  one when left out) and ``target``.
- ``disconnect``: ``source``, and ``way`` and/or ``target`` to narrow it.
- ``update_workflow``: ``name`` and/or ``description``.

Every node's ``outputs`` are worked out again from its kind and settings
(``graph.outputs_of``: a Switch's cases, an HTTP request's status ways), and
an edge whose way is gone is dropped and reported. A new node is placed on
the canvas beside the node that leads to it, clear of the others.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from forge_task_adk_workflows.graph import NODE_CATALOG, adk_name, outputs_of

from forge_admin.adk_workflows.documents import SCHEMA_PATH, schema_problems

ACTIONS = (
    "add_node",
    "update_node",
    "remove_node",
    "connect",
    "disconnect",
    "update_workflow",
)
#: A node's ID, as the format takes it.
NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
#: How far apart the canvas puts nodes, and how much room one takes.
STEP_X = 320
STEP_Y = 160
ROOM_X = 260
ROOM_Y = 140
#: The most changes one batch makes.
MAX_CHANGES = 60


class EditRefused(ValueError):
    """A change can't be made: the message says which and why."""


@dataclass
class Edited:
    """A document with a batch of changes made."""

    document: dict[str, Any]
    #: What each change did, in words, in order.
    done: list[str] = field(default_factory=list)
    #: The nodes it added, by ID.
    added: list[str] = field(default_factory=list)
    #: Edges dropped because the way they left by is gone.
    dropped: list[str] = field(default_factory=list)


@lru_cache
def format_schema() -> dict[str, Any]:
    """:return: The format's JSON Schema (``agent.schema.json``)."""
    loaded: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return loaded


def schema_def(name: str) -> dict[str, Any]:
    """:return: One of the format's definitions (``config_http``,
    ``tool_config_mcp``); ``{}`` for none."""
    found = format_schema()["$defs"].get(name)
    return found if isinstance(found, dict) else {}


def settings_of_kind(kind: str) -> dict[str, Any]:
    """:return: The JSON Schema of a node kind's settings (``config``); ``{}``
    for one the format doesn't know."""
    return schema_def(f"config_{kind}")


def apply_changes(document: Mapping[str, Any], changes: Sequence[Any]) -> Edited:
    """
    :param document: The workflow's document, as it's saved.
    :param changes: The changes, in order.
    :return: A copy with every change made, its ``outputs`` and layout
        brought up to date.
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
    _settle(edited)
    problems = schema_problems(doc)
    if problems:
        raise EditRefused(
            "The workflow these changes make doesn't fit Forge's format: "
            + "; ".join(_named(doc, problem) for problem in problems)
        )
    return edited


# ---------------------------------------------------------------- the changes


def _add_node(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    kind = _text(change.get("kind"))
    entry = NODE_CATALOG.find(kind)
    if entry is None:
        known = ", ".join(each.kind for each in NODE_CATALOG.kinds)
        raise EditRefused(f"there's no node kind {kind!r}; the kinds are {known}.")
    if kind == "start" and any(node.get("kind") == "start" for node in _nodes(doc)):
        raise EditRefused("the workflow has a start already, and has only one.")
    name = _text(change.get("name")).strip()
    if not name:
        raise EditRefused("give the node a name: what it does, in a few words.")
    _check_name(doc, name, None)
    node_id = _text(change.get("id")).strip() or _new_id(doc, entry.id_prefix)
    if not NODE_ID.fullmatch(node_id):
        raise EditRefused(
            f"its id {node_id!r} isn't one: lowercase letters, digits and _, "
            "starting with a letter."
        )
    if _find(doc, node_id) is not None:
        raise EditRefused(
            f"the workflow has a node {node_id!r} already: pick another id."
        )
    settings = _settings(kind, change.get("settings"))
    node = {
        "id": node_id,
        "kind": kind,
        "name": name,
        "config": {**copy.deepcopy(entry.defaults), **settings},
        "outputs": [],
    }
    doc["nodes"].append(node)
    edited.added.append(node_id)
    said = f"Added {_label(node)}"
    after = change.get("after")
    if after is not None:
        if not isinstance(after, dict):
            raise EditRefused('its after must be {"node": <id>, "way": <way out>}.')
        said += "; " + _connect(
            edited,
            {"source": after.get("node"), "way": after.get("way"), "target": node_id},
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
        _check_name(doc, name, node["id"])
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
    if node.get("kind") == "start":
        raise EditRefused("the start can't be removed: every workflow has one.")
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


def _connect(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    source = _node(doc, change.get("source"))
    target = _node(doc, change.get("target"))
    if target.get("kind") == "start":
        raise EditRefused("nothing leads into the start: the run's input does.")
    if source is target:
        raise EditRefused("a node can't lead to itself.")
    ways = outputs_of(source)
    if not ways:
        raise EditRefused(f"{_label(source)} is an ending: it has no ways out.")
    way = _text(change.get("way")).strip()
    if not way:
        if len(ways) > 1:
            raise EditRefused(
                f"say which way out of {_label(source)} leads there: one of {', '.join(ways)}."
            )
        way = ways[0]
    if way not in ways:
        raise EditRefused(
            f"{_label(source)} has no way out {way!r}: its ways out are {', '.join(ways)}."
        )
    edge = {
        "id": _edge_id(doc, source["id"], way, target["id"]),
        "source": source["id"],
        "source_output": way,
        "target": target["id"],
    }
    if any(
        each.get("source") == source["id"]
        and each.get("source_output") == way
        and each.get("target") == target["id"]
        for each in _edges(doc)
    ):
        return f"{_label(source)} already leads to {_label(target)} by {way}"
    doc["edges"].append(edge)
    return f"Connected {_label(source)} by {way} to {_label(target)}"


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
    return f"Disconnected {', '.join(_edge_label(doc, edge) for edge in gone)}"


def _update_workflow(edited: Edited, change: dict[str, Any]) -> str:
    doc = edited.document
    said = []
    if change.get("name") is not None:
        name = _text(change.get("name")).strip()
        if not name:
            raise EditRefused("the workflow's name can't be empty.")
        doc["name"] = name
        said.append(f"named it {name!r}")
    if change.get("description") is not None:
        doc["description"] = _text(change.get("description"))
        said.append("set its description")
    if not said:
        raise EditRefused("give it a name or a description.")
    return "Updated the workflow: " + "; ".join(said)


_DO = {
    "add_node": _add_node,
    "update_node": _update_node,
    "remove_node": _remove_node,
    "connect": _connect,
    "disconnect": _disconnect,
    "update_workflow": _update_workflow,
}


# ---------------------------------------------------------------- after the changes


def _settle(edited: Edited) -> None:
    # Each node's ways out from its settings; edges whose way is gone
    # dropped; the new nodes placed.
    doc = edited.document
    for node in _nodes(doc):
        node["outputs"] = outputs_of(node)
    ways = {node["id"]: set(node["outputs"]) for node in _nodes(doc)}
    kept = []
    for edge in _edges(doc):
        if edge.get("source_output") in ways.get(str(edge.get("source")), set()):
            kept.append(edge)
        else:
            edited.dropped.append(_edge_label(doc, edge))
    doc["edges"] = kept
    place_new_nodes(doc, edited.added)


def place_new_nodes(doc: dict[str, Any], added: list[str]) -> None:
    """
    Places each new node on the canvas: right of the node that leads to it
    (or of the rightmost one), moved down until it's clear of the others.
    A node already placed stays where it is.

    :param doc: A workflow's or a chat agent's document, changed in place.
    :param added: The new nodes, by ID, in the order they were added.
    """
    layout = doc.get("layout")
    if not isinstance(layout, dict):
        layout = doc["layout"] = {}
    for node_id in added:
        if node_id in layout:
            continue
        sources = [
            edge.get("source") for edge in _edges(doc) if edge.get("target") == node_id
        ]
        placed = [layout[source] for source in sources if source in layout]
        if placed:
            x, y = placed[0]["x"] + STEP_X, placed[0]["y"]
        elif layout:
            x = max(point["x"] for point in layout.values()) + STEP_X
            y = min(point["y"] for point in layout.values())
        else:
            x, y = 0, 0
        while any(
            abs(point["x"] - x) < ROOM_X and abs(point["y"] - y) < ROOM_Y
            for point in layout.values()
        ):
            y += STEP_Y
        layout[node_id] = {"x": x, "y": y}


# ---------------------------------------------------------------- helpers


def _settings(kind: str, value: Any) -> dict[str, Any]:
    # Settings a node of the kind has, by the format; others refused by name.
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EditRefused("its settings must be an object of setting names to values.")
    known = set(settings_of_kind(kind).get("properties", {}))
    unknown = sorted(set(value) - known)
    if unknown:
        raise EditRefused(
            f"a {kind} node has no setting {', '.join(map(repr, unknown))}; "
            f"its settings are {', '.join(sorted(known))}."
        )
    return copy.deepcopy(value)


def _check_name(doc: dict[str, Any], name: str, own: str | None) -> None:
    # Each node's ADK name (its name as a Python identifier) is its own.
    wanted = adk_name(name)
    if not wanted:
        raise EditRefused(f"{name!r} makes no name ADK takes: use letters in it.")
    for node in _nodes(doc):
        if (
            node["id"] != own
            and node.get("kind") != "start"
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
        known = ", ".join(
            f"{node['id']} ({_text(node.get('name'))})" for node in _nodes(doc)
        )
        raise EditRefused(
            f"the workflow has no node {node_id!r}; its nodes are {known}."
        )
    return found


def _find(doc: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((node for node in _nodes(doc) if node["id"] == node_id), None)


def _nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node
        for node in doc.get("nodes") or []
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]


def _edges(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge for edge in doc.get("edges") or [] if isinstance(edge, dict)]


def _label(node: Mapping[str, Any]) -> str:
    return f"{_text(node.get('name')) or node.get('id')} ({node.get('id')})"


def _edge_label(doc: dict[str, Any], edge: Mapping[str, Any]) -> str:
    source = _find(doc, _text(edge.get("source")))
    target = _find(doc, _text(edge.get("target")))
    return (
        f"{_label(source) if source else edge.get('source')} by {edge.get('source_output')} "
        f"to {_label(target) if target else edge.get('target')}"
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
