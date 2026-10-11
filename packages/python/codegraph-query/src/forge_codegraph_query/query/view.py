"""A bounded sample of a repository's graph, for the visual explorer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from forge_codegraph_query.errors import InvalidRequest
from forge_codegraph_query.model import BOTH, NeighborQuery, Version, jfield
from forge_codegraph_query.store import GraphStore

SEEDS = 12  # nodes per page
SEED_NEIGHBORS = 24  # edges read around each seed
MAX_VIEW_NODES = 160
VIEW_SECONDS = 20.0


@dataclass(slots=True)
class GraphView:
    repository_id: str = jfield("")
    branch: str = jfield("", omitempty=True)
    commit_sha: str = jfield("", omitempty=True)
    generation: int = jfield(0)
    nodes: list[Version] = jfield(factory=list)
    edges: list[Version] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)
    # More nodes or edges exist than the sample holds.
    truncated: bool = jfield(False)


async def graph_view(
    store: GraphStore,
    *,
    repository_id: str,
    generation: int = 0,
    kind: str = "",
    cursor: str = "",
) -> GraphView:
    """Twelve seed nodes, of one kind or any, and one bounded neighborhood
    around each, at one generation (live for 0), resolved once before any
    read. Kind counts in the explorer describe this sample, not the
    repository's totals; ``next_cursor`` pages the seeds, and the neighbors
    read expands any node."""
    out = GraphView(repository_id=repository_id)
    if not repository_id or len(kind) > 128 or len(cursor) > 8192:
        raise InvalidRequest("repository, kind or cursor")
    async with asyncio.timeout(VIEW_SECONDS):
        state = await store.state(repository_id)
        if generation > state.live_generation:
            raise InvalidRequest("generation is not published")
        generation = generation or state.live_generation
        out.generation, out.branch = generation, state.branch
        if generation == 0:
            return out
        out.commit_sha = state.live_commit
        if generation != state.live_generation:
            out.commit_sha = await store.generation_commit(repository_id, generation)
        page = await store.list_nodes(repository_id, generation, kind, SEEDS, cursor)
        if page.generation != generation:
            raise InvalidRequest("graph generation changed")
        out.next_cursor, out.truncated = page.next_cursor, bool(page.next_cursor)
        nodes = {v.id: v for v in page.nodes if v.node is not None}
        edges: dict[str, Version] = {}
        for seed in page.nodes:
            if seed.node is None:
                continue
            around = await store.neighbors(
                NeighborQuery(
                    repository_id=repository_id,
                    node_id=seed.id,
                    direction=BOTH,
                    generation=generation,
                    limit=SEED_NEIGHBORS,
                )
            )
            if around.generation != generation:
                raise InvalidRequest("graph generation changed")
            out.truncated = out.truncated or bool(around.next_cursor)
            for neighbor in around.neighbors:
                edge = neighbor.edge.edge
                if edge is None:
                    continue
                far = neighbor.node
                if (
                    far is not None
                    and far.node is not None
                    and (far.id in nodes or len(nodes) < MAX_VIEW_NODES)
                ):
                    nodes[far.id] = far
                if edge.get("source_id") not in nodes or edge.get("target_id") not in nodes:
                    out.truncated = True
                    continue
                edges[str(edge["id"])] = neighbor.edge
    out.nodes = [nodes[node_id] for node_id in sorted(nodes)]
    out.edges = [edges[edge_id] for edge_id in sorted(edges)]
    return out
