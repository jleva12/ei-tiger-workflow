"""Following cross-repository links: code in one repository that reaches code
in another over the network, which people record as links."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from forge_codegraph_mcp.graph.errors import NotFound
from forge_codegraph_mcp.graph.model import (
    BOTH,
    IN,
    OUT,
    CrossEnd,
    CrossLink,
    CrossLinkQuery,
    Version,
    jfield,
)
from forge_codegraph_mcp.graph.query.compact import Brief, briefly
from forge_codegraph_mcp.graph.store import GraphStore

# Whether the caller may read a repository. Links into one it can't are left
# out, as if they weren't there.
type Readable = Callable[[str], bool]


@dataclass(slots=True)
class CrossHop:
    """A link followed from a node, and the node at its far end, read in its
    own repository at that repository's live generation. ``node`` is absent
    and ``stale`` set when the far node is gone."""

    link: CrossLink = jfield(factory=CrossLink)
    # out: the node links to the far one; in: the far one links to it.
    direction: str = jfield("")
    repository_id: str = jfield("")
    node: Version | None = jfield(None, omitempty=True)
    stale: bool = jfield(False, omitempty=True)


async def cross_hops(
    store: GraphStore,
    repo: str,
    node: dict[str, Any],
    direction: str,
    readable: Readable | None,
) -> list[CrossHop]:
    """The links touching a node, followed into the repositories the caller
    can read. A link finds the node by id, or by qualified name when an
    ingestion gave it a new id."""
    qualified = str(node.get("qualified_name") or "")
    query = CrossLinkQuery(
        repository_id=repo,
        direction=direction,
        node_ids=(str(node["id"]),),
        qualified_names=(qualified,) if qualified else (),
    )
    hops: list[CrossHop] = []
    for link in await store.cross_links(query):
        for side in (OUT, IN):
            if direction not in (BOTH, side) or not query.matches(link, side):
                continue
            far = link.far(side)
            if readable is not None and not readable(far.repository_id):
                continue
            version = await resolve_end(store, far)
            hops.append(
                CrossHop(
                    link=link,
                    direction=side,
                    repository_id=far.repository_id,
                    node=version,
                    stale=version is None,
                )
            )
    return hops


async def resolve_end(store: GraphStore, end: CrossEnd) -> Version | None:
    """A link's end at its repository's live generation: the node by id, else
    the one node of its kind with its qualified name; None when neither is
    there, including when the repository is gone."""
    try:
        nodes = await store.get_nodes(end.repository_id, [end.node_id], 0)
    except NotFound:
        return None
    version = nodes.get(end.node_id)
    # A retired node's last version isn't the node now.
    if version is not None and version.gen_to == 0 and not version.retired:
        return version
    if not end.qualified_name:
        return None
    try:
        found = await store.find_nodes(
            end.repository_id, "", end.qualified_name, [end.kind] if end.kind else None, 0, 2
        )
    except NotFound:
        return None
    return found[0] if len(found) == 1 else None


@dataclass(slots=True)
class CompactCrossHop:
    """A hop without records: the far node in brief with its repository, the
    link's kind, label and id, and which way it points."""

    brief: Brief = jfield(factory=Brief, inline=True)
    repository_id: str = jfield("")
    via: str = jfield("")
    direction: str = jfield("")
    label: str = jfield("", omitempty=True)
    link_id: str = jfield("")
    stale: bool = jfield(False, omitempty=True)


def compact_cross_hops(hops: list[CrossHop]) -> list[CompactCrossHop]:
    """A stale hop keeps the far node's id, qualified name and kind as the
    link recorded them."""
    out = []
    for hop in hops:
        item = CompactCrossHop(
            repository_id=hop.repository_id,
            via=hop.link.kind,
            direction=hop.direction,
            label=hop.link.label,
            link_id=hop.link.id,
            stale=hop.stale,
        )
        far_node = hop.node.node if hop.node is not None else None
        if far_node is not None:
            item.brief = briefly(far_node)
        else:
            far = hop.link.far(hop.direction)
            item.brief = Brief(id=far.node_id, kind=far.kind, qualified_name=far.qualified_name)
        out.append(item)
    return out
