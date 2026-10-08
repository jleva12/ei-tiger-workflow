"""The code graph's tools, every one read-only.

The operations codegraph-api serves over HTTP, described for a model that
explores code by asking questions in plain language and then reading exact
source. Each tool's docstring is its description, as the Go server's was.

Answers are JSON objects. Failures a caller can act on (a bad argument, a node
that isn't there, a stale cursor) come back as tool errors the model reads;
anything else is logged and masked.

Each caller reads only its organization's repositories (access.py): every
repository asked for is checked, and links into other repositories are
followed only into those it reads, and only when its organization drew them
(another organization sharing a repository's graph may have drawn others).
repository_connections answers how its repositories connect, from its system
maps.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from forge_codegraph_mcp.access import RepositoryScope, current_scope
from forge_codegraph_mcp.core.settings import Settings
from forge_codegraph_mcp.graph import query as ops
from forge_codegraph_mcp.graph.embedding import Embedder
from forge_codegraph_mcp.graph.errors import GraphError, InvalidRequest
from forge_codegraph_mcp.graph.model import (
    BOTH,
    CROSS_KINDS,
    EDGE,
    IN,
    NODE,
    OUT,
    SEMANTIC_EDGE_KINDS,
    CrossLink,
    CrossLinkQuery,
    NeighborQuery,
    to_json,
)
from forge_codegraph_mcp.graph.query.crossrepo import CrossHop
from forge_codegraph_mcp.graph.store import GraphStore
from forge_codegraph_mcp.tools.base import Toolset, mcp_tool

# The most repositories list_repositories names, and the most bytes read_file returns.
MAX_LISTED_REPOSITORIES = 500
MAX_FILE_BYTES = 256 << 10
DEFAULT_NEIGHBORS = 100

type Answer = dict[str, Any]

Repository = Annotated[str, Field(description="repository id")]
NodeId = Annotated[str, Field(description="node id from an earlier result")]
Generation = Annotated[int, Field(ge=0, description="graph generation to read; zero means live")]
Full = Annotated[
    bool,
    Field(description="return full records with spans, hashes and properties instead of briefs"),
]


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, idempotent_hint=True)


def _answers[**P](fn: Callable[P, Awaitable[Answer]]) -> Callable[P, Awaitable[Answer]]:
    """Turns the graph's expected failures into tool errors the model reads,
    logged without a traceback."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Answer:
        try:
            return await fn(*args, **kwargs)
        except GraphError as exc:
            raise ToolError(str(exc), log_level=logging.WARNING) from exc

    return wrapper


def split_kinds(kinds: list[str] | None) -> list[str]:
    """Kinds as listed, or comma-separated within an entry."""
    return [part.strip() for kind in kinds or () for part in kind.split(",") if part.strip()]


class _OwnedLinks:
    """A store whose cross-repository links are only those one organization
    drew; everything else is the store's."""

    def __init__(self, store: GraphStore, owns: Callable[[str], bool]) -> None:
        self._store = store
        self._owns = owns

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def cross_links(self, query: CrossLinkQuery) -> list[CrossLink]:
        return [link for link in await self._store.cross_links(query) if self._owns(link.owner)]


