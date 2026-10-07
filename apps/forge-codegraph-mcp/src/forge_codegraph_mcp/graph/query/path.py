"""Path: the shortest chain of proven edges from one declaration to another."""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph.errors import InvalidRequest
from forge_codegraph_mcp.graph.model import (
    BOTH,
    EDGE_CONTAINS,
    IN,
    OUT,
    SEMANTIC_EDGE_KINDS,
    jfield,
)
from forge_codegraph_mcp.graph.query.compact import Brief, briefly
from forge_codegraph_mcp.graph.store import EdgeRef, GraphStore

DEFAULT_HOPS = 4
MAX_HOPS = 8
# Nodes both sides may discover together before the search gives up.
MAX_VISITED = 5000
EDGE_BUDGET = 20000


@dataclass(slots=True)
class PathEdge:
    edge_id: str = jfield("")
    kind: str = jfield("")
    source_id: str = jfield("")
    target_id: str = jfield("")


@dataclass(slots=True)
class PathResult:
    """The shortest path found, in order from ``from`` to ``to``, or found
    false; visited counts the nodes the search discovered."""

    from_: str = jfield("", name="from")
    to: str = jfield("")
    found: bool = jfield(False)
    hops: int = jfield(0)
    nodes: list[Brief] = jfield(factory=list)
    edges: list[PathEdge] = jfield(factory=list)
    visited: int = jfield(0)
    truncated: bool = jfield(False)


@dataclass(slots=True)
class _Side:
    frontier: list[str]
    visited: set[str]
    # The edge that discovered each node.
    parent: dict[str, EdgeRef]
    hops: int = 0


def _edge(ref: EdgeRef) -> PathEdge:
    return PathEdge(edge_id=ref.id, kind=ref.kind, source_id=ref.source_id, target_id=ref.target_id)


async def path(
    store: GraphStore,
    *,
    repository_id: str,
    from_id: str,
    to_id: str,
    generation: int = 0,
    max_hops: int = 0,
    kinds: list[str] | None = None,
    direction: str = "",
) -> PathResult:
    """A bidirectional breadth-first search: the forward side follows edges
    from ``from_id``, the backward side into ``to_id``, each expanding one
    level per query over the edge indexes, until they meet, ``max_hops``
    levels are expanded, or the visited bound is hit. Direction out (the
    default) follows dependencies; both ignores edge direction. Kinds default
    to the semantic kinds plus contains, so a path can descend from a type
    into the member that holds the dependency."""
    if not 0 <= max_hops <= MAX_HOPS:
        raise InvalidRequest(f"path max_hops 0-{MAX_HOPS}")
    max_hops = max_hops or DEFAULT_HOPS
    direction = direction or OUT
    if direction not in (OUT, BOTH):
        raise InvalidRequest("path direction must be out or both")
    followed = list(kinds) if kinds else [*SEMANTIC_EDGE_KINDS, EDGE_CONTAINS]
    for node_id in (from_id, to_id):
        await store.get_node(repository_id, node_id, generation)
    result = PathResult(from_=from_id, to=to_id)
    if from_id == to_id:
        return await _finish(store, repository_id, generation, result, [from_id], [])
    forward = _Side(frontier=[from_id], visited={from_id}, parent={})
    backward = _Side(frontier=[to_id], visited={to_id}, parent={})
    meet = ""
    while (
        not meet
        and forward.hops + backward.hops < max_hops
        and forward.frontier
        and backward.frontier
    ):
        # Expand the smaller frontier; ties go to the forward side.
        side, other, walk = forward, backward, OUT
        if len(backward.frontier) < len(forward.frontier):
            side, other, walk = backward, forward, IN
        if direction == BOTH:
            walk = BOTH
        refs, cut = await store.dependency_edges(
            repository_id, side.frontier, walk, followed, generation, EDGE_BUDGET
        )
        if cut:
            result.truncated = True
        in_frontier = set(side.frontier)
        following: list[str] = []
        for ref in refs:
            far = ref.target_id
            if side is backward or (direction == BOTH and ref.source_id not in in_frontier):
                far = ref.source_id
            if side is backward and direction == BOTH and ref.target_id not in in_frontier:
                far = ref.target_id
            if not far or far in side.visited:
                continue
            side.visited.add(far)
            side.parent[far] = ref
            following.append(far)
            if far in other.visited:
                meet = far
                break
        side.frontier = following
        side.hops += 1
        result.visited = len(forward.visited) + len(backward.visited) - 2
        if result.visited > MAX_VISITED:
            result.truncated = True
            break
    if not meet:
        return result
    # Walk back from the meeting node to each end and stitch the halves.
    ids: list[str] = []
    edges: list[PathEdge] = []
    node_id = meet
    while node_id != from_id:
        step = forward.parent.get(node_id)
        if step is None:
            return result
        edges.insert(0, _edge(step))
        ids.insert(0, node_id)
        node_id = step.source_id
        if direction == BOTH and node_id == ids[0]:
            node_id = step.target_id
    ids.insert(0, from_id)
    node_id = meet
    while node_id != to_id:
        step = backward.parent.get(node_id)
        if step is None:
            return result
        edges.append(_edge(step))
        following_id = step.target_id
        if direction == BOTH and following_id == node_id:
            following_id = step.source_id
        ids.append(following_id)
        node_id = following_id
    result.found, result.hops = True, len(edges)
    return await _finish(store, repository_id, generation, result, ids, edges)


async def _finish(
    store: GraphStore,
    repo: str,
    generation: int,
    result: PathResult,
    ids: list[str],
    edges: list[PathEdge],
) -> PathResult:
    nodes = await store.get_nodes(repo, ids, generation)
    for node_id in ids:
        version = nodes.get(node_id)
        node = version.node if version is not None else None
        result.nodes.append(briefly(node) if node is not None else Brief(id=node_id))
    result.edges = edges
    if len(ids) == 1:
        result.found = True
    return result
