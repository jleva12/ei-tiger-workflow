"""Hubs: the most depended-on declarations of a repository."""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph.errors import InvalidRequest
from forge_codegraph_mcp.graph.model import SEMANTIC_EDGE_KINDS, jfield
from forge_codegraph_mcp.graph.query.compact import Brief, briefly
from forge_codegraph_mcp.graph.store import GraphStore

DEFAULT_HUBS = 20
MAX_HUBS = 200

# Kinds of the symbols a repository only refers to: String and void would
# otherwise top every Java repository.
_EXTERNAL_PREFIXES = (
    "external_symbol", "intrinsic", "constructed_type", "unresolved_reference", "derived_",
)  # fmt: skip


def declared_kind(kind: str) -> bool:
    """Whether a node kind is something the repository itself declares."""
    return not kind.startswith(_EXTERNAL_PREFIXES)


@dataclass(slots=True)
class HubEntry:
    brief: Brief = jfield(factory=Brief, inline=True)
    in_degree: int = jfield(0)
    by_kind: dict[str, int] = jfield(factory=dict)


@dataclass(slots=True)
class HubsResult:
    edge_kinds: list[str] = jfield(factory=list)
    hubs: list[HubEntry] = jfield(factory=list)


async def hubs(
    store: GraphStore,
    *,
    repository_id: str,
    generation: int = 0,
    edge_kinds: list[str] | None = None,
    node_kinds: list[str] | None = None,
    limit: int = 0,
    include_external: bool = False,
) -> HubsResult:
    """Nodes ranked by incoming edges of ``edge_kinds`` (the semantic kinds
    by default). Without node kinds, symbols the repository doesn't declare
    are left out unless ``include_external``; a filter ranks the whole
    candidate window and keeps the matches, so a rare kind may return fewer
    than ``limit``."""
    if not 0 <= limit <= MAX_HUBS:
        raise InvalidRequest(f"hubs limit 0-{MAX_HUBS}")
    limit = limit or DEFAULT_HUBS
    # The aggregation costs the same whatever the limit, so rank the whole
    # window whenever a filter will drop some of it.
    fetch = MAX_HUBS if node_kinds or not include_external else limit
    ranked = await store.hubs(repository_id, generation, edge_kinds, fetch)
    nodes = await store.get_nodes(repository_id, [hub.node_id for hub in ranked], generation)
    keep = set(node_kinds or ())
    result = HubsResult(edge_kinds=list(edge_kinds or SEMANTIC_EDGE_KINDS))
    for hub in ranked:
        version = nodes.get(hub.node_id)
        node = version.node if version is not None else None
        if node is None:
            continue
        kind = str(node.get("kind") or "")
        if keep and kind not in keep:
            continue
        if not keep and not include_external and not declared_kind(kind):
            continue
        result.hubs.append(
            HubEntry(brief=briefly(node), in_degree=hub.in_degree, by_kind=hub.by_kind)
        )
        if len(result.hubs) == limit:
            break
    return result
