"""The read surface the graph operations compose, and the one write.

:class:`~forge_codegraph_query.spanner.SpannerGraphStore` reads the
worker's Spanner database (:class:`~forge_codegraph_query.testing.MemStore`
holds a graph in memory, for tests). Every read resolves generation 0 to the
repository's live generation and never shows a version above it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from forge_codegraph_query.model import (
    CrossLink,
    CrossLinkQuery,
    NeighborPage,
    NeighborQuery,
    RepositoryState,
    Version,
    jfield,
)

# Search modes: every branch whose inputs are present, the search index only,
# or stored embeddings only.
SEARCH_HYBRID = "hybrid"
SEARCH_LEXICAL = "lexical"
SEARCH_SEMANTIC = "semantic"
SEARCH_MODES = (SEARCH_HYBRID, SEARCH_LEXICAL, SEARCH_SEMANTIC)

# What a generation did to a record.
CHANGE_ADDED = "added"
CHANGE_UPDATED = "updated"
CHANGE_RETIRED = "retired"


@dataclass(slots=True)
class SearchRequest:
    repository_ids: list[str]
    text: str
    mode: str = SEARCH_HYBRID
    # The embedding model of ``vector``; empty disables the vector branch.
    model: str = ""
    vector: list[float] | None = None
    kinds: list[str] | None = None
    # Keep nodes whose file_path starts with this.
    path_prefix: str = ""
    # The caller's current file; hits in nearby directories rank higher.
    near_path: str = ""
    # Spanner query enhancement (spelling, synonyms, plurals); managed only.
    enhance_query: bool = False
    limit: int = 10


@dataclass(slots=True)
class SearchHit:
    repository_id: str = jfield("")
    node: Version | None = jfield()
    # The fused and reranked score the hits are ordered by.
    score: float = jfield(0.0)
    # The search index's SCORE, 0 when the lexical branch didn't find it.
    lexical: float = jfield(0.0)
    # Cosine similarity to the question, 0 when the vector branch didn't.
    vector: float = jfield(0.0)
    # The name or qualified name is the query, or a symbol it names.
    exact_match: bool = jfield(False)
    # The name is a plain word of the question.
    name_match: bool = jfield(False)
    # Open incoming and outgoing semantic edges.
    callers: int = jfield(0)
    callees: int = jfield(0)
    signature: str = jfield("", omitempty=True)
    snippet: str = jfield("", omitempty=True)
    # The query terms present in the node's document.
    matched: list[str] = jfield(factory=list, omitempty=True)


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """An edge as the edge indexes hold it: identity, kind and endpoints."""

    id: str
    kind: str
    source_id: str
    target_id: str


@dataclass(slots=True)
class Hub:
    node_id: str
    in_degree: int
    by_kind: dict[str, int]


@dataclass(slots=True)
class RecordChange:
    """One record a generation touched: the version it opened, or for a
    retired record the version it closed; ``before`` for an update."""

    op: str = jfield("")
    id: str = jfield("")
    version: Version | None = jfield()
    before: Version | None = jfield(None, omitempty=True)


@dataclass(slots=True)
class ChangesPage:
    generation: int = jfield(0)
    commit: str = jfield("", omitempty=True)
    added: int = jfield(0)
    updated: int = jfield(0)
    retired: int = jfield(0)
    changes: list[RecordChange] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


@dataclass(slots=True)
class Repository:
    """A repository with a published graph, as list_repositories names it."""

    # What every other tool takes as repository.
    id: str = jfield("")
    # owner/name at the provider.
    full_name: str = jfield("", omitempty=True)
    url: str = jfield("", omitempty=True)
    branch: str = jfield("", omitempty=True)
    live_generation: int = jfield(0)
    live_commit: str = jfield("", omitempty=True)


@dataclass(slots=True)
class NodePage:
    """A page of the nodes open at a generation, in id order."""

    generation: int = jfield(0)
    nodes: list[Version] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


@dataclass(slots=True)
class EmbeddingStats:
    """The stored vectors of one embedding model and dimension: ``current``
    are the searchable open nodes whose vector matches their document,
    ``stored`` every vector of the repository for the model, stale ones
    included. An empty model means embeddings are off and nothing was
    counted."""

    model: str = jfield("")
    dimensions: int = jfield(0)
    current: int = jfield(0)
    stored: int = jfield(0)


@dataclass(slots=True)
class RepositoryStats:
    """Totals of a repository's live graph, for an ingestion audit: open
    records by node and edge kind, the nodes carrying a search document, and
    their embeddings. Before the first publication every count is zero."""

    repository_id: str = jfield("")
    branch: str = jfield("")
    generation: int = jfield(0)
    commit_sha: str = jfield("")
    run_id: str = jfield("")
    nodes: int = jfield(0)
    edges: int = jfield(0)
    node_kinds: dict[str, int] = jfield(factory=dict)
    edge_kinds: dict[str, int] = jfield(factory=dict)
    searchable: int = jfield(0)
    embeddings: EmbeddingStats = jfield(factory=EmbeddingStats)


@dataclass(slots=True)
class RunPage:
    """A repository's ingestion runs, newest deployment first, each the
    run record the worker keeps (phase, attempts, errors, metrics, index)."""

    runs: list[dict[str, Any]] = jfield(factory=list)
    next_cursor: str = jfield("", omitempty=True)


class GraphStore(Protocol):
    async def hybrid_search(self, request: SearchRequest) -> list[SearchHit]: ...

    async def find_nodes(
        self,
        repo: str,
        name: str,
        qualified_name: str,
        kinds: list[str] | None,
        generation: int,
        limit: int,
    ) -> list[Version]: ...

    async def get_node(self, repo: str, node_id: str, generation: int) -> Version: ...

    async def list_nodes(
        self, repo: str, generation: int, kind: str, limit: int, cursor: str
    ) -> NodePage:
        """The nodes open at the generation, of one kind or any, by id."""
        ...

    async def generation_commit(self, repo: str, generation: int) -> str:
        """The commit a published generation was built from."""
        ...

    async def neighbors(self, query: NeighborQuery) -> NeighborPage: ...

    async def dependency_edges(
        self,
        repo: str,
        node_ids: list[str],
        direction: str,
        kinds: list[str] | tuple[str, ...] | None,
        generation: int,
        limit: int,
    ) -> tuple[list[EdgeRef], bool]:
        """The open edges touching any of the nodes, from the edge indexes,
        without payloads; and whether ``limit`` cut any."""
        ...

    async def get_nodes(self, repo: str, ids: list[str], generation: int) -> dict[str, Version]:
        """The versions open at the generation, or a retired record's last
        version; ids with no version are absent."""
        ...

    async def hubs(
        self, repo: str, generation: int, kinds: list[str] | None, limit: int
    ) -> list[Hub]: ...

    async def changes(
        self, repo: str, generation: int, kind: str, limit: int, cursor: str
    ) -> ChangesPage: ...

    async def history(self, repo: str, kind: str, record_id: str) -> list[Version]: ...

    async def get_source(self, repo: str, sha256: str) -> bytes: ...

    async def state(self, repo: str) -> RepositoryState: ...

    async def cross_links(self, query: CrossLinkQuery) -> list[CrossLink]:
        """The cross-repository links touching a repository's nodes; a
        database without any has none."""
        ...

    async def repositories(
        self, limit: int, ids: Sequence[str] | None = None
    ) -> tuple[list[Repository], bool]:
        """The repositories with a published graph, by id, of ``ids`` when
        given; and whether there are more than ``limit``."""
        ...

    async def stats(self, repo: str, model: str, dimensions: int) -> RepositoryStats:
        """The live graph's totals, and its embeddings for (model,
        dimensions) unless model is empty, from one consistent snapshot."""
        ...

    async def runs(self, repo: str, limit: int, cursor: str) -> RunPage:
        """The repository's ingestion runs, newest deployment first."""
        ...


class CrossLinkWriter(Protocol):
    """Writes the one thing outside the worker's graphs: the cross-repository
    links people draw, kept beside the graphs and never in a generation."""

    async def replace_cross_links(self, owner: str, links: Sequence[CrossLink]) -> None:
        """Makes ``links`` the owner's whole set, in one transaction: links it
        had and no longer lists are removed; an empty set removes them all."""
        ...
