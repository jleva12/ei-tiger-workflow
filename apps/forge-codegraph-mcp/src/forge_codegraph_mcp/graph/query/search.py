"""Search as an agent asks it: hybrid search with the question embedded when
an embedder is configured, then optionally a second, lexical pass with the
repository's own vocabulary."""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph import codesearch
from forge_codegraph_mcp.graph.embedding import Embedder, embed_text
from forge_codegraph_mcp.graph.errors import GraphError, InvalidRequest, Unavailable
from forge_codegraph_mcp.graph.model import jfield, prop
from forge_codegraph_mcp.graph.store import (
    SEARCH_HYBRID,
    SEARCH_LEXICAL,
    GraphStore,
    SearchHit,
    SearchRequest,
)

# How many top hits lend their vocabulary to a query, how many terms are
# added, and the query length below which nothing is (short queries are
# usually identifiers, which are already exact).
EXPANSION_SEEDS = 5
EXPANSION_TERMS = 4
MIN_EXPANSION_QUERY = 3
RRF_K = 60.0

# Fragments that name no concept of a repository.
EXPANSION_STOPWORDS = frozenset(
    [
        "java",
        "lang",
        "util",
        "com",
        "org",
        "net",
        "string",
        "object",
        "int",
        "long",
        "boolean",
        "void",
        "list",
        "map",
        "get",
        "set",
        "new",
        "the",
        "and",
        "for",
        "with",
        "impl",
        "test",
        "main",
        "src",
    ]
)


@dataclass(slots=True)
class SearchResult:
    hits: list[SearchHit] = jfield(factory=list)
    query: codesearch.Query = jfield(factory=codesearch.Query)
    mode: str = jfield(SEARCH_HYBRID)
    # Whether the semantic branch ran.
    semantic: bool = jfield(False)
    expanded_terms: list[str] = jfield(factory=list, omitempty=True)


async def embed_question(embedder: Embedder, text: str) -> list[float]:
    try:
        return await embed_text(embedder, text)
    except GraphError:
        raise
    except Exception as exc:
        # Only the failure's type: provider errors can quote the request.
        raise Unavailable(
            f"the question could not be embedded ({type(exc).__name__}); retry, or search "
            "with mode lexical"
        ) from exc


async def search(
    store: GraphStore,
    embedder: Embedder | None,
    *,
    repository_ids: list[str],
    text: str,
    mode: str = "",
    limit: int = 0,
    kinds: list[str] | None = None,
    path_prefix: str = "",
    near_path: str = "",
    enhance_query: bool = False,
    expand: bool = False,
) -> SearchResult:
    """Hybrid search. With ``expand``, a first pass without an exact match
    adds the identifier fragments most common among its top hits to a
    lexical second pass, and the two rankings are fused."""
    text = text.strip()
    if not text:
        raise InvalidRequest("query is required")
    mode = mode or SEARCH_HYBRID
    request = SearchRequest(
        repository_ids=repository_ids,
        text=text,
        mode=mode,
        limit=limit,
        kinds=kinds,
        path_prefix=path_prefix,
        near_path=near_path,
        enhance_query=enhance_query,
    )
    result = SearchResult(query=codesearch.parse_query(text), mode=mode)
    if embedder is not None and mode != SEARCH_LEXICAL:
        request.model, request.vector = embedder.model, await embed_question(embedder, text)
        result.semantic = True
    result.hits = await store.hybrid_search(request)
    if not expand:
        return result
    terms = expansion_terms_for(result.query, result.hits)
    if not terms:
        return result
    second = SearchRequest(
        repository_ids=repository_ids,
        text=text + " " + " ".join(terms),
        mode=SEARCH_LEXICAL,
        limit=limit,
        kinds=kinds,
        path_prefix=path_prefix,
        near_path=near_path,
        enhance_query=enhance_query,
    )
    more = await store.hybrid_search(second)
    result.expanded_terms = terms
    result.hits = fuse_hits(result.hits, more, limit)
    return result


def expansion_terms_for(query: codesearch.Query, hits: list[SearchHit]) -> list[str]:
    """The identifier fragments most shared by the top hits' simple names that
    the query doesn't already contain; none when a hit matched exactly or the
    query is too short to be a question."""
    query_terms = codesearch.terms(query.text)
    if len(query_terms) < MIN_EXPANSION_QUERY:
        return []
    present = set(query_terms)
    counts: dict[str, int] = {}
    for i, hit in enumerate(hits):
        if hit.exact_match:
            return []
        if i == EXPANSION_SEEDS:
            break
        node = hit.node.node if hit.node is not None else None
        if node is None:
            continue
        # The simple name and its owning type's simple name lend fragments;
        # qualified names would add package segments, which name no concept.
        owner = prop(node, "owner_key")
        cut = max(owner.rfind("."), owner.rfind("$"))
        if cut >= 0:
            owner = owner[cut + 1 :]
        seen: set[str] = set()
        fragments = codesearch.identifier_terms(str(node.get("name") or ""), owner).lower().split()
        for term in fragments:
            if (
                len(term.encode()) < 3
                or term in present
                or term in seen
                or term in EXPANSION_STOPWORDS
                or any(c in term for c in ".()<>[]$,")
            ):
                continue
            seen.add(term)
            counts[term] = counts.get(term, 0) + 1
    # Stable: equally common terms keep the order they were first seen in.
    order = sorted(counts, key=lambda term: -counts[term])
    return [term for term in order if counts[term] >= 2][:EXPANSION_TERMS]


def fuse_hits(first: list[SearchHit], second: list[SearchHit], limit: int) -> list[SearchHit]:
    """Merges two rankings by reciprocal rank fusion; ties keep first-pass order."""
    limit = limit if limit > 0 else 10
    scores: dict[tuple[str, str], float] = {}
    hits: dict[tuple[str, str], SearchHit] = {}
    for ranking in (first, second):
        for rank, hit in enumerate(ranking, start=1):
            key = (hit.repository_id, hit.node.id if hit.node is not None else "")
            hits.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank)
    order = sorted(hits, key=lambda key: -scores[key])
    return [hits[key] for key in order[:limit]]
