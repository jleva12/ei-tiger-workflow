"""The code graph as the apps read it: one object per process, opened from
:class:`~forge_codegraph_query.settings.CodeGraphSettings`, whose methods
check their arguments the same way for every caller.

The caller decides who may read which repository: these methods read any
repository they are given. The MCP server checks its caller's organization's
repositories first; the admin API and ADK workflows pass only the
repositories of the knowledge bases and code repositories the person or run
may use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Self, cast

from forge_codegraph_query.embedding import Embedder, OpenAIEmbedder
from forge_codegraph_query.errors import InvalidRequest, Unavailable
from forge_codegraph_query.model import (
    BOTH,
    DIRECTIONS,
    CrossLink,
    NeighborPage,
    NeighborQuery,
    Version,
)
from forge_codegraph_query.query.search import CodeSearch, search_code
from forge_codegraph_query.query.source import MAX_CONTEXT_LINES, SourceResult, node_source
from forge_codegraph_query.query.view import GraphView, graph_view
from forge_codegraph_query.settings import CodeGraphSettings
from forge_codegraph_query.spanner import SpannerGraphStore, VectorLengthMismatch
from forge_codegraph_query.store import (
    CrossLinkWriter,
    GraphStore,
    RepositoryStats,
    RunPage,
)

logger = logging.getLogger(__name__)

# The most repositories one search reads, and the most hits it returns.
MAX_SEARCH_REPOSITORIES = 32
MAX_SEARCH_LIMIT = 50
MAX_QUERY_BYTES = 8000
MAX_NODE_ID = 1024

# Defaults and bounds of the explorer's reads.
DEFAULT_NEIGHBORS = 40
MAX_NEIGHBORS = 200
DEFAULT_SYMBOLS = 20
MAX_SYMBOLS = 200
DEFAULT_CONTEXT_LINES = 3
DEFAULT_RUNS = 10
MAX_RUNS = 50


def open_store(settings: CodeGraphSettings) -> SpannerGraphStore:
    """The worker's database. :raises ValueError: The code graph isn't set up."""
    spanner = settings.spanner
    if spanner.database is None or spanner.cursor_signing_key is None:
        raise ValueError(f"the code graph isn't set up: set {settings.missing}")
    return SpannerGraphStore(
        spanner.database,
        scope=spanner.scope,
        cursor_signing_key=spanner.cursor_signing_key.get_secret_value().encode(),
        vector_length=settings.embedding.vector_length,
        credentials_json=(
            spanner.credentials_json.get_secret_value() if spanner.credentials_json else None
        ),
        timeout=spanner.timeout,
    )


def open_embedder(settings: CodeGraphSettings) -> OpenAIEmbedder | None:
    """The worker's embedding model for questions; None leaves search
    lexical."""
    embedding = settings.embedding
    if not embedding.enabled:
        if embedding.model:
            logger.warning("An embedding model without an API key: code search stays lexical")
        return None
    assert embedding.api_key is not None
    return OpenAIEmbedder(
        embedding.api_key.get_secret_value(),
        embedding.model,
        embedding.vector_length,
        base_url=embedding.base_url,
        max_input_bytes=embedding.max_input_bytes,
    )


class CodeGraph:
    """
    Reads repositories' code graphs and writes the links people draw between
    them.

    :param store: The graph; :class:`SpannerGraphStore` outside tests.
    :param embedder: Embeds questions for the semantic branch; None searches
        by words only.
    :param enhance_query: Spanner's query enhancement, for every search.
    :param embedding_model: The model whose vectors the ingestion audit
        counts, with ``embedding_dimensions``; empty counts none.
    """

    def __init__(
        self,
        store: GraphStore,
        embedder: Embedder | None = None,
        *,
        enhance_query: bool = False,
        embedding_model: str = "",
        embedding_dimensions: int = 0,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.enhance_query = enhance_query
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._on_close = on_close

    @classmethod
    def open(cls, settings: CodeGraphSettings) -> Self:
        """
        Opens the worker's database and the embedding model, without
        reading either: :meth:`wait` checks the database.

        :raises ValueError: The code graph isn't set up.
        """
        store = open_store(settings)
        embedder = open_embedder(settings)

        async def close() -> None:
            await store.close()
            if embedder is not None:
                await embedder.close()

        return cls(
            store,
            embedder,
            enhance_query=settings.search.enhance_query,
            embedding_model=settings.embedding.model,
            embedding_dimensions=settings.embedding.vector_length,
            on_close=close,
        )

    @property
    def semantic(self) -> bool:
        return self.embedder is not None

    async def wait(self, patience: float) -> None:
        """
        Waits up to ``patience`` seconds for the database to answer, with an
        embedding column of the configured length.

        :raises VectorLengthMismatch: At once: the database was made for
            another embedding dimension, and waiting won't change that.
        :raises Exception: The database didn't answer in time.
        """
        ping = getattr(self.store, "ping", None)
        if ping is None:
            return
        deadline = time.monotonic() + patience
        while True:
            try:
                await ping()
                return
            except VectorLengthMismatch:
                raise
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise
                logger.warning("Waiting for the code graph database: %s", str(exc)[:200])
                await asyncio.sleep(2)

    async def live(self) -> None:
        """Whether the database answers: a readiness probe."""
        live = getattr(self.store, "live", None)
        if live is not None:
            await live()

    async def close(self) -> None:
        if self._on_close is not None:
            await self._on_close()

    # --- search -----------------------------------------------------------

    async def search(
        self,
        repository_ids: Sequence[str],
        query: str,
        *,
        limit: int = 0,
        source: bool = False,
        mode: str = "",
        kinds: list[str] | None = None,
        path_prefix: str = "",
        near_path: str = "",
        expand: bool = True,
    ) -> CodeSearch:
        """
        The declarations that best answer ``query`` across repositories,
        ranked together, best first (:func:`search_code`). No repository finds
        nothing.

        :param limit: 1 to :data:`MAX_SEARCH_LIMIT` hits; 0 means 10.
        :param source: Each hit's code, cut to a few kilobytes.
        :param expand: A lexical second pass with the repositories' own
            vocabulary when the first finds no exact match.
        :raises InvalidRequest: More than :data:`MAX_SEARCH_REPOSITORIES`, an
            empty or too long query, a limit out of range.
        """
        ids = list(dict.fromkeys(repository_ids))
        text = query.strip()
        if not text or len(text.encode()) > MAX_QUERY_BYTES:
            raise InvalidRequest(f"query is required, at most {MAX_QUERY_BYTES} bytes")
        if len(ids) > MAX_SEARCH_REPOSITORIES:
            raise InvalidRequest(f"at most {MAX_SEARCH_REPOSITORIES} repositories per search")
        if not 0 <= limit <= MAX_SEARCH_LIMIT:
            raise InvalidRequest(f"limit must be 1 to {MAX_SEARCH_LIMIT}")
        if not ids:
            return CodeSearch()
        return await search_code(
            self.store,
            self.embedder,
            repository_ids=ids,
            text=text,
            limit=limit,
            mode=mode,
            kinds=kinds,
            path_prefix=path_prefix,
            near_path=near_path,
            enhance_query=self.enhance_query,
            expand=expand,
            source=source,
        )

    # --- the explorer -----------------------------------------------------

    async def view(
        self, repository_id: str, *, generation: int = 0, kind: str = "", cursor: str = ""
    ) -> GraphView:
        """A page of seed nodes, of one kind or any, with a bounded
        neighborhood around each (:func:`graph_view`)."""
        _generation(generation)
        try:
            return await graph_view(
                self.store,
                repository_id=repository_id,
                generation=generation,
                kind=kind,
                cursor=cursor,
            )
        except TimeoutError:
            raise Unavailable("the graph took too long to read; retry") from None

    async def node(self, repository_id: str, node_id: str, *, generation: int = 0) -> Version:
        _node_id(node_id)
        _generation(generation)
        return await self.store.get_node(repository_id, node_id, generation)

    async def neighbors(
        self,
        repository_id: str,
        node_id: str,
        *,
        direction: str = BOTH,
        generation: int = 0,
        limit: int = DEFAULT_NEIGHBORS,
        cursor: str = "",
    ) -> NeighborPage:
        """A node's edges, in id order, and the nodes at their far ends."""
        _node_id(node_id)
        _generation(generation)
        if direction not in DIRECTIONS:
            raise InvalidRequest("direction must be in, out or both")
        _bounded("limit", limit, 1, MAX_NEIGHBORS)
        return await self.store.neighbors(
            NeighborQuery(
                repository_id=repository_id,
                node_id=node_id,
                direction=direction,
                generation=generation,
                limit=limit,
                cursor=cursor,
            )
        )

    async def symbols(
        self,
        repository_id: str,
        *,
        name: str = "",
        qualified_name: str = "",
        generation: int = 0,
        limit: int = DEFAULT_SYMBOLS,
    ) -> list[Version]:
        """The nodes with exactly that name (any case) or qualified name."""
        name, qualified_name = name.strip(), qualified_name.strip()
        if bool(name) == bool(qualified_name):
            raise InvalidRequest("give exactly one of name and qualified_name")
        _generation(generation)
        _bounded("limit", limit, 1, MAX_SYMBOLS)
        return await self.store.find_nodes(
            repository_id, name, qualified_name, None, generation, limit
        )

    async def source(
        self,
        repository_id: str,
        node_id: str,
        *,
        generation: int = 0,
        context: int = DEFAULT_CONTEXT_LINES,
    ) -> SourceResult:
        """The code a node was read from, with lines of context around it."""
        _node_id(node_id)
        _generation(generation)
        _bounded("context", context, 0, MAX_CONTEXT_LINES)
        return await node_source(
            self.store,
            repository_id=repository_id,
            node_id=node_id,
            generation=generation,
            context_lines=context,
        )

    # --- the ingestion audit ----------------------------------------------

    async def stats(self, repository_id: str) -> RepositoryStats:
        """The live graph's totals, and its embeddings for the configured
        model."""
        return await self.store.stats(
            repository_id, self._embedding_model, self._embedding_dimensions
        )

    async def runs(
        self, repository_id: str, *, limit: int = DEFAULT_RUNS, cursor: str = ""
    ) -> RunPage:
        _bounded("limit", limit, 1, MAX_RUNS)
        if len(cursor) > 8192:
            raise InvalidRequest("cursor is too long")
        return await self.store.runs(repository_id, limit, cursor)

    # --- cross-repository links -------------------------------------------

    async def replace_cross_links(
        self, owner: str, links: Sequence[CrossLink | Mapping[str, Any]]
    ) -> int:
        """
        Makes ``links`` the owner's whole set (e.g. ``kb:<id>``): links it had
        and no longer lists are removed.

        :return: How many links the owner has now.
        :raises InvalidRequest: A link breaks the rules, or the store can't
            write.
        """
        if not hasattr(self.store, "replace_cross_links"):
            raise InvalidRequest("this graph store doesn't write cross-repository links")
        writer = cast(CrossLinkWriter, self.store)
        parsed = [
            link if isinstance(link, CrossLink) else CrossLink.from_json(link) for link in links
        ]
        await writer.replace_cross_links(owner, parsed)
        return len(parsed)


def _generation(generation: int) -> None:
    if generation < 0:
        raise InvalidRequest("generation must be a whole number")


def _node_id(node_id: str) -> None:
    if not node_id or len(node_id) > MAX_NODE_ID:
        raise InvalidRequest("node is required")


def _bounded(name: str, value: int, least: int, most: int) -> None:
    if not least <= value <= most:
        raise InvalidRequest(f"{name} must be {least}-{most}")
