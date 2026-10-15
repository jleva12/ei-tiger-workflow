"""
The kinds of node a ``forge.agent/v1`` document can have, as the builder
offers them (``node_kinds.json``): each kind's words and icon, the library
group it's in, its ways out and what a new one starts with. It's the one
place they're kept: the admin API serves it to the web console's builder,
and a node's ways out here are the ones its graph is built with
(``graph.outputs_of``). Every kind in it has a factory (``factories``), and
every factory a kind in it. Its groups and ways out have the shape of the
Agents canvas's catalog's (``forge_agent_runtime.catalog``).
"""

from __future__ import annotations

from importlib import resources
from typing import Any, Self

from forge_agent_runtime.catalog import KindGroup, NodeOutput
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutputRows(_Entry):
    """
    Ways out a node's settings list, one per row, before its fixed ones: a
    Switch's cases, a Match's rules. Each row's ``id`` is its way out's; with
    ``ways``, each row has those (a Decision board's choice its Accepted and
    Rejected ways), ``<row ID>:<way ID>``.
    """

    #: The setting holding the rows.
    setting: str
    #: The row's key its way out is called by.
    label: str
    #: What it's called when that's blank; ``{n}`` is the row's number.
    empty: str
    #: Each row's ways out, when it has more than one.
    ways: list[NodeOutput] | None = None


class NodeKind(_Entry):
    """One kind of node."""

    kind: str
    label: str
    #: Its group's ID.
    group: str
    #: The web console's name for its icon (a Hugeicons name: ``robot-01``).
    icon: str
    #: One line: what the node does.
    summary: str
    #: Words the library's filter also matches.
    keywords: str
    #: The prefix of a new node's ID.
    id_prefix: str
    #: Agent nodes wear an agent orb: which of the five.
    orb: int | None = None
    #: Drawn as a start or finish.
    terminal: bool = False
    #: A person decides here.
    person: bool = False
    #: Ways out from its settings' rows, before ``outputs``.
    rows: OutputRows | None = None
    #: Its fixed ways out, in the order the canvas lists them.
    outputs: list[NodeOutput]
    #: The settings a new one starts with.
    defaults: dict[str, Any] = Field(default_factory=dict)


class NodeCatalog(_Entry):
    """Every kind of node, in the library's order, and the library's groups."""

    groups: list[KindGroup]
    kinds: list[NodeKind]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        groups = [group.id for group in self.groups]
        if len(set(groups)) != len(groups):
            raise ValueError("a group is listed twice")
        kinds = [kind.kind for kind in self.kinds]
        if len(set(kinds)) != len(kinds):
            raise ValueError("a kind is listed twice")
        for kind in self.kinds:
            if kind.group not in groups:
                raise ValueError(f"{kind.kind}'s group {kind.group!r} isn't one of the groups")
            ways = [output.id for output in kind.outputs]
            if len(set(ways)) != len(ways):
                raise ValueError(f"{kind.kind} has a way out twice")
        return self

    def find(self, kind: Any) -> NodeKind | None:
        """:return: The kind called ``kind``; None for one it doesn't have."""
        return next((entry for entry in self.kinds if entry.kind == kind), None)


def _load() -> NodeCatalog:
    text = resources.files(__package__).joinpath("node_kinds.json").read_text("utf-8")
    return NodeCatalog.model_validate_json(text)


#: Every kind of node, read once.
NODE_CATALOG = _load()
