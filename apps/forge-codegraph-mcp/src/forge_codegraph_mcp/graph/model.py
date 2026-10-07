"""The code graph as the worker stores it: versioned node and edge records.

Every stored fact is a version bounded by the generations that opened and
closed it, so the live graph, any earlier generation and a node's timeline are
all views over the same rows. A fact keeps the JSON form the worker wrote
(``{"node": {...}}`` or ``{"edge": {...}}``), so a record reaches the caller
exactly as stored.

Results are dataclasses rendered with :func:`to_json`, which follows the Go
server's JSON: fields marked ``omitempty`` are left out when empty, and
``inline`` fields are flattened into their parent, as embedded structs are.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from forge_codegraph_mcp.graph.errors import IntegrityFailure

# Directions: "out" follows an edge from its source, "in" from its target.
OUT = "out"
IN = "in"
BOTH = "both"
DIRECTIONS = (OUT, IN, BOTH)

# Record kinds.
NODE = "node"
EDGE = "edge"

# Node kinds the projector emits beside the declarations a language adapter adds.
NODE_SOURCE_FILE = "source_file"
NODE_EXTERNAL_SYMBOL = "external_symbol"
NODE_CODE_CHUNK = "code_chunk"

# Edge kinds. Contains is structural; the rest are relationships the language
# resolver proved.
EDGE_CONTAINS = "contains"
EDGE_CALLS = "calls"
EDGE_USES_TYPE = "uses_type"
EDGE_REFERENCES = "references"
EDGE_INHERITS = "inherits"
EDGE_OVERRIDES = "overrides"
EDGE_IMPLEMENTS = "implements"
EDGE_FRAMEWORK_BINDING = "framework_binding"

# The edges that relate declarations by meaning rather than containment: the
# ones followed to find callers, callees, users of a type and a hierarchy.
SEMANTIC_EDGE_KINDS = (
    EDGE_CALLS,
    EDGE_REFERENCES,
    EDGE_USES_TYPE,
    EDGE_INHERITS,
    EDGE_OVERRIDES,
    EDGE_IMPLEMENTS,
    EDGE_FRAMEWORK_BINDING,
)

# Cross-repository link kinds: how a node in one repository reaches a node in
# another outside the code any one compiler sees.
CROSS_KINDS = ("calls_api", "sends_event", "depends_on", "shares_data", "connects_to")


def jfield(
    default: Any = None,
    *,
    omitempty: bool = False,
    inline: bool = False,
    name: str | None = None,
    factory: Any = None,
) -> Any:
    """A dataclass field with its JSON rendering: ``omitempty`` drops an empty
    value, ``inline`` flattens a nested result into its parent, ``name``
    renames the key."""
    metadata = {"omitempty": omitempty, "inline": inline, "json": name}
    if factory is not None:
        return field(default_factory=factory, metadata=metadata)
    return field(default=default, metadata=metadata)


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float | str | list | tuple | dict):
        return not value
    return False


def to_json(value: Any) -> Any:
    """Renders results as the JSON values the tools return."""
    if isinstance(value, Version):
        return value.to_json()
    if is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in fields(value):
            item = getattr(value, f.name)
            if f.metadata.get("inline"):
                out.update(to_json(item))
                continue
            if f.metadata.get("omitempty") and _empty(item):
                continue
            out[f.metadata.get("json") or f.name] = to_json(item)
        return out
    if isinstance(value, list | tuple):
        return [to_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: to_json(item) for key, item in value.items()}
    return value


def text(properties: Mapping[str, Any] | None, key: str) -> str:
    """A string property, or "" when absent or not a string. Properties are a
    closed union per value: ``{"string": ...}``, ``{"int64": ...}`` and so on."""
    if not properties:
        return ""
    value = properties.get(key)
    if isinstance(value, Mapping):
        string = value.get("string")
        if isinstance(string, str):
            return string
    return ""


def prop(record: Mapping[str, Any] | None, key: str) -> str:
    """A node's or edge's string property."""
    return text(record.get("properties") if record else None, key)


def span_line(record: Mapping[str, Any] | None, end: str = "start") -> int:
    """The line where a record's source span starts (or ends), 0 without one."""
    source = (record or {}).get("source") or {}
    return int(((source.get("span") or {}).get(end) or {}).get("line") or 0)


def span_offset(record: Mapping[str, Any] | None, end: str = "start") -> int:
    source = (record or {}).get("source") or {}
    return int(((source.get("span") or {}).get(end) or {}).get("byte_offset") or 0)


def content_sha256(record: Mapping[str, Any] | None) -> str:
    return str(((record or {}).get("source") or {}).get("content_sha256") or "")


def graph_id(kind: str, *parts: str) -> str:
    """The worker's identifier for a kind and ordered parts: kind ":" base64url
    of the first 16 bytes of SHA-256 over the parts' JSON array."""
    encoded = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    digest = hashlib.sha256(encoded.encode()).digest()[:16]
    return kind + ":" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def valid_id(value: str) -> bool:
    """Whether an identifier has the kind:token shape and a bounded length."""
    if not value or len(value) > 256:
        return False
    kind, sep, token = value.partition(":")
    if not sep or not kind or not token:
        return False
    if not all("a" <= c <= "z" or c == "_" for c in kind):
        return False
    return all(c.isascii() and (c.isalnum() or c in "-_") for c in token)


def valid_repository(value: str) -> bool:
    """A repository id as the store accepts it: printable, without spaces."""
    return bool(value) and len(value) <= 256 and all("!" <= c <= "~" for c in value)


