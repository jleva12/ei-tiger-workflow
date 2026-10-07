"""The compact forms answers take by default: enough of each node to decide
the next hop and to ask for more. ``full: true`` returns the records instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge_codegraph_mcp.graph.model import (
    IN,
    OUT,
    NeighborPage,
    content_sha256,
    jfield,
    prop,
    span_line,
    span_offset,
)
from forge_codegraph_mcp.graph.store import SearchHit


@dataclass(slots=True)
class Summary:
    """The part of a node needed to decide whether to look closer: identity,
    location and signature, never the body."""

    id: str = jfield("")
    kind: str = jfield("")
    name: str = jfield("", omitempty=True)
    qualified_name: str = jfield("", omitempty=True)
    file_path: str = jfield("", omitempty=True)
    language: str = jfield("", omitempty=True)
    signature: str = jfield("", omitempty=True)
    start_line: int = jfield(0, omitempty=True)
    end_line: int = jfield(0, omitempty=True)
    byte_start: int = jfield(0, omitempty=True)
    byte_end: int = jfield(0, omitempty=True)
    content_sha256: str = jfield("", omitempty=True)


def summarize(node: dict[str, Any]) -> Summary:
    return Summary(
        id=str(node.get("id") or ""),
        kind=str(node.get("kind") or ""),
        name=str(node.get("name") or ""),
        qualified_name=str(node.get("qualified_name") or ""),
        file_path=prop(node, "file_path"),
        language=prop(node, "language"),
        signature=prop(node, "signature"),
        start_line=span_line(node, "start"),
        end_line=span_line(node, "end"),
        byte_start=span_offset(node, "start"),
        byte_end=span_offset(node, "end"),
        content_sha256=content_sha256(node),
    )


def truncate(value: str, limit: int) -> str:
    """Bounds text to ``limit`` UTF-8 bytes on a character boundary."""
    data = value.encode()
    if len(data) <= limit:
        return value
    cut = limit
    while 0 < cut < len(data) and data[cut] & 0xC0 == 0x80:
        cut -= 1
    return data[:cut].decode() + "…"


@dataclass(slots=True)
class Brief:
    """A node in compact form. Spans, hashes and properties come from get_node."""

    id: str = jfield("")
    kind: str = jfield("")
    qualified_name: str = jfield("", omitempty=True)
    # Only when the qualified name doesn't start with it.
    name: str = jfield("", omitempty=True)
    file: str = jfield("", omitempty=True)
    line: int = jfield(0, omitempty=True)


def has_prefix_word(qualified: str, name: str) -> bool:
    """Whether a qualified name starts with the name as a whole token, as
    "placeOrder(shop.Cart)" starts with "placeOrder"."""
    if not qualified.startswith(name):
        return False
    return len(qualified) == len(name) or qualified[len(name)] in "(.:<["


def _brief(node_id: str, kind: str, name: str, qualified: str, file: str, line: int) -> Brief:
    brief = Brief(id=node_id, kind=kind, qualified_name=qualified, file=file, line=line)
    if not qualified or (name and not has_prefix_word(qualified, name)):
        brief.name = name
    return brief


def briefly(node: dict[str, Any]) -> Brief:
    return _brief(
        str(node.get("id") or ""),
        str(node.get("kind") or ""),
        str(node.get("name") or ""),
        str(node.get("qualified_name") or ""),
        prop(node, "file_path"),
        span_line(node, "start"),
    )


def brief_of_summary(summary: Summary) -> Brief:
    return _brief(
        summary.id,
        summary.kind,
        summary.name,
        summary.qualified_name,
        summary.file_path,
        summary.start_line,
    )


@dataclass(slots=True)
class CompactNeighbor:
    """One edge of a node: the far node in brief, the edge kind, which way it
    points ("in": the far node points at the node) and the line of the
    occurrence that proves it."""

    brief: Brief = jfield(factory=Brief, inline=True)
    via: str = jfield("")
    direction: str = jfield("")
    edge_id: str = jfield("")
    at_line: int = jfield(0, omitempty=True)


@dataclass(slots=True)
class CompactNeighborPage:
    generation: int = jfield(0)
    neighbors: list[CompactNeighbor] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


def compact_neighbors(page: NeighborPage, node_id: str) -> CompactNeighborPage:
    """A neighbor page around ``node_id`` without records. An edge whose far
    node isn't open at the generation keeps only its id."""
    out = CompactNeighborPage(generation=page.generation, next_cursor=page.next_cursor)
    for neighbor in page.neighbors:
        edge = neighbor.edge.edge
        if edge is None:
            continue
        item = CompactNeighbor(
            via=str(edge.get("kind") or ""), edge_id=str(edge["id"]), direction=OUT
        )
        far = str(edge.get("target_id") or "")
        if edge.get("target_id") == node_id and edge.get("source_id") != node_id:
            item.direction, far = IN, str(edge.get("source_id") or "")
        far_node = neighbor.node.node if neighbor.node is not None else None
        item.brief = briefly(far_node) if far_node is not None else Brief(id=far)
        item.at_line = span_line(edge, "start")
        out.neighbors.append(item)
    return out


@dataclass(slots=True)
class CompactHit:
    """A search hit without the record."""

    brief: Brief = jfield(factory=Brief, inline=True)
    repository_id: str = jfield("")
    score: float = jfield(0.0)
    exact_match: bool = jfield(False, omitempty=True)
    name_match: bool = jfield(False, omitempty=True)
    callers: int = jfield(0)
    callees: int = jfield(0)
    matched: list[str] = jfield(factory=list, omitempty=True)
    snippet: str = jfield("", omitempty=True)


def compact_hits(hits: list[SearchHit]) -> list[CompactHit]:
    out = []
    for hit in hits:
        item = CompactHit(
            repository_id=hit.repository_id,
            score=hit.score,
            exact_match=hit.exact_match,
            name_match=hit.name_match,
            callers=hit.callers,
            callees=hit.callees,
            matched=hit.matched,
            snippet=hit.snippet,
        )
        if hit.node is not None and hit.node.node is not None:
            item.brief = briefly(hit.node.node)
        out.append(item)
    return out