class CodeGraphTools(Toolset):
    """Explore, search and walk the code graph, and read exact source."""

    def __init__(
        self,
        settings: Settings,
        store: GraphStore,
        embedder: Embedder | None,
        scope: Callable[[], RepositoryScope] = current_scope,
    ) -> None:
        """
        :param scope: The repositories the request being served reads: by
            default its credential's, as the admin API answered.
        """
        super().__init__(settings)
        self.store = store
        self.embedder = embedder
        self.scope = scope

    @mcp_tool(annotations=_read_only("List repositories"))
    @_answers
    async def list_repositories(self) -> Answer:
        """The repositories you can read, your organization's: the id every other tool takes as repository, owner/name, the branch ingested and its live generation and commit."""  # noqa: E501
        readable = sorted(self.scope().repository_ids)
        repositories, more = await self.store.repositories(MAX_LISTED_REPOSITORIES, ids=readable)
        out: Answer = {"repositories": to_json(repositories)}
        if more:
            out["truncated"] = True
        return out

    def _linked(self) -> GraphStore:
        """The store, with only the caller's organization's cross-repository links."""
        scope = self.scope()
        if scope.link_owners is None:
            return self.store
        return cast(GraphStore, _OwnedLinks(self.store, scope.owns))

    @mcp_tool(annotations=_read_only("Repository connections"))
    @_answers
    async def repository_connections(
        self,
        repositories: Annotated[
            list[str] | None,
            Field(
                description="repository ids: only the connections touching any of them; "
                "all of your organization's when absent"
            ),
        ] = None,
    ) -> Answer:
        """How repositories connect, as people drew them on your organization's system design maps: what calls what, what sends messages to what. Each connection has its knowledge_base (the system it's drawn in), its source and target repository (repository_id and owner/name), its kind: calls (calls the target's API: HTTP, gRPC, GraphQL), events (publishes events or messages the target consumes), depends_on (uses it as a library), shares_data (the same database or storage) or connects_to (unspecified), a description (such as POST /v1/orders or a topic), and its code_links: where in the code it happens, a node in each repository (the client method and the handler it reaches, the publisher and the listener) with node_id, kind, qualified_name and path. Follow a code link's node ids with callers, callees, impact and read_source in its repository. Use this first for how projects, services or repositories interact. A connection without code links says only that the two connect; search both repositories for where."""  # noqa: E501
        scope = self.scope()
        wanted = set(repositories or ())
        scope.check(*wanted)
        found = [
            connection
            for connection in scope.connections
            if not wanted
            or connection["source"]["repository_id"] in wanted
            or connection["target"]["repository_id"] in wanted
        ]
        return {"connections": found}

    @mcp_tool(annotations=_read_only("Explore code"))
    @_answers
    async def explore_code(
        self,
        question: Annotated[
            str,
            Field(description="the question in plain language; may name symbols and file paths"),
        ],
        repositories: Annotated[list[str], Field(description="repository ids to explore")],
        limit: Annotated[
            int, Field(description="seed declarations to return, 1-25 (default 8)")
        ] = 0,
        expand: Annotated[
            int, Field(description="seeds to expand into their neighborhood, 1-10 (default 5)")
        ] = 0,
        neighbors: Annotated[
            int, Field(description="neighbors per expanded seed, 1-100 (default 20)")
        ] = 0,
        kinds: Annotated[
            list[str] | None,
            Field(
                description="node kinds to keep as seeds, such as class, interface, method, field"
            ),
        ] = None,
        path_prefix: Annotated[
            str, Field(description="keep only seeds whose file path starts with this")
        ] = "",
        near_path: Annotated[
            str, Field(description="the file being worked on; nearby seeds rank higher")
        ] = "",
        mode: Annotated[str, Field(description="hybrid (default), lexical or semantic")] = "",
        no_expansion: Annotated[
            bool, Field(description="disable query expansion with the repository's vocabulary")
        ] = False,
        full: Full = False,
    ) -> Answer:
        """Answer a question about the code with evidence: ranked seed declarations for the question, one hop of callers, callees, type uses and inheritance around the strongest seeds, edges among seeds, and the files involved. Symbols named in the question (camelCase, dotted or with parentheses) are matched exactly; paths mentioned rank their files higher. When no exact match exists the question is expanded with the repository's own vocabulary (expanded_terms). Compact by default; full: true returns records. Use this first for any natural-language question."""  # noqa: E501
        self.scope().check(*repositories)
        result = await ops.explore(
            self.store,
            self.embedder,
            repository_ids=repositories,
            question=question,
            limit=limit,
            expand=expand,
            neighbors=neighbors,
            kinds=split_kinds(kinds),
            path_prefix=path_prefix,
            near_path=near_path,
            mode=mode,
            enhance_query=self.settings.search.enhance_query,
            expand_query=not no_expansion,
        )
        return to_json(result if full else ops.compact_explore(result))

    @mcp_tool(annotations=_read_only("Search code"))
    @_answers
    async def search_code(
        self,
        query: Annotated[
            str,
            Field(description="an identifier, a partial identifier or a natural-language question"),
        ],
        repositories: Annotated[list[str], Field(description="repository ids to search")],
        limit: Annotated[int, Field(description="maximum hits, 1-50 (default 10)")] = 0,
        kinds: Annotated[
            list[str] | None,
            Field(description="node kinds to keep, such as class, interface, method, field"),
        ] = None,
        path_prefix: Annotated[
            str, Field(description="keep only nodes whose file path starts with this")
        ] = "",
        near_path: Annotated[
            str, Field(description="the file being worked on; nearby hits rank higher")
        ] = "",
        mode: Annotated[str, Field(description="hybrid (default), lexical or semantic")] = "",
        no_expansion: Annotated[
            bool, Field(description="disable query expansion with the repository's vocabulary")
        ] = False,
        full: Full = False,
    ) -> Answer:
        """Ranked search for declarations by identifier, partial identifier or natural language. Each hit has a snippet around the matched terms, the matched terms, an exact-match flag and caller and callee counts. Compact by default; full: true returns records."""  # noqa: E501
        self.scope().check(*repositories)
        result = await ops.search(
            self.store,
            self.embedder,
            repository_ids=repositories,
            text=query,
            mode=mode,
            limit=limit,
            kinds=split_kinds(kinds),
            path_prefix=path_prefix,
            near_path=near_path,
            enhance_query=self.settings.search.enhance_query,
            expand=not no_expansion,
        )
        if full:
            return to_json(result)
        return {
            "hits": to_json(ops.compact_hits(result.hits)),
            "query": to_json(result.query),
            "mode": result.mode,
            "semantic": result.semantic,
            "expanded_terms": result.expanded_terms,
        }

    @mcp_tool(annotations=_read_only("Find symbol"))
    @_answers
    async def find_symbol(
        self,
        repository: Repository,
        name: Annotated[
            str, Field(description="simple name, such as placeOrder or OrderService")
        ] = "",
        qualified_name: Annotated[
            str,
            Field(description="qualified name, such as shop.OrderService or placeOrder(shop.Cart)"),
        ] = "",
        kinds: Annotated[list[str] | None, Field(description="node kinds to keep")] = None,
        limit: Annotated[int, Field(description="maximum nodes, 1-200 (default 20)")] = 0,
        generation: Generation = 0,
    ) -> Answer:
        """Resolve a simple name or a qualified name to the declarations that carry it. Exactly one of name and qualified_name is required."""  # noqa: E501
        self.scope().check(repository)
        limit = limit or 20
        if not 1 <= limit <= 200:
            raise InvalidRequest("limit 1-200")
        nodes = await self.store.find_nodes(
            repository, name.strip(), qualified_name.strip(), split_kinds(kinds), generation, limit
        )
        return {"nodes": to_json(nodes)}

    @mcp_tool(annotations=_read_only("Get node"))
    @_answers
    async def get_node(
        self, repository: Repository, id: NodeId, generation: Generation = 0
    ) -> Answer:
        """Fetch one node by id with all its properties, its source anchor (file lineage, content hash and byte span) and its version bounds."""  # noqa: E501
        self.scope().check(repository)
        return to_json(await self.store.get_node(repository, id, generation))

    @mcp_tool(annotations=_read_only("Neighbors"))
    @_answers
    async def neighbors(
        self,
        repository: Repository,
        id: NodeId,
        direction: Annotated[str, Field(description="in, out or both (default both)")] = "",
        kinds: Annotated[
            list[str] | None, Field(description="edge kinds to keep; empty means all")
        ] = None,
        limit: Annotated[int, Field(description="maximum edges per page, 1-500 (default 100)")] = 0,
        cursor: Annotated[str, Field(description="next_cursor from the previous page")] = "",
        generation: Generation = 0,
        full: Full = False,
    ) -> Answer:
        """List the edges touching a node and the nodes at their far ends, filtered by direction and edge kinds (contains, calls, uses_type, references, inherits, overrides, implements, framework_binding). Paginated by cursor. Compact by default: far node in brief, edge kind, direction and the line of the occurrence; full: true returns records. The first page also lists the node's cross-repository links (across_repositories) in the direction, unless kinds leaves out every cross-repository kind (calls_api, sends_event, depends_on, shares_data, connects_to)."""  # noqa: E501
        return await self._neighbor_page(
            repository, id, direction or BOTH, split_kinds(kinds), limit, cursor, generation, full
        )

    @mcp_tool(annotations=_read_only("Callers"))
    @_answers
    async def callers(
        self,
        repository: Repository,
        id: NodeId,
        limit: Annotated[int, Field(description="maximum edges per page, 1-500 (default 100)")] = 0,
        cursor: Annotated[str, Field(description="next_cursor from the previous page")] = "",
        generation: Generation = 0,
        full: Full = False,
    ) -> Answer:
        """Nodes that depend on this one: incoming calls, references, type uses, inheritance, overrides and implementations. Paginated by cursor. The first page also lists code in other repositories linked to this node (across_repositories), such as the clients that call the API it serves or the publishers of the events it consumes."""  # noqa: E501
        return await self._neighbor_page(
            repository, id, IN, list(SEMANTIC_EDGE_KINDS), limit, cursor, generation, full
        )

    @mcp_tool(annotations=_read_only("Callees"))
    @_answers
    async def callees(
        self,
        repository: Repository,
        id: NodeId,
        limit: Annotated[int, Field(description="maximum edges per page, 1-500 (default 100)")] = 0,
        cursor: Annotated[str, Field(description="next_cursor from the previous page")] = "",
        generation: Generation = 0,
        full: Full = False,
    ) -> Answer:
        """Nodes this one depends on: outgoing calls, references, type uses, inheritance, overrides and implementations. Paginated by cursor. The first page also lists code in other repositories this node links to (across_repositories), such as the handler serving an API it calls or the listener consuming an event it sends."""  # noqa: E501
        return await self._neighbor_page(
            repository, id, OUT, list(SEMANTIC_EDGE_KINDS), limit, cursor, generation, full
        )

    async def _neighbor_page(
        self,
        repository: str,
        node_id: str,
        direction: str,
        kinds: list[str],
        limit: int,
        cursor: str,
        generation: int,
        full: bool,
    ) -> Answer:
        self.scope().check(repository)
        if not 0 <= limit <= 500:
            raise InvalidRequest("limit 1-500")
        page = await self.store.neighbors(
            NeighborQuery(
                repository_id=repository,
                node_id=node_id,
                direction=direction,
                edge_kinds=tuple(kinds),
                generation=generation,
                limit=limit or DEFAULT_NEIGHBORS,
                cursor=cursor,
            )
        )
        out = to_json(page if full else ops.compact_neighbors(page, node_id))
        # Links to other repositories come once, with the first page.
        if not cursor and _cross_kinds_wanted(kinds):
            hops = await self._cross_hops(repository, node_id, direction, generation, kinds)
            if hops:
                out["across_repositories"] = to_json(hops if full else ops.compact_cross_hops(hops))
        return out

    async def _cross_hops(
        self, repository: str, node_id: str, direction: str, generation: int, kinds: list[str]
    ) -> list[CrossHop]:
        """A node's links, followed; only the kinds asked for when the filter
        names cross-repository kinds."""
        version = await self.store.get_node(repository, node_id, generation)
        if version.node is None:
            return []
        readable = self.scope().readable
        hops = await ops.cross_hops(self._linked(), repository, version.node, direction, readable)
        named = {kind for kind in kinds if kind in CROSS_KINDS}
        return [hop for hop in hops if hop.link.kind in named] if named else hops

    @mcp_tool(annotations=_read_only("Cross-repository links"))
    @_answers
    async def cross_repository_links(
        self,
        repository: Repository,
        id: NodeId,
        direction: Annotated[
            str,
            Field(description="out: what this node links to; in: what links to it; both (default)"),
        ] = "",
        full: Annotated[
            bool, Field(description="return the links and far node records instead of briefs")
        ] = False,
    ) -> Answer:
        """A node's links to code in other repositories, as people recorded them: the handler serving an API it calls, the listener consuming an event it sends (out), or the clients and publishers that reach it (in). Each has the link's kind, label (such as POST /v1/orders or a topic) and the far node with its repository_id, read at that repository's live generation; stale when the far node is gone. Only repositories you can read."""  # noqa: E501
        self.scope().check(repository)
        direction = direction or BOTH
        if direction not in (BOTH, IN, OUT):
            raise InvalidRequest("direction in, out or both")
        hops = await self._cross_hops(repository, id, direction, 0, [])
        return {"links": to_json(hops if full else ops.compact_cross_hops(hops))}

    @mcp_tool(annotations=_read_only("Impact"))
    @_answers
    async def impact(
        self,
        repository: Repository,
        id: Annotated[str, Field(description="node id of the declaration that would change")],
        change: Annotated[
            str,
            Field(description="kind of change: any (default), body, signature, remove or contract"),
        ] = "",
        depth: Annotated[int, Field(description="hops to walk backwards, 1-6 (default 2)")] = 0,
        limit: Annotated[
            int, Field(description="maximum impacted nodes, 1-2000 (default 200)")
        ] = 0,
        generation: Generation = 0,
        include_locals: Annotated[
            bool,
            Field(
                description="also list local variables, parameters and pattern variables that use "
                "the declaration (folded away by default)"
            ),
        ] = False,
        full: Annotated[
            bool, Field(description="return full node records instead of briefs")
        ] = False,
    ) -> Answer:
        """Everything that would be affected by changing a declaration: dependants walked backwards up to depth hops. change selects what matters: body (callers, including through dispatch), signature (every direct user), remove (everything touching it), contract (implementors and their callers) or any (default). Each impacted node appears once with its module, source root and the number of dependency edges that reach it, plus an assessment: counts per module and root, the entry points nothing else depends on, the test classes to run and a Maven command per module; full: true returns records. Where code in another repository you can read is linked to an impacted node (it calls the API or consumes the events), across lists that code and its own dependants there, within the same depth."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.impact(
            self._linked(),
            repository_id=repository,
            node_id=id,
            generation=generation,
            change=change,
            depth=depth,
            limit=limit,
            include_locals=include_locals,
            across=self.scope().readable,
        )
        return to_json(result if full else ops.compact_impact(result))

    @mcp_tool(annotations=_read_only("Changes"))
    @_answers
    async def changes(
        self,
        repository: Repository,
        generation: Annotated[
            int, Field(ge=0, description="the generation to describe; zero means live")
        ] = 0,
        kinds: Annotated[
            list[str] | None, Field(description="node kinds to keep; default the declaration kinds")
        ] = None,
        limit: Annotated[
            int, Field(description="changed declarations to return, 1-2000 (default 200)")
        ] = 0,
        cursor: Annotated[str, Field(description="next_cursor from the previous page")] = "",
    ) -> Answer:
        """What a published generation changed: the declarations it added, updated and retired, with the generation's totals and the commit. Generation zero means live. Paginated by cursor."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.changes(
            self.store,
            repository_id=repository,
            generation=generation,
            kinds=split_kinds(kinds),
            limit=limit,
            cursor=cursor,
        )
        return to_json(result)

    @mcp_tool(annotations=_read_only("Change impact"))
    @_answers
    async def change_impact(
        self,
        repository: Repository,
        generation: Annotated[
            int,
            Field(ge=0, description="the generation whose changes to assess; zero means live"),
        ] = 0,
        change: Annotated[
            str,
            Field(
                description="kind of change to assume for every root: any (default), body, "
                "signature, remove or contract"
            ),
        ] = "",
        depth: Annotated[int, Field(description="hops to walk backwards, 1-6 (default 2)")] = 0,
        limit: Annotated[
            int, Field(description="maximum impacted nodes, 1-2000 (default 200)")
        ] = 0,
        max_roots: Annotated[
            int,
            Field(description="changed declarations to use as roots, 1-2000 (default 500)"),
        ] = 0,
        include_locals: Annotated[
            bool, Field(description="also list local declarations that use the changed ones")
        ] = False,
        full: Annotated[
            bool, Field(description="return full node records instead of briefs")
        ] = False,
    ) -> Answer:
        """The impact of a whole generation: every declaration it added or updated is a root walked at that generation, every retired one a root walked at the generation before, merged into one assessment with modules, entry points and tests. Use it to review a commit after the fact."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.change_impact(
            self.store,
            repository_id=repository,
            generation=generation,
            change=change,
            depth=depth,
            limit=limit,
            max_roots=max_roots,
            include_locals=include_locals,
        )
        return to_json(result if full else ops.compact_change_impact(result))

    @mcp_tool(annotations=_read_only("Path"))
    @_answers
    async def path(
        self,
        repository: Repository,
        from_id: Annotated[str, Field(alias="from", description="node id the path starts from")],
        to: Annotated[str, Field(description="node id the path must reach")],
        max_hops: Annotated[
            int, Field(description="longest path to consider, 1-8 (default 4)")
        ] = 0,
        direction: Annotated[
            str,
            Field(
                description="out (default): from depends on ... depends on to; both: ignore "
                "edge direction"
            ),
        ] = "",
        kinds: Annotated[
            list[str] | None,
            Field(
                description="edge kinds to follow; default calls, references, uses_type, "
                "inherits, overrides, implements, framework_binding"
            ),
        ] = None,
        generation: Generation = 0,
    ) -> Answer:
        """The shortest chain of proven edges from one declaration to another: by default a dependency path (from depends on ... depends on to) over calls, references, type uses, inheritance, overrides and implementations; direction both ignores edge direction. Bidirectional search bounded by max_hops."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.path(
            self.store,
            repository_id=repository,
            from_id=from_id,
            to_id=to,
            generation=generation,
            max_hops=max_hops,
            kinds=split_kinds(kinds),
            direction=direction,
        )
        return to_json(result)

    @mcp_tool(annotations=_read_only("Hubs"))
    @_answers
    async def hubs(
        self,
        repository: Repository,
        limit: Annotated[int, Field(description="hubs to return, 1-200 (default 20)")] = 0,
        edge_kinds: Annotated[
            list[str] | None,
            Field(description="incoming edge kinds to count; default the semantic kinds"),
        ] = None,
        node_kinds: Annotated[
            list[str] | None,
            Field(description="keep only nodes of these kinds, such as class, interface, method"),
        ] = None,
        include_external: Annotated[
            bool,
            Field(
                description="also rank symbols the repository only refers to (JDK and library "
                "types, intrinsics), left out by default"
            ),
        ] = False,
        generation: Generation = 0,
    ) -> Answer:
        """The most depended-on declarations of a repository: nodes ranked by incoming semantic edges with a per-kind breakdown, optionally only nodes of given kinds. Scans every open edge once."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.hubs(
            self.store,
            repository_id=repository,
            generation=generation,
            edge_kinds=split_kinds(edge_kinds),
            node_kinds=split_kinds(node_kinds),
            limit=limit,
            include_external=include_external,
        )
        return to_json(result)

    @mcp_tool(annotations=_read_only("History"))
    @_answers
    async def history(
        self,
        repository: Repository,
        id: Annotated[str, Field(description="node or edge id")],
        kind: Annotated[str, Field(description="node (default) or edge")] = "",
    ) -> Answer:
        """Every stored version of a node or edge, oldest first, with the generations and commits that introduced and replaced it."""  # noqa: E501
        self.scope().check(repository)
        if kind not in ("", NODE, EDGE):
            raise InvalidRequest("kind must be node or edge")
        versions = await self.store.history(repository, kind or NODE, id)
        return {"versions": to_json(versions)}

    @mcp_tool(annotations=_read_only("Read source"))
    @_answers
    async def read_source(
        self,
        repository: Repository,
        id: NodeId,
        context_lines: Annotated[
            int, Field(description="whole lines to include before and after the node, 0-200")
        ] = 0,
        generation: Generation = 0,
    ) -> Answer:
        """The exact source of a node as it was ingested, cut from the retained file by the node's span, optionally with whole lines of context before and after."""  # noqa: E501
        self.scope().check(repository)
        result = await ops.node_source(
            self.store,
            repository_id=repository,
            node_id=id,
            generation=generation,
            context_lines=context_lines,
        )
        return to_json(result)

    @mcp_tool(annotations=_read_only("Read file"))
    @_answers
    async def read_file(
        self,
        repository: Repository,
        sha256: Annotated[
            str, Field(description="content hash of the file, from a node's content_sha256")
        ],
        start: Annotated[int, Field(description="first byte offset to return (default 0)")] = 0,
        end: Annotated[
            int,
            Field(
                description="byte offset to stop at (default: end of file); at most 262144 bytes "
                "are returned"
            ),
        ] = 0,
    ) -> Answer:
        """Retained file bytes by content hash (from a node's content_sha256), optionally a byte range. Files are immutable per hash."""  # noqa: E501
        self.scope().check(repository)
        data = await self.store.get_source(repository, sha256)
        end = end or len(data)
        if start < 0 or end < start or end > len(data):
            raise InvalidRequest(f"byte range [{start},{end}) outside a {len(data)}-byte file")
        truncated = end - start > MAX_FILE_BYTES
        if truncated:
            end = start + MAX_FILE_BYTES
        return {
            "sha256": sha256,
            "size": len(data),
            "start": start,
            "end": end,
            "truncated": truncated,
            "text": data[start:end].decode(errors="replace"),
        }

    @mcp_tool(annotations=_read_only("Repository state"))
    @_answers
    async def repository_state(self, repository: Repository) -> Answer:
        """The live generation, commit and run of a repository: what every other tool reads unless a generation is given."""  # noqa: E501
        self.scope().check(repository)
        return to_json(await self.store.state(repository))


def _cross_kinds_wanted(kinds: list[str]) -> bool:
    """Whether an edge kind filter lets cross-repository links through: none,
    the semantic kinds callers and callees ask for, or one that names a
    cross-repository kind."""
    if not kinds or kinds == list(SEMANTIC_EDGE_KINDS):
        return True
    return any(kind in CROSS_KINDS for kind in kinds)
