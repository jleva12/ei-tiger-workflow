"""Explore: a question answered with evidence rather than prose.

Hybrid search finds seeds, the strongest seeds are expanded one semantic hop
in both directions, and everything is summarized with spans so the agent can
read source or walk further. Every related node arrives through a stored edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph import codesearch
from forge_codegraph_mcp.graph.embedding import Embedder
from forge_codegraph_mcp.graph.errors import InvalidRequest
from forge_codegraph_mcp.graph.model import (
    BOTH,
    IN,
    OUT,
    SEMANTIC_EDGE_KINDS,
    NeighborQuery,
    jfield,
    prop,
)
from forge_codegraph_mcp.graph.query.compact import (
    Brief,
    Summary,
    brief_of_summary,
    summarize,
    truncate,
)
from forge_codegraph_mcp.graph.query.search import search
from forge_codegraph_mcp.graph.store import SEARCH_HYBRID, GraphStore

DEFAULT_SEEDS = 8
MAX_SEEDS = 25
DEFAULT_EXPAND = 5
MAX_EXPAND = 10
DEFAULT_NEIGHBORS = 20
MAX_NEIGHBORS = 100
MAX_QUESTION_BYTES = 8000
DOC_SUMMARY_BYTES = 400


@dataclass(slots=True)
class Seed:
    """A search hit chosen as an anchor for the question."""

    summary: Summary = jfield(factory=Summary, inline=True)
    repository_id: str = jfield("")
    score: float = jfield(0.0)
    lexical: float = jfield(0.0, omitempty=True)
    vector: float = jfield(0.0, omitempty=True)
    exact_match: bool = jfield(False, omitempty=True)
    name_match: bool = jfield(False, omitempty=True)
    callers: int = jfield(0)
    callees: int = jfield(0)
    matched: list[str] = jfield(factory=list, omitempty=True)
    snippet: str = jfield("", omitempty=True)
    documentation: str = jfield("", omitempty=True)
    expanded: bool = jfield(False)


@dataclass(slots=True)
class Related:
    """A node one semantic edge from an expanded seed; direction "in": it
    points at the seed, "out": the seed points at it."""

    summary: Summary = jfield(factory=Summary, inline=True)
    repository_id: str = jfield("")
    seed_id: str = jfield("")
    edge_id: str = jfield("")
    via: str = jfield("")
    direction: str = jfield("")


@dataclass(slots=True)
class Link:
    """A semantic edge between two seeds."""

    repository_id: str = jfield("")
    edge_id: str = jfield("")
    via: str = jfield("")
    source_id: str = jfield("")
    target_id: str = jfield("")


@dataclass(slots=True)
class FileGroup:
    repository_id: str = jfield("")
    file_path: str = jfield("")
    language: str = jfield("", omitempty=True)
    seeds: int = jfield(0)
    related: int = jfield(0)


@dataclass(slots=True)
class ExploreResult:
    question: str = jfield("")
    query: codesearch.Query = jfield(factory=codesearch.Query)
    semantic: bool = jfield(False)
    expanded_terms: list[str] = jfield(factory=list, omitempty=True)
    seeds: list[Seed] = jfield(factory=list)
    related: list[Related] = jfield(factory=list)
    links: list[Link] = jfield(factory=list, omitempty=True)
    files: list[FileGroup] = jfield(factory=list)
    # An expanded seed had more neighbors than returned.
    truncated: bool = jfield(False)


async def explore(
    store: GraphStore,
    embedder: Embedder | None,
    *,
    repository_ids: list[str],
    question: str,
    limit: int = 0,
    expand: int = 0,
    neighbors: int = 0,
    kinds: list[str] | None = None,
    path_prefix: str = "",
    near_path: str = "",
    mode: str = "",
    enhance_query: bool = False,
    expand_query: bool = False,
) -> ExploreResult:
    if (
        not repository_ids
        or not question
        or len(question.encode()) > MAX_QUESTION_BYTES
        or not 0 <= limit <= MAX_SEEDS
        or not 0 <= expand <= MAX_EXPAND
        or not 0 <= neighbors <= MAX_NEIGHBORS
    ):
        raise InvalidRequest(
            f"explore needs repositories, a question of at most {MAX_QUESTION_BYTES} bytes, "
            f"limit 0-{MAX_SEEDS}, expand 0-{MAX_EXPAND} and neighbors 0-{MAX_NEIGHBORS}"
        )
    limit = limit or DEFAULT_SEEDS
    expand = expand or DEFAULT_EXPAND
    neighbors = neighbors or DEFAULT_NEIGHBORS
    parsed = codesearch.parse_query(question)
    found = await search(
        store,
        embedder,
        repository_ids=repository_ids,
        text=parsed.text,
        mode=mode or SEARCH_HYBRID,
        limit=limit,
        kinds=kinds,
        path_prefix=path_prefix,
        near_path=near_path,
        enhance_query=enhance_query,
        expand=expand_query,
    )
    result = ExploreResult(
        question=parsed.text,
        query=parsed,
        semantic=found.semantic,
        expanded_terms=found.expanded_terms,
    )
    seed_keys: set[tuple[str, str]] = set()
    for hit in found.hits:
        node = hit.node.node if hit.node is not None else None
        if node is None:
            continue
        result.seeds.append(
            Seed(
                summary=summarize(node),
                repository_id=hit.repository_id,
                score=hit.score,
                lexical=hit.lexical,
                vector=hit.vector,
                exact_match=hit.exact_match,
                name_match=hit.name_match,
                callers=hit.callers,
                callees=hit.callees,
                matched=hit.matched,
                snippet=hit.snippet,
                documentation=truncate(prop(node, "docstring"), DOC_SUMMARY_BYTES),
            )
        )
        seed_keys.add((hit.repository_id, str(node["id"])))

    # Exact matches first, then the best fused scores: the hits arrive in that
    # order, so it is kept.
    seen_related: set[tuple[str, str]] = set()
    seen_links: set[tuple[str, str]] = set()
    for seed in result.seeds[:expand]:
        seed_id = seed.summary.id
        page = await store.neighbors(
            NeighborQuery(
                repository_id=seed.repository_id,
                node_id=seed_id,
                direction=BOTH,
                edge_kinds=SEMANTIC_EDGE_KINDS,
                limit=neighbors,
            )
        )
        seed.expanded = True
        if page.next_cursor:
            result.truncated = True
        for neighbor in page.neighbors:
            edge = neighbor.edge.edge
            far = neighbor.node.node if neighbor.node is not None else None
            if far is None or edge is None:
                continue
            if (seed.repository_id, far["id"]) in seed_keys:
                if (seed.repository_id, edge["id"]) not in seen_links:
                    seen_links.add((seed.repository_id, edge["id"]))
                    result.links.append(
                        Link(
                            repository_id=seed.repository_id,
                            edge_id=edge["id"],
                            via=edge.get("kind") or "",
                            source_id=edge.get("source_id") or "",
                            target_id=edge.get("target_id") or "",
                        )
                    )
                continue
            if (seed.repository_id, far["id"]) not in seen_related:
                seen_related.add((seed.repository_id, far["id"]))
                result.related.append(
                    Related(
                        summary=summarize(far),
                        repository_id=seed.repository_id,
                        seed_id=seed_id,
                        edge_id=edge["id"],
                        via=edge.get("kind") or "",
                        direction=IN if edge.get("target_id") == seed_id else OUT,
                    )
                )
    result.files = group_files(result.seeds, result.related)
    return result


def group_files(seeds: list[Seed], related: list[Related]) -> list[FileGroup]:
    """Seeds and related nodes counted per file, most relevant first."""
    groups: dict[tuple[str, str], FileGroup] = {}

    def group(repo: str, path: str, language: str) -> FileGroup:
        return groups.setdefault(
            (repo, path), FileGroup(repository_id=repo, file_path=path, language=language)
        )

    for seed in seeds:
        if seed.summary.file_path:
            group(seed.repository_id, seed.summary.file_path, seed.summary.language).seeds += 1
    for item in related:
        if item.summary.file_path:
            group(item.repository_id, item.summary.file_path, item.summary.language).related += 1
    return sorted(
        groups.values(), key=lambda g: (-g.seeds, -g.related, g.repository_id, g.file_path)
    )


@dataclass(slots=True)
class CompactSeed:
    brief: Brief = jfield(factory=Brief, inline=True)
    repository_id: str = jfield("")
    score: float = jfield(0.0)
    exact_match: bool = jfield(False, omitempty=True)
    expanded: bool = jfield(False)
    snippet: str = jfield("", omitempty=True)


@dataclass(slots=True)
class CompactRelated:
    brief: Brief = jfield(factory=Brief, inline=True)
    seed_id: str = jfield("")
    via: str = jfield("")
    direction: str = jfield("")


@dataclass(slots=True)
class CompactExploreResult:
    """An explore result without records, signatures, documentation or byte
    spans: briefs, scores and edges."""

    question: str = jfield("")
    semantic: bool = jfield(False)
    expanded_terms: list[str] = jfield(factory=list, omitempty=True)
    seeds: list[CompactSeed] = jfield(factory=list)
    related: list[CompactRelated] = jfield(factory=list)
    links: list[Link] = jfield(factory=list, omitempty=True)
    files: list[FileGroup] = jfield(factory=list)
    truncated: bool = jfield(False)


def compact_explore(result: ExploreResult) -> CompactExploreResult:
    return CompactExploreResult(
        question=result.question,
        semantic=result.semantic,
        expanded_terms=result.expanded_terms,
        seeds=[
            CompactSeed(
                brief=brief_of_summary(seed.summary),
                repository_id=seed.repository_id,
                score=seed.score,
                exact_match=seed.exact_match,
                expanded=seed.expanded,
                snippet=seed.snippet,
            )
            for seed in result.seeds
        ],
        related=[
            CompactRelated(
                brief=brief_of_summary(item.summary),
                seed_id=item.seed_id,
                via=item.via,
                direction=item.direction,
            )
            for item in result.related
        ],
        links=result.links,
        files=result.files,
        truncated=result.truncated,
    )
