"""An in-memory GraphStore for tests of the operations and the tools: a port
of the Go server's memstore. Search results are canned; ranking is the
Spanner store's job."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from forge_codegraph_mcp.graph.errors import InvalidRequest, NotFound
from forge_codegraph_mcp.graph.model import (
    IN,
    OUT,
    SEMANTIC_EDGE_KINDS,
    CrossEnd,
    CrossLink,
    CrossLinkQuery,
    Neighbor,
    NeighborPage,
    NeighborQuery,
    RepositoryState,
    Version,
    graph_id,
    prop,
)
from forge_codegraph_mcp.graph.store import (
    CHANGE_ADDED,
    CHANGE_RETIRED,
    CHANGE_UPDATED,
    ChangesPage,
    EdgeRef,
    Hub,
    RecordChange,
    Repository,
    SearchHit,
    SearchRequest,
)


@dataclass
class MemStore:
    """One repository's live graph, and others that cross-repository links
    lead to: a read naming one of ``others`` is answered by it."""

    repo: str
    others: dict[str, MemStore] = field(default_factory=dict)
    links: list[CrossLink] = field(default_factory=list)
    nodes: dict[str, Version] = field(default_factory=dict)
    edges: list[Version] = field(default_factory=list)
    sources: dict[str, bytes] = field(default_factory=dict)
    histories: dict[str, list[Version]] = field(default_factory=dict)
    # Returned by hybrid_search, cut to the request's limit.
    hits: list[SearchHit] = field(default_factory=list)
    # Every search request received.
    searches: list[SearchRequest] = field(default_factory=list)
    # Bounds one neighbors page; 0 means 100.
    neighbor_page: int = 0

    def add(self, repo: str) -> MemStore:
        other = MemStore(repo)
        self.others[repo] = other
        return other

    def _at(self, repo: str) -> MemStore | None:
        return self if repo == self.repo else self.others.get(repo)

    def _other(self, repo: str) -> MemStore | None:
        return None if repo == self.repo else self.others.get(repo)

    def add_node(self, node: dict[str, Any]) -> dict[str, Any]:
        lineage = (node.get("source") or {}).get("lineage", "")
        self.nodes[node["id"]] = Version(
            fact={"node": node}, gen_from=1, commit_from="c1", lineage=lineage
        )
        return node

    def add_version(self, version: Version) -> None:
        """An open version replaces the node's current one; a closed one is
        kept as history, to shape a generation's change set."""
        if version.gen_to == 0:
            self.nodes[version.id] = version
        else:
            self.histories.setdefault(version.id, []).append(version)

    def add_edge(self, kind: str, source: str, target: str, line: int = 0) -> dict[str, Any]:
        edge: dict[str, Any] = {
            "id": graph_id("edge", kind, source, target),
            "kind": kind,
            "source_id": source,
            "target_id": target,
        }
        if line:
            edge["source"] = {
                "lineage": "file:x",
                "content_sha256": "0" * 64,
                "span": {"start": {"byte_offset": 0, "line": line, "column": 1}, "end": {}},
            }
        self.edges.append(Version(fact={"edge": edge}, gen_from=1, commit_from="c1"))
        return edge

    def link(
        self, link_id: str, kind: str, from_repo: str, from_id: str, to_repo: str, to_id: str
    ) -> CrossLink:
        def end(repo: str, node_id: str) -> CrossEnd:
            held = self._at(repo)
            node = held.nodes[node_id].node if held and node_id in held.nodes else None
            return CrossEnd(
                repository_id=repo,
                node_id=node_id,
                qualified_name=(node or {}).get("qualified_name", ""),
                kind=(node or {}).get("kind", ""),
            )

        result = CrossLink(
            id=link_id,
            owner="team:test",
            kind=kind,
            source=end(from_repo, from_id),
            target=end(to_repo, to_id),
            provenance="manual",
        )
        self.links.append(result)
        return result

    def hit(self, node_id: str, score: float, exact: bool) -> SearchHit:
        version = self.nodes[node_id]
        return SearchHit(
            repository_id=self.repo,
            node=version,
            score=score,
            exact_match=exact,
            signature=prop(version.node, "signature"),
        )

    # --- GraphStore -------------------------------------------------------

    async def cross_links(self, query: CrossLinkQuery) -> list[CrossLink]:
        return [
            link
            for link in self.links
            if (query.direction != IN and query.matches(link, OUT))
            or (query.direction != OUT and query.matches(link, IN))
        ]

    async def hybrid_search(self, request: SearchRequest) -> list[SearchHit]:
        self.searches.append(request)
        if request.mode not in ("", "hybrid", "lexical", "semantic"):
            raise InvalidRequest(f"search mode {request.mode!r}")
        hits = list(self.hits)
        return hits[: request.limit] if request.limit > 0 else hits

    async def find_nodes(
        self,
        repo: str,
        name: str,
        qualified_name: str,
        kinds: list[str] | None,
        generation: int,
        limit: int,
    ) -> list[Version]:
        if (other := self._other(repo)) is not None:
            return await other.find_nodes(repo, name, qualified_name, kinds, generation, limit)
        if repo != self.repo or bool(name) == bool(qualified_name):
            raise InvalidRequest("repository, one of name or qualified name")
        out = []
        for version in self.nodes.values():
            node = version.node or {}
            if name and str(node.get("name", "")).lower() != name.lower():
                continue
            if qualified_name and node.get("qualified_name") != qualified_name:
                continue
            if kinds and node.get("kind") not in kinds:
                continue
            out.append(version)
        out.sort(key=lambda v: v.id)
        return out[:limit] if limit > 0 else out

    async def get_node(self, repo: str, node_id: str, generation: int) -> Version:
        if (other := self._other(repo)) is not None:
            return await other.get_node(repo, node_id, generation)
        if repo == self.repo and node_id in self.nodes:
            return self.nodes[node_id]
        raise NotFound(f"node {node_id}")

    async def neighbors(self, query: NeighborQuery) -> NeighborPage:
        if (other := self._other(query.repository_id)) is not None:
            return await other.neighbors(query)
        if query.repository_id != self.repo:
            return NeighborPage(generation=1)
        edges = []
        for version in self.edges:
            edge = version.edge or {}
            out = edge["source_id"] == query.node_id and query.direction != IN
            into = edge["target_id"] == query.node_id and query.direction != OUT
            if not (out or into):
                continue
            if query.edge_kinds and edge["kind"] not in query.edge_kinds:
                continue
            edges.append(version)
        edges.sort(key=lambda v: v.id)
        offset = 0
        if query.cursor:
            try:
                offset = int(query.cursor)
            except ValueError:
                raise InvalidRequest("cursor") from None
        limit = query.limit if query.limit > 0 else 100
        if self.neighbor_page > 0:
            limit = min(limit, self.neighbor_page)
        page = NeighborPage(generation=1)
        end = offset + limit
        if end < len(edges):
            page.next_cursor = str(end)
        for version in edges[offset:end]:
            edge = version.edge or {}
            far = edge["source_id"] if edge["target_id"] == query.node_id else edge["target_id"]
            page.neighbors.append(Neighbor(edge=version, node=self.nodes.get(far)))
        return page

    async def dependency_edges(
        self,
        repo: str,
        node_ids: list[str],
        direction: str,
        kinds: list[str] | tuple[str, ...] | None,
        generation: int,
        limit: int,
    ) -> tuple[list[EdgeRef], bool]:
        if (other := self._other(repo)) is not None:
            return await other.dependency_edges(repo, node_ids, direction, kinds, generation, limit)
        if repo != self.repo or not node_ids or limit < 1:
            raise InvalidRequest("dependency edge query")
        wanted = set(node_ids)
        out = []
        for version in self.edges:
            edge = version.edge or {}
            if kinds and edge["kind"] not in kinds:
                continue
            if (direction != IN and edge["source_id"] in wanted) or (
                direction != OUT and edge["target_id"] in wanted
            ):
                out.append(EdgeRef(edge["id"], edge["kind"], edge["source_id"], edge["target_id"]))
        out.sort(key=lambda ref: ref.id)
        if len(out) > limit:
            return out[:limit], True
        return out, False

    async def get_nodes(self, repo: str, ids: list[str], generation: int) -> dict[str, Version]:
        if (other := self._other(repo)) is not None:
            return await other.get_nodes(repo, ids, generation)
        if repo != self.repo:
            return {}
        return {node_id: self.nodes[node_id] for node_id in ids if node_id in self.nodes}

    async def hubs(
        self, repo: str, generation: int, kinds: list[str] | None, limit: int
    ) -> list[Hub]:
        if repo != self.repo or limit < 1:
            raise InvalidRequest("hubs query")
        counted = kinds or list(SEMANTIC_EDGE_KINDS)
        by_target: dict[str, Hub] = {}
        for version in self.edges:
            edge = version.edge or {}
            if edge["kind"] not in counted:
                continue
            hub = by_target.setdefault(edge["target_id"], Hub(edge["target_id"], 0, {}))
            hub.in_degree += 1
            hub.by_kind[edge["kind"]] = hub.by_kind.get(edge["kind"], 0) + 1
        out = sorted(by_target.values(), key=lambda hub: (-hub.in_degree, hub.node_id))
        return out[:limit]

    async def changes(
        self, repo: str, generation: int, kind: str, limit: int, cursor: str
    ) -> ChangesPage:
        if repo != self.repo or kind != "node":
            raise InvalidRequest("changes query")
        generation = generation or 1
        opened = {i: v for i, v in self.nodes.items() if v.gen_from == generation}
        closed: dict[str, Version] = {}
        for node_id, versions in self.histories.items():
            for version in versions:
                if version.gen_from == generation and version.gen_to != 0:
                    opened[node_id] = version
                if version.gen_to == generation:
                    closed[node_id] = version
        page = ChangesPage(generation=generation)
        for node_id in sorted(set(opened) | set(closed)):
            if node_id <= cursor:
                continue
            if node_id in opened and node_id in closed:
                page.changes.append(
                    RecordChange(
                        CHANGE_UPDATED, node_id, version=opened[node_id], before=closed[node_id]
                    )
                )
                page.updated += 1
            elif node_id in opened:
                page.changes.append(RecordChange(CHANGE_ADDED, node_id, version=opened[node_id]))
                page.added += 1
            else:
                page.changes.append(RecordChange(CHANGE_RETIRED, node_id, version=closed[node_id]))
                page.retired += 1
            page.commit = page.commit or f"c{generation}"
        if 0 < limit < len(page.changes):
            page.next_cursor = page.changes[limit - 1].id
            page.changes = page.changes[:limit]
        return page

    async def history(self, repo: str, kind: str, record_id: str) -> list[Version]:
        if repo == self.repo and record_id in self.histories:
            return self.histories[record_id]
        if kind == "node" and record_id in self.nodes:
            return [self.nodes[record_id]]
        raise NotFound(f"{kind} {record_id}")

    async def get_source(self, repo: str, sha256: str) -> bytes:
        if (other := self._other(repo)) is not None:
            return await other.get_source(repo, sha256)
        if repo == self.repo and sha256 in self.sources:
            return self.sources[sha256]
        raise NotFound(f"source {sha256}")

    async def state(self, repo: str) -> RepositoryState:
        if (other := self._other(repo)) is not None:
            return await other.state(repo)
        if repo != self.repo:
            raise NotFound(f"repository {repo}")
        return RepositoryState(repository_id=repo, live_generation=1, live_commit="c1")

    async def repositories(
        self, limit: int, ids: Sequence[str] | None = None
    ) -> tuple[list[Repository], bool]:
        repos = [r for r in (self.repo, *self.others) if ids is None or r in ids]
        out = [Repository(id=repo, live_generation=1, live_commit="c1") for repo in sorted(repos)]
        return out[:limit], len(out) > limit