@dataclass(frozen=True, slots=True)
class Version:
    """One stored version of a record. ``gen_to`` is 0 while the version is
    open, else the generation that replaced it (``retired`` False) or retired
    it (``retired`` True)."""

    fact: dict[str, Any]
    gen_from: int
    commit_from: str = ""
    lineage: str = ""
    gen_to: int = 0
    commit_to: str = ""
    retired: bool = False
    fact_digest: str = ""

    @property
    def node(self) -> dict[str, Any] | None:
        return self.fact.get("node")

    @property
    def edge(self) -> dict[str, Any] | None:
        return self.fact.get("edge")

    @property
    def key(self) -> tuple[str, str]:
        if self.node is not None:
            return NODE, str(self.node.get("id", ""))
        if self.edge is not None:
            return EDGE, str(self.edge.get("id", ""))
        return "", ""

    @property
    def id(self) -> str:
        return self.key[1]

    def open_at(self, generation: int) -> bool:
        return self.gen_from <= generation and (self.gen_to == 0 or self.gen_to > generation)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"fact": self.fact}
        if self.lineage:
            out["lineage"] = self.lineage
        out["gen_from"] = self.gen_from
        if self.gen_to:
            out["gen_to"] = self.gen_to
        out["commit_from"] = self.commit_from
        if self.commit_to:
            out["commit_to"] = self.commit_to
        if self.retired:
            out["retired"] = True
        if self.fact_digest:
            out["fact_digest"] = self.fact_digest
        return out

    @staticmethod
    def decode_fact(payload: bytes, kind: str, record_id: str) -> dict[str, Any]:
        """A stored payload's fact, checked against the row's key."""
        try:
            fact = json.loads(payload)
        except ValueError as exc:
            raise IntegrityFailure(f"record {kind} {record_id} payload: {exc}") from None
        if not isinstance(fact, dict) or (fact.get("node") is None) == (fact.get("edge") is None):
            raise IntegrityFailure(f"record {kind} {record_id} payload is not one node or edge")
        body = fact.get(kind)
        if not isinstance(body, dict) or body.get("id") != record_id:
            raise IntegrityFailure(f"record {kind} {record_id} payload key differs")
        return fact


@dataclass(frozen=True, slots=True)
class RepositoryState:
    """The read gate: readers see the versions open at ``live_generation``."""

    repository_id: str = jfield("")
    branch: str = jfield("", omitempty=True)
    live_generation: int = jfield(0)
    live_commit: str = jfield("", omitempty=True)
    live_run_id: str = jfield("", omitempty=True)


@dataclass(frozen=True, slots=True)
class NeighborQuery:
    repository_id: str
    node_id: str
    direction: str = BOTH
    edge_kinds: tuple[str, ...] = ()
    generation: int = 0
    limit: int = 0
    cursor: str = ""


@dataclass(slots=True)
class Neighbor:
    edge: Version = jfield()
    # The far endpoint, when it exists at the generation.
    node: Version | None = jfield(None, omitempty=True)


@dataclass(slots=True)
class NeighborPage:
    generation: int = jfield(0)
    neighbors: list[Neighbor] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


@dataclass(frozen=True, slots=True)
class CrossEnd:
    """One end of a cross-repository link: a node of a repository's graph, with
    the qualified name and kind that find it again under a new id."""

    repository_id: str = jfield("")
    node_id: str = jfield("")
    qualified_name: str = jfield("", omitempty=True)
    kind: str = jfield("", omitempty=True)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CrossEnd:
        return cls(
            repository_id=str(value.get("repository_id") or ""),
            node_id=str(value.get("node_id") or ""),
            qualified_name=str(value.get("qualified_name") or ""),
            kind=str(value.get("kind") or ""),
        )


@dataclass(frozen=True, slots=True)
class CrossLink:
    """A link people recorded from a node in one repository's graph to a node
    in another's: the client method to the handler serving its API, or the
    publisher to the listener of its events."""

    id: str = jfield("")
    owner: str = jfield("")
    kind: str = jfield("")
    source: CrossEnd = jfield(factory=CrossEnd)
    target: CrossEnd = jfield(factory=CrossEnd)
    label: str = jfield("", omitempty=True)
    provenance: str = jfield("")
    created_by: str = jfield("", omitempty=True)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CrossLink:
        return cls(
            id=str(value.get("id") or ""),
            owner=str(value.get("owner") or ""),
            kind=str(value.get("kind") or ""),
            source=CrossEnd.from_json(value.get("source") or {}),
            target=CrossEnd.from_json(value.get("target") or {}),
            label=str(value.get("label") or ""),
            provenance=str(value.get("provenance") or ""),
            created_by=str(value.get("created_by") or ""),
        )

    def end(self, direction: str) -> CrossEnd:
        """The link's end on one side: its source for out, its target for in."""
        return self.target if direction == IN else self.source

    def far(self, direction: str) -> CrossEnd:
        """The end opposite the side a walk arrives from."""
        return self.source if direction == IN else self.target


@dataclass(frozen=True, slots=True)
class CrossLinkQuery:
    """The links touching a repository on one side (or both), narrowed to some
    nodes by id or qualified name; without either, every link on that side."""

    repository_id: str
    direction: str
    node_ids: tuple[str, ...] = ()
    qualified_names: tuple[str, ...] = ()

    def matches(self, link: CrossLink, direction: str) -> bool:
        end = link.end(direction)
        if end.repository_id != self.repository_id:
            return False
        if not self.node_ids and not self.qualified_names:
            return True
        if end.node_id in self.node_ids:
            return True
        return bool(end.qualified_name) and end.qualified_name in self.qualified_names
