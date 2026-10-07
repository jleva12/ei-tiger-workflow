"""What a published generation changed, and what those changes affect."""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph.errors import InvalidRequest
from forge_codegraph_mcp.graph.model import NODE, jfield
from forge_codegraph_mcp.graph.query.compact import Brief, briefly
from forge_codegraph_mcp.graph.query.impact import (
    DEFAULT_DEPTH,
    DEFAULT_LIMIT,
    MAX_DEPTH,
    MAX_LIMIT,
    CompactImpactResult,
    ImpactResult,
    assess,
    compact_impact,
    impact_from,
    profile_for,
)
from forge_codegraph_mcp.graph.store import CHANGE_RETIRED, GraphStore

DEFAULT_CHANGES = 200
MAX_CHANGES = 2000
CHANGES_PAGE = 500
DEFAULT_ROOTS = 500
MAX_ROOTS = 2000

# What a developer recognizes as a changed declaration: not occurrences,
# chunks or the symbols a repository merely refers to.
DECLARATION_KINDS = frozenset(
    {
        "class", "interface", "enum", "record", "annotation_type", "type_alias", "namespace",
        "method", "constructor", "function", "field", "variable", "enum_constant", "initializer",
    }
)  # fmt: skip


@dataclass(slots=True)
class ChangedNode:
    brief: Brief = jfield(factory=Brief, inline=True)
    op: str = jfield("")


@dataclass(slots=True)
class ChangeSet:
    """A page of a generation's changed declarations, with the generation's
    totals over all its node records."""

    generation: int = jfield(0)
    commit: str = jfield("", omitempty=True)
    added: int = jfield(0)
    updated: int = jfield(0)
    retired: int = jfield(0)
    nodes: list[ChangedNode] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


async def changes(
    store: GraphStore,
    *,
    repository_id: str,
    generation: int = 0,
    kinds: list[str] | None = None,
    limit: int = 0,
    cursor: str = "",
) -> ChangeSet:
    """Pages through the generation's node changes, keeping the kinds asked
    for (declarations by default): at least ``limit`` when more exist, and
    possibly a few more from the last page read."""
    if not 0 <= limit <= MAX_CHANGES:
        raise InvalidRequest(f"changes limit 0-{MAX_CHANGES}")
    limit = limit or DEFAULT_CHANGES
    keep = set(kinds) if kinds else DECLARATION_KINDS
    out = ChangeSet()
    while True:
        page = await store.changes(repository_id, generation, NODE, CHANGES_PAGE, cursor)
        out.generation, out.added = page.generation, page.added
        out.updated, out.retired = page.updated, page.retired
        out.commit = out.commit or page.commit
        for change in page.changes:
            node = change.version.node if change.version is not None else None
            if node is None or node.get("kind") not in keep:
                continue
            out.nodes.append(ChangedNode(brief=briefly(node), op=change.op))
        out.next_cursor = page.next_cursor
        if not page.next_cursor or len(out.nodes) >= limit:
            return out
        cursor = page.next_cursor


@dataclass(slots=True)
class ChangeImpactResult:
    generation: int = jfield(0)
    commit: str = jfield("", omitempty=True)
    changed: list[ChangedNode] = jfield(factory=list)
    roots_truncated: bool = jfield(False)
    impact: ImpactResult = jfield(factory=ImpactResult)


@dataclass(slots=True)
class CompactChangeImpactResult:
    generation: int = jfield(0)
    commit: str = jfield("", omitempty=True)
    changed: list[ChangedNode] = jfield(factory=list)
    roots_truncated: bool = jfield(False)
    impact: CompactImpactResult = jfield(factory=CompactImpactResult)


async def change_impact(
    store: GraphStore,
    *,
    repository_id: str,
    generation: int = 0,
    change: str = "",
    depth: int = 0,
    limit: int = 0,
    max_roots: int = 0,
    include_locals: bool = False,
) -> ChangeImpactResult:
    """Walks the dependants of a generation's added and updated declarations
    at the generation, and of its retired ones at the generation before
    (where their dependants still exist), merged with each node once (first
    reach wins) and assessed at the generation."""
    if (
        not 0 <= depth <= MAX_DEPTH
        or not 0 <= limit <= MAX_LIMIT
        or not 0 <= max_roots <= MAX_ROOTS
    ):
        raise InvalidRequest(
            f"change impact depth 0-{MAX_DEPTH}, limit 0-{MAX_LIMIT} and max_roots 0-{MAX_ROOTS}"
        )
    profile = profile_for(change)
    depth = depth or DEFAULT_DEPTH
    limit = limit or DEFAULT_LIMIT
    max_roots = max_roots or DEFAULT_ROOTS
    found = await changes(
        store, repository_id=repository_id, generation=generation, limit=max_roots
    )
    result = ChangeImpactResult(
        generation=found.generation, commit=found.commit, changed=found.nodes
    )
    if len(result.changed) > max_roots:
        result.changed = result.changed[:max_roots]
        result.roots_truncated = True
    if found.next_cursor:
        result.roots_truncated = True
    live = [c.brief.id for c in result.changed if c.op != CHANGE_RETIRED]
    gone = [c.brief.id for c in result.changed if c.op == CHANGE_RETIRED]
    merged = ImpactResult(roots=live + gone, change=profile.name, note=profile.note)
    seen = set(merged.roots)

    async def walk(roots: list[str], at: int) -> None:
        if not roots:
            return
        part = await impact_from(
            store, repository_id, at, roots, profile, depth, limit, include_locals
        )
        merged.edges += part.edges
        merged.skipped_locals += part.skipped_locals
        merged.truncated = merged.truncated or part.truncated
        for hit in part.hits:
            hit_id = hit.node.id if hit.node is not None else ""
            if hit_id in seen:
                continue
            if len(merged.hits) >= limit:
                merged.truncated = True
                break
            seen.add(hit_id)
            merged.hits.append(hit)

    await walk(live, found.generation)
    if found.generation > 1:
        await walk(gone, found.generation - 1)
    await assess(store, repository_id, found.generation, merged)
    result.impact = merged
    return result


def compact_change_impact(result: ChangeImpactResult) -> CompactChangeImpactResult:
    return CompactChangeImpactResult(
        generation=result.generation,
        commit=result.commit,
        changed=result.changed,
        roots_truncated=result.roots_truncated,
        impact=compact_impact(result.impact),
    )
