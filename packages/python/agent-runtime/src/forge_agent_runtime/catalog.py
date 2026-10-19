"""
The kinds of node a chat agent (``forge.chat_agent/v1``) can have, as the
Agents canvas offers them (``node_kinds.json``): each kind's words and icon,
the library group it's in, its ways out, whether it can be handed off to,
whether an agent can call it as a tool, whether a workflow's LLM agent may
have it as a tool, and what a new one starts with. It's the one place
they're kept: the admin API serves it to the web console, the document's
checks and defaults read it (``document``), and so do the tools built for a
workflow's LLM agents (``build.TOOL_KINDS``, and the ADK workflows task's).

The groups and ways out have the same shape as the workflow node catalog's
(``forge_task_adk_workflows.graph.catalog``), which uses these models.
"""

from __future__ import annotations

from importlib import resources
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KindGroup(_Entry):
    """A group of kinds in a builder's library."""

    id: str
    label: str


class NodeOutput(_Entry):
    """A way out of a node: its ID in edges and what the canvas calls it."""

    id: str
    label: str
    #: A branch's own, named way out (a case, a rule, true or false, tools).
    branch: bool = False
    #: The way out for everything no branch took.
    fallback: bool = False
    #: A loop's body: it comes back to the loop.
    body: bool = False


class ChatNodeKind(_Entry):
    """One kind of node on the Agents canvas."""

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
    #: An agent's ways out (its tools, who it hands off to); none for what it calls.
    outputs: list[NodeOutput]
    #: Whether an agent can hand off to it.
    handed_to: bool = False
    #: Whether an agent can call it as a tool (an intake agent: file requests to it); one that's only handed off to isn't.
    tool: bool = True
    #: Whether a workflow's LLM agent may have it as a tool.
    workflow_tool: bool = False
    #: The settings a new one starts with, and what an older document's lack.
    defaults: dict[str, Any] = Field(default_factory=dict)


class ChatNodeCatalog(_Entry):
    """Every kind of node on the Agents canvas, in the library's order, and its groups."""

    groups: list[KindGroup]
    kinds: list[ChatNodeKind]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        groups = [group.id for group in self.groups]
        if len(set(groups)) != len(groups):
            raise ValueError("a group is listed twice")
        kinds = [kind.kind for kind in self.kinds]
        if len(set(kinds)) != len(kinds):
            raise ValueError("a kind is listed twice")
        if "agent" not in kinds:
            raise ValueError("it has no agent, where conversations start")
        for kind in self.kinds:
            if kind.group not in groups:
                raise ValueError(f"{kind.kind}'s group {kind.group!r} isn't one of the groups")
            ways = [output.id for output in kind.outputs]
            if len(set(ways)) != len(ways):
                raise ValueError(f"{kind.kind} has a way out twice")
            if not kind.tool and not kind.handed_to and kind.kind != "agent":
                raise ValueError(f"{kind.kind} is neither called nor handed off to: nothing could reach it")
        return self

    def find(self, kind: Any) -> ChatNodeKind | None:
        """:return: The kind called ``kind``; None for one it doesn't have."""
        return next((entry for entry in self.kinds if entry.kind == kind), None)


def _load() -> ChatNodeCatalog:
    text = resources.files("forge_agent_runtime").joinpath("node_kinds.json").read_text("utf-8")
    return ChatNodeCatalog.model_validate_json(text)


#: Every kind of node on the Agents canvas, read once.
CHAT_NODE_CATALOG = _load()
