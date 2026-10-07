"""The graph as the worker keeps it in Spanner, read-only.

A port of the reads of the worker's store (packages/go/code-graph/storage):
the same tables, indexes, queries and generation rules, so this server
answers as the Go server did. The worker owns the schema and every write;
this server never writes.

Generation semantics: a reader at generation L sees a record version iff
``GenFrom <= L AND (GenTo IS NULL OR GenTo > L)``. Nothing above a
repository's live generation is ever shown: a version closed above live (by a
generation still loading) reads as open, and versions opened above live are
invisible.

The Spanner client is synchronous, so each operation runs in a worker thread,
inside one read-only snapshot when it reads more than once.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from google.api_core import exceptions as google_errors
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types
from google.cloud.spanner_v1.pool import BurstyPool

from forge_codegraph_mcp.graph import codesearch
from forge_codegraph_mcp.graph.cursor import CursorCodec
from forge_codegraph_mcp.graph.embedding import EmbeddingError, validate_vector
from forge_codegraph_mcp.graph.errors import IntegrityFailure, InvalidRequest, NotFound
from forge_codegraph_mcp.graph.model import (
    BOTH,
    DIRECTIONS,
    EDGE,
    IN,
    NODE,
    OUT,
    SEMANTIC_EDGE_KINDS,
    CrossLink,
    CrossLinkQuery,
    Neighbor,
    NeighborPage,
    NeighborQuery,
    RepositoryState,
    Version,
    prop,
    valid_id,
    valid_repository,
)
from forge_codegraph_mcp.graph.store import (
    CHANGE_ADDED,
    CHANGE_RETIRED,
    CHANGE_UPDATED,
    SEARCH_HYBRID,
    SEARCH_LEXICAL,
    SEARCH_SEMANTIC,
    ChangesPage,
    EdgeRef,
    Hub,
    RecordChange,
    Repository,
    SearchHit,
    SearchRequest,
)

DATABASE_NAME = re.compile(r"^projects/[^/\s]+/instances/[^/\s]+/databases/[^/\s]+$")
MAX_VECTOR_LENGTH = 4096

RECORD_SELECT = (
    "RecordKind, RecordID, GenFrom, GenTo, CommitFrom, CommitTo, Retired, Lineage, FactDigest, "
    "Payload"
)
# Lets the emulator use a NULL_FILTERED index for queries whose predicates
# already exclude NULL keys; managed Spanner checks that itself.
NULL_FILTERED_HINT = "@{spanner_emulator.disable_query_null_filtered_index_check=true} "

ID_BATCH = 500  # ids per IN UNNEST query
WALK_CHUNK = 500  # node ids per edge-index query
MAX_HUBS = 200
MAX_CROSS_LINKS = 2000

# Search: weighted reciprocal rank fusion of the lexical and vector branches,
# then a rerank by exact names, connectivity and path proximity.
MAX_SEARCH_LIMIT = 50
RRF_K = 60.0
EXACT_MATCH_BOOST = 1.0  # dwarfs any fused score, at most 2/(RRF_K+1)
NAME_MATCH_BOOST = 0.02  # between one and two first-rank branch contributions
RERANK_BOOST = 0.004  # about a quarter of a first-rank branch contribution
MIN_NEEDLE_LENGTH = 3  # words shorter than this are never tried as names
SNIPPET_BYTES = 240
CANDIDATE_FACTOR = 4  # candidates per branch relative to the requested page
MIN_CANDIDATES = 32
EXACT_TIER_LIMIT = 200  # exact name matches are never cut by the page size
SUBSTRING_MINIMUM = 4  # TOKENIZE_SUBSTRING's shortest n-gram
ANN_LEAVES_TO_SEARCH = 32
ANN_FILTER_FACTOR = 4

# The kinds a plain word of a question may name: types and callables.
NAMED_KINDS = (
    "class", "interface", "enum", "record", "annotation_type", "method", "constructor", "function",
)  # fmt: skip


class VectorLengthMismatch(RuntimeError):
    """The database was made for embeddings of another dimension: waiting
    won't change that."""


def _open_predicate(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"{a}GenFrom<=@gen AND ({a}GenTo IS NULL OR {a}GenTo>@gen)"


def _types(params: dict[str, Any]) -> dict[str, Any]:
    def infer(value: Any) -> Any:
        if isinstance(value, bool):
            return param_types.BOOL
        if isinstance(value, int):
            return param_types.INT64
        if isinstance(value, float):
            return param_types.FLOAT64
        if isinstance(value, list | tuple):
            return param_types.Array(infer(value[0]) if value else param_types.STRING)
        return param_types.STRING

    return {
        name: param_types.Array(param_types.FLOAT32) if name == "vector" else infer(value)
        for name, value in params.items()
    }


def _bytes(value: Any) -> bytes:
    """A BYTES cell: the client hands them over base64-encoded."""
    return base64.b64decode(value) if value is not None else b""


def _valid_kinds(kinds: Iterable[str] | None) -> bool:
    kinds = list(kinds or ())
    return len(kinds) <= 32 and all(0 < len(k) <= 64 for k in kinds)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _unique(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _mask_above_live(version: Version, live: int) -> Version:
    if version.gen_to > live:
        return Version(
            fact=version.fact,
            gen_from=version.gen_from,
            commit_from=version.commit_from,
            lineage=version.lineage,
            fact_digest=version.fact_digest,
        )
    return version


def _version(row: Sequence[Any], live: int) -> Version:
    kind, record_id, gen_from, gen_to, commit_from, commit_to, retired, lineage, digest, payload = (
        row
    )
    fact = Version.decode_fact(_bytes(payload), kind, record_id)
    version = Version(
        fact=fact,
        gen_from=int(gen_from),
        commit_from=commit_from or "",
        lineage=lineage or "",
        gen_to=int(gen_to) if gen_to is not None else 0,
        commit_to=commit_to or "",
        retired=bool(retired),
        fact_digest=digest or "",
    )
    return _mask_above_live(version, live)


def _missing_table(exc: Exception) -> bool:
    """A query of a table the database doesn't have: one made before
    cross-repository links."""
    return isinstance(exc, google_errors.InvalidArgument | google_errors.NotFound) and (
        "Table not found" in str(exc)
    )


def _full_name(url: str) -> str:
    path = urlsplit(url).path.strip("/").removesuffix(".git")
    return path if path.count("/") == 1 else ""


class SpannerGraphStore:
    """Reads the worker's graph database. ``scope`` and ``cursor_signing_key``
    must be the worker's, so cursors are signed and fingerprinted alike, and
    ``vector_length`` the dimension its embedding column was created with."""

    def __init__(
        self,
        database: str,
        *,
        scope: str,
        cursor_signing_key: bytes,
        vector_length: int,
        credentials_json: str | None = None,
        timeout: float = 30.0,
        page_size: int = 500,
    ) -> None:
        if not DATABASE_NAME.match(database):
            raise ValueError("spanner database must be projects/P/instances/I/databases/D")
        if not scope or len(scope) > 256:
            raise ValueError("spanner scope required")
        if len(cursor_signing_key) < 16:
            raise ValueError("cursor signing key of at least 16 bytes required")
        if not 1 <= vector_length <= MAX_VECTOR_LENGTH:
            raise ValueError(f"vector length 1..{MAX_VECTOR_LENGTH} required")
        self.database = database
        self.vector_length = vector_length
        self._timeout = timeout
        self._page_size = page_size
        self._cursors = CursorCodec(cursor_signing_key, f"{database}|{scope}")
        _, project, _, instance, _, name = database.split("/")
        # Compose overlays set an empty value to turn the emulator off; the
        # client would take it for an endpoint.
        if os.environ.get("SPANNER_EMULATOR_HOST") == "":
            os.environ.pop("SPANNER_EMULATOR_HOST")
        # The client's multiplexed-session maintenance thread sleeps for ten
        # minutes and close() joins it; pooled sessions shut down promptly.
        for option in (
            "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS",
            "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS_FOR_RW",
            "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS_PARTITIONED_OPS",
        ):
            os.environ.setdefault(option, "false")
        credentials = None
        if credentials_json:
            from google.oauth2 import service_account

            try:
                info = json.loads(credentials_json)
                if not isinstance(info, dict) or info.get("type") != "service_account":
                    raise ValueError("expected a service account")
                credentials = service_account.Credentials.from_service_account_info(info)
            except (ValueError, KeyError, TypeError):
                # Never the credential bytes or the parser's error.
                raise ValueError(
                    "the Google credentials must be a service account's JSON key"
                ) from None
        self._client = spanner.Client(project=project, credentials=credentials)
        self._pool = BurstyPool(target_size=16)
        self._db = self._client.instance(instance).database(name, pool=self._pool)

    # --- lifecycle --------------------------------------------------------

    async def live(self) -> None:
        """Whether the database answers at all: the readiness probe."""
        await self._run(lambda: self._single("SELECT 1"))

    async def ping(self) -> None:
        """Checks that the database answers and that its embedding column has
        the configured length, so questions are never embedded at a dimension
        the vector index can't search."""

        def check() -> None:
            rows = self._single(
                "SELECT SPANNER_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='' "
                "AND TABLE_NAME='CGSearchEmbeddings' AND COLUMN_NAME='Embedding'",
            )
            if not rows:
                raise RuntimeError(
                    f"database {self.database} has no CGSearchEmbeddings.Embedding column: "
                    "the code graph worker creates its schema; start it first"
                )
            want = f"ARRAY<FLOAT32>(vector_length=>{self.vector_length})"
            if rows[0][0] != want:
                raise VectorLengthMismatch(
                    f"database {self.database} embedding column is {rows[0][0]}, configured "
                    f"{want}: the embedding dimensions must be the worker's"
                )

        await self._run(check)

    async def close(self) -> None:
        def close() -> None:
            self._pool.clear()
            transport = getattr(getattr(self._db, "spanner_api", None), "transport", None)
            if transport is not None:
                transport.close()

        await asyncio.to_thread(close)

    # --- plumbing ---------------------------------------------------------

    async def _run[T](self, operation: Callable[[], T]) -> T:
        return await asyncio.to_thread(operation)

    def _rows(self, reader: Any, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        params = params or {}
        return list(
            reader.execute_sql(
                sql, params=params, param_types=_types(params), timeout=self._timeout
            )
        )

    def _single(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        """One query in its own single-use snapshot."""
        with self._db.snapshot() as snapshot:
            return self._rows(snapshot, sql, params)

    def _page_limit(self, limit: int) -> int:
        return self._page_size if limit <= 0 or limit > self._page_size else limit

    def _repository_row(self, reader: Any, repo: str) -> list[Any]:
        rows = self._rows(
            reader,
            "SELECT RepositoryID, LiveGeneration, LiveCommit, LiveRunID, Payload "
            "FROM CGRepositories WHERE RepositoryID=@repo",
            {"repo": repo},
        )
        if not rows:
            raise NotFound(f"repository {repo}")
        return rows[0]

    def _live(self, reader: Any, repo: str) -> int:
        return int(self._repository_row(reader, repo)[1])

    def _generation_for(self, reader: Any, repo: str, requested: int) -> tuple[int, int]:
        """The generation to read (0 is live) and live; generations still
        loading are never readable."""
        live = self._live(reader, repo)
        if requested == 0:
            return live, live
        if requested < 0 or requested > live:
            raise InvalidRequest(f"generation {requested} above live {live}")
        return requested, live

    def _versions(self, reader: Any, sql: str, params: dict[str, Any], live: int) -> list[Version]:
        return [_version(row, live) for row in self._rows(reader, sql, params)]

    def _read_open_nodes(
        self, reader: Any, repo: str, ids: Iterable[str], gen: int, live: int
    ) -> dict[str, Version]:
        out: dict[str, Version] = {}
        unique = _unique(ids)
        for start in range(0, len(unique), ID_BATCH):
            chunk = unique[start : start + ID_BATCH]
            for version in self._versions(
                reader,
                f"SELECT {RECORD_SELECT} FROM CGRecords WHERE RepositoryID=@repo AND "
                f"RecordKind='node' AND RecordID IN UNNEST(@ids) AND {_open_predicate()}",
                {"repo": repo, "ids": chunk, "gen": gen},
                live,
            ):
                out[version.id] = version
        return out

    def _read_latest_versions(
        self, reader: Any, repo: str, kind: str, ids: Iterable[str], gen: int, live: int
    ) -> dict[str, Version]:
        """Per id, the version open at gen or, when the record isn't open
        there, its latest version with GenFrom <= gen (a retired record's
        last version)."""
        out: dict[str, Version] = {}
        unique = _unique(ids)
        for start in range(0, len(unique), ID_BATCH):
            chunk = unique[start : start + ID_BATCH]
            for version in self._versions(
                reader,
                f"SELECT {RECORD_SELECT} FROM CGRecords WHERE RepositoryID=@repo AND "
                "RecordKind=@kind AND RecordID IN UNNEST(@ids) AND GenFrom<=@gen",
                {"repo": repo, "kind": kind, "ids": chunk, "gen": gen},
                live,
            ):
                current = out.get(version.id)
                if (
                    current is None
                    or (version.open_at(gen) and not current.open_at(gen))
                    or (not current.open_at(gen) and version.gen_from > current.gen_from)
                ):
                    out[version.id] = version
        return out

    # --- the read surface ---------------------------------------------------

    async def state(self, repo: str) -> RepositoryState:
        if not valid_repository(repo):
            raise InvalidRequest("repository id")

        def read() -> RepositoryState:
            with self._db.snapshot() as snapshot:
                _, live, commit, run_id, payload = self._repository_row(snapshot, repo)
            try:
                registration = json.loads(_bytes(payload))
            except ValueError:
                raise IntegrityFailure(f"repository {repo} payload") from None
            return RepositoryState(
                repository_id=repo,
                branch=str(registration.get("branch") or ""),
                live_generation=int(live),
                live_commit=commit or "",
                live_run_id=run_id or "",
            )

        return await self._run(read)

    async def repositories(
        self, limit: int, ids: Sequence[str] | None = None
    ) -> tuple[list[Repository], bool]:
        if ids is not None and not ids:
            return [], False

        def read() -> tuple[list[Repository], bool]:
            only = "" if ids is None else " AND RepositoryID IN UNNEST(@ids)"
            params: dict[str, Any] = {"limit": limit + 1}
            if ids is not None:
                params["ids"] = list(ids)
            rows = self._single(
                "SELECT RepositoryID, LiveGeneration, LiveCommit, Payload FROM CGRepositories "
                f"WHERE LiveGeneration > 0{only} ORDER BY RepositoryID LIMIT @limit",
                params,
            )
            out: list[Repository] = []
            for repo, live, commit, payload in rows[:limit]:
                try:
                    registration = json.loads(_bytes(payload))
                except ValueError:
                    registration = {}
                url = str(registration.get("github_url") or "")
                out.append(
                    Repository(
                        id=repo,
                        full_name=_full_name(url),
                        url=url,
                        branch=str(registration.get("branch") or ""),
                        live_generation=int(live),
                        live_commit=commit or "",
                    )
                )
            return out, len(rows) > limit

        return await self._run(read)

    async def get_node(self, repo: str, node_id: str, generation: int) -> Version:
        if not valid_repository(repo) or not valid_id(node_id):
            raise InvalidRequest("repository or node id")

        def read() -> Version:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, live = self._generation_for(snapshot, repo, generation)
                found = self._versions(
                    snapshot,
                    f"SELECT {RECORD_SELECT} FROM CGRecords WHERE RepositoryID=@repo AND "
                    f"RecordKind=@kind AND RecordID=@id AND {_open_predicate()} LIMIT 1",
                    {"repo": repo, "kind": NODE, "id": node_id, "gen": gen},
                    live,
                )
            if not found:
                raise NotFound(f"node {node_id} at generation {gen}")
            return found[0]

        return await self._run(read)

    async def get_nodes(self, repo: str, ids: list[str], generation: int) -> dict[str, Version]:
        if not valid_repository(repo):
            raise InvalidRequest("repository id")
        for node_id in ids:
            if not valid_id(node_id):
                raise InvalidRequest(f"node id {node_id!r}")

        def read() -> dict[str, Version]:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, live = self._generation_for(snapshot, repo, generation)
                return self._read_latest_versions(snapshot, repo, NODE, ids, gen, live)

        return await self._run(read)

    async def history(self, repo: str, kind: str, record_id: str) -> list[Version]:
        if not valid_repository(repo) or not valid_id(record_id) or kind not in (NODE, EDGE):
            raise InvalidRequest("history key")

        def read() -> list[Version]:
            with self._db.snapshot(multi_use=True) as snapshot:
                live = self._live(snapshot, repo)
                versions = self._versions(
                    snapshot,
                    f"SELECT {RECORD_SELECT} FROM CGRecords WHERE RepositoryID=@repo AND "
                    "RecordKind=@kind AND RecordID=@id ORDER BY GenFrom",
                    {"repo": repo, "kind": kind, "id": record_id},
                    live,
                )
            return [v for v in versions if v.gen_from <= live]

        return await self._run(read)

    async def neighbors(self, query: NeighborQuery) -> NeighborPage:
        if (
            not valid_repository(query.repository_id)
            or not valid_id(query.node_id)
            or not _valid_kinds(query.edge_kinds)
            or query.direction not in DIRECTIONS
        ):
            raise InvalidRequest("neighbor query")
        limit = self._page_limit(query.limit)
        repo, node = query.repository_id, query.node_id
        kinds = list(query.edge_kinds)
        scope = self._cursors.scope("neighbors", repo, node, query.direction, kinds or None)

        def read() -> NeighborPage:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, live = self._generation_for(snapshot, repo, query.generation)
                after = self._cursors.decode(scope, gen, query.cursor)
                edges: dict[str, Version] = {}
                for direction in (OUT, IN):
                    if query.direction not in (BOTH, direction):
                        continue
                    index, column = (
                        ("CGEdgesByTarget", "TargetID")
                        if direction == IN
                        else ("CGEdgesBySource", "SourceID")
                    )
                    params: dict[str, Any] = {
                        "repo": repo,
                        "node": node,
                        "after": after,
                        "gen": gen,
                        "limit": limit + 1,
                    }
                    kind_filter = ""
                    if kinds:
                        kind_filter = " AND Kind IN UNNEST(@kinds)"
                        params["kinds"] = kinds
                    for version in self._versions(
                        snapshot,
                        f"{NULL_FILTERED_HINT}SELECT {RECORD_SELECT} FROM "
                        f"CGRecords@{{FORCE_INDEX={index}}} WHERE RepositoryID=@repo AND "
                        f"{column} IS NOT NULL AND {column}=@node AND RecordID>@after AND "
                        f"{_open_predicate()}{kind_filter} ORDER BY RecordID LIMIT @limit",
                        params,
                        live,
                    ):
                        edges.setdefault(version.id, version)
                ordered = [edges[edge_id] for edge_id in sorted(edges)]
                page = NeighborPage(generation=gen)
                if len(ordered) > limit:
                    ordered = ordered[:limit]
                    page.next_cursor = self._cursors.encode(scope, gen, ordered[-1].id)
                far = []
                for version in ordered:
                    edge = version.edge or {}
                    far.append(
                        edge.get("target_id")
                        if edge.get("source_id") == node
                        else edge.get("source_id")
                    )
                nodes = self._read_open_nodes(snapshot, repo, [f for f in far if f], gen, live)
            for version, far_id in zip(ordered, far, strict=True):
                page.neighbors.append(Neighbor(edge=version, node=nodes.get(far_id or "")))
            return page

        return await self._run(read)

    async def dependency_edges(
        self,
        repo: str,
        node_ids: list[str],
        direction: str,
        kinds: list[str] | tuple[str, ...] | None,
        generation: int,
        limit: int,
    ) -> tuple[list[EdgeRef], bool]:
        if not valid_repository(repo) or not _valid_kinds(kinds) or not node_ids or limit < 1:
            raise InvalidRequest("dependency edge query")
        for node_id in node_ids:
            if not valid_id(node_id):
                raise InvalidRequest(f"node id {node_id!r}")
        if direction not in DIRECTIONS:
            raise InvalidRequest(f"direction {direction!r}")
        kinds = list(kinds or ())
        unique = _unique(node_ids)

        def read() -> tuple[list[EdgeRef], bool]:
            edges: list[EdgeRef] = []
            truncated = False
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, _ = self._generation_for(snapshot, repo, generation)
                for side in (OUT, IN):
                    if direction not in (BOTH, side):
                        continue
                    index, column = (
                        ("CGEdgesByTarget", "TargetID")
                        if side == IN
                        else ("CGEdgesBySource", "SourceID")
                    )
                    for start in range(0, len(unique), WALK_CHUNK):
                        if truncated:
                            break
                        remaining = limit - len(edges)
                        if remaining <= 0:
                            truncated = True
                            break
                        params: dict[str, Any] = {
                            "repo": repo,
                            "ids": unique[start : start + WALK_CHUNK],
                            "gen": gen,
                            "limit": remaining + 1,
                        }
                        kind_filter = ""
                        if kinds:
                            kind_filter = " AND Kind IN UNNEST(@kinds)"
                            params["kinds"] = kinds
                        rows = self._rows(
                            snapshot,
                            f"{NULL_FILTERED_HINT}SELECT RecordID, Kind, SourceID, TargetID FROM "
                            f"CGRecords@{{FORCE_INDEX={index}}} WHERE RepositoryID=@repo AND "
                            f"{column} IS NOT NULL AND {column} IN UNNEST(@ids) AND "
                            f"{_open_predicate()}{kind_filter} ORDER BY RecordID LIMIT @limit",
                            params,
                        )
                        if len(rows) > remaining:
                            rows = rows[:remaining]
                            truncated = True
                        edges.extend(
                            EdgeRef(id=r[0], kind=r[1], source_id=r[2] or "", target_id=r[3] or "")
                            for r in rows
                        )
            return edges, truncated

        return await self._run(read)

    async def hubs(
        self, repo: str, generation: int, kinds: list[str] | None, limit: int
    ) -> list[Hub]:
        if not valid_repository(repo) or not _valid_kinds(kinds) or not 1 <= limit <= MAX_HUBS:
            raise InvalidRequest("hubs query")
        counted = list(kinds or SEMANTIC_EDGE_KINDS)

        def read() -> list[Hub]:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, _ = self._generation_for(snapshot, repo, generation)
                rows = self._rows(
                    snapshot,
                    f"{NULL_FILTERED_HINT}SELECT TargetID, SUM(n) AS total, "
                    "ARRAY_AGG(STRUCT(Kind AS kind, n AS n)) AS kinds FROM ("
                    "SELECT TargetID, Kind, COUNT(*) AS n FROM "
                    "CGRecords@{FORCE_INDEX=CGEdgesByTarget} WHERE RepositoryID=@repo AND "
                    f"TargetID IS NOT NULL AND Kind IN UNNEST(@kinds) AND {_open_predicate()} "
                    "GROUP BY TargetID, Kind) GROUP BY TargetID ORDER BY total DESC, TargetID "
                    "LIMIT @limit",
                    {"repo": repo, "kinds": counted, "gen": gen, "limit": limit},
                )
            out = []
            for target, total, parts in rows:
                by_kind: dict[str, int] = {}
                for part in parts or ():
                    if part is not None:
                        by_kind[part[0]] = by_kind.get(part[0], 0) + int(part[1])
                out.append(Hub(node_id=target, in_degree=int(total), by_kind=by_kind))
            out.sort(key=lambda hub: -hub.in_degree)
            return out

        return await self._run(read)

    async def changes(
        self, repo: str, generation: int, kind: str, limit: int, cursor: str
    ) -> ChangesPage:
        if not valid_repository(repo) or kind not in (NODE, EDGE):
            raise InvalidRequest("changes query")
        limit = self._page_limit(limit)

        def read() -> ChangesPage:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, live = self._generation_for(snapshot, repo, generation)
                if gen == 0:
                    raise NotFound("no generation has been published")
                scope = self._cursors.scope("changes", repo, gen, kind)
                after = self._cursors.decode(scope, gen, cursor)
                params: dict[str, Any] = {"repo": repo, "gen": gen, "kind": kind}
                # Totals over the whole generation: versions opened, versions
                # closed, and how many of those closures were retirements.
                opened_count = int(
                    self._rows(
                        snapshot,
                        "SELECT COUNT(*) FROM CGRecords@{FORCE_INDEX=CGRecordsByGenFrom} "
                        "WHERE RepositoryID=@repo AND GenFrom=@gen AND RecordKind=@kind",
                        params,
                    )[0][0]
                )
                closed_count, retired_count = self._rows(
                    snapshot,
                    f"{NULL_FILTERED_HINT}SELECT COUNT(*), COUNTIF(Retired) FROM "
                    "CGRecords@{FORCE_INDEX=CGRecordsByGenTo} WHERE RepositoryID=@repo AND "
                    "GenTo=@gen AND RecordKind=@kind",
                    params,
                )[0]
                page = ChangesPage(generation=gen)
                page.updated = int(closed_count) - int(retired_count)
                page.retired = int(retired_count)
                page.added = max(opened_count - page.updated, 0)
                page_params = {**params, "after": after, "limit": limit + 1}
                opened = self._versions(
                    snapshot,
                    f"SELECT {RECORD_SELECT} FROM CGRecords@{{FORCE_INDEX=CGRecordsByGenFrom}} "
                    "WHERE RepositoryID=@repo AND GenFrom=@gen AND RecordKind=@kind AND "
                    "RecordID>@after ORDER BY RecordID LIMIT @limit",
                    page_params,
                    live,
                )
                closed = self._versions(
                    snapshot,
                    f"{NULL_FILTERED_HINT}SELECT {RECORD_SELECT} FROM "
                    "CGRecords@{FORCE_INDEX=CGRecordsByGenTo} WHERE RepositoryID=@repo AND "
                    "GenTo=@gen AND RecordKind=@kind AND RecordID>@after ORDER BY RecordID "
                    "LIMIT @limit",
                    page_params,
                    live,
                )
            return self._merge_changes(page, opened, closed, limit, scope, gen, after)

        return await self._run(read)

    def _merge_changes(
        self,
        page: ChangesPage,
        opened: list[Version],
        closed: list[Version],
        limit: int,
        scope: str,
        gen: int,
        after: str,
    ) -> ChangesPage:
        # Both streams are cut at limit+1, so an id past the shorter stream's
        # last id may lack its counterpart: stop at the smaller last id to
        # keep pairs together.
        bound = ""
        if len(opened) > limit:
            bound = opened[limit].id
        if len(closed) > limit and (not bound or closed[limit].id < bound):
            bound = closed[limit].id
        i = j = 0
        while i < len(opened) or j < len(closed):
            if j >= len(closed) or (i < len(opened) and opened[i].id < closed[j].id):
                change = RecordChange(op=CHANGE_ADDED, id=opened[i].id, version=opened[i])
                i += 1
            elif i >= len(opened) or closed[j].id < opened[i].id:
                change = RecordChange(op=CHANGE_RETIRED, id=closed[j].id, version=closed[j])
                j += 1
            else:
                change = RecordChange(
                    op=CHANGE_UPDATED, id=opened[i].id, version=opened[i], before=closed[j]
                )
                i += 1
                j += 1
            if bound and change.id >= bound:
                last = page.changes[-1].id if page.changes else after
                page.next_cursor = self._cursors.encode(scope, gen, last)
                return page
            if not page.commit and change.version is not None:
                page.commit = (
                    change.version.commit_to
                    if change.op == CHANGE_RETIRED
                    else change.version.commit_from
                )
            page.changes.append(change)
            if len(page.changes) == limit:
                if i < len(opened) or j < len(closed):
                    page.next_cursor = self._cursors.encode(scope, gen, change.id)
                return page
        return page

    async def get_source(self, repo: str, sha256: str) -> bytes:
        if not valid_repository(repo) or not _valid_sha256(sha256):
            raise InvalidRequest("repository and content hash")

        def read() -> bytes:
            rows = self._single(
                "SELECT UncompressedBytes, Payload FROM CGContent "
                "WHERE RepositoryID=@repo AND SHA256=@sha",
                {"repo": repo, "sha": sha256},
            )
            if not rows:
                raise NotFound(f"source {sha256}")
            size, payload = int(rows[0][0]), _bytes(rows[0][1])
            try:
                data = gzip.decompress(payload)
            except (OSError, EOFError):
                raise IntegrityFailure(f"source {sha256} does not decompress") from None
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                raise IntegrityFailure(f"source {sha256} content differs from its hash")
            return data

        return await self._run(read)

    async def find_nodes(
        self,
        repo: str,
        name: str,
        qualified_name: str,
        kinds: list[str] | None,
        generation: int,
        limit: int,
    ) -> list[Version]:
        if (
            not valid_repository(repo)
            or bool(name) == bool(qualified_name)
            or len(name) > 1024
            or len(qualified_name) > 4096
            or not _valid_kinds(kinds)
        ):
            raise InvalidRequest("repository, one of name or qualified name, and kinds")
        limit = self._page_limit(limit)
        needle = (codesearch.simple_name(qualified_name) if qualified_name else name).lower()

        def read() -> list[Version]:
            with self._db.snapshot(multi_use=True) as snapshot:
                gen, live = self._generation_for(snapshot, repo, generation)
                sql = (
                    "SELECT NodeID, QualifiedName FROM "
                    "CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsByName} "
                    "WHERE RepositoryID=@repo AND NameLower=@needle"
                )
                params: dict[str, Any] = {
                    "repo": repo,
                    "needle": needle,
                    "limit": limit * CANDIDATE_FACTOR,
                }
                if qualified_name:
                    # Exact in the query: a repository can hold hundreds of
                    # same-named overrides, and the one asked for must not
                    # depend on sorting into the first candidates.
                    sql += " AND QualifiedName=@qualified"
                    params["qualified"] = qualified_name
                if kinds:
                    sql += " AND Kind IN UNNEST(@kinds)"
                    params["kinds"] = list(kinds)
                ids = [
                    node_id
                    for node_id, qualified in self._rows(
                        snapshot, sql + " ORDER BY NodeID LIMIT @limit", params
                    )
                    if not qualified_name or qualified == qualified_name
                ]
                if not ids:
                    return []
                nodes = self._read_open_nodes(snapshot, repo, ids, gen, live)
            return [nodes[node_id] for node_id in sorted(nodes)][:limit]

        return await self._run(read)

    async def cross_links(self, query: CrossLinkQuery) -> list[CrossLink]:
        if not valid_repository(query.repository_id) or query.direction not in DIRECTIONS:
            raise InvalidRequest("cross link query")
        for node_id in query.node_ids:
            if not valid_id(node_id):
                raise InvalidRequest("cross link query node id")
        for name in query.qualified_names:
            if not name.strip() or len(name) > 4096:
                raise InvalidRequest("cross link query qualified name")

        def read() -> list[CrossLink]:
            out: dict[tuple[str, str], CrossLink] = {}
            for direction in (OUT, IN):
                if query.direction not in (BOTH, direction):
                    continue
                side = "Target" if direction == IN else "Source"
                sql = (
                    f"SELECT Owner, LinkID, Payload FROM CGCrossLinks@{{FORCE_INDEX="
                    f"CGCrossLinksBy{side}}} WHERE {side}RepositoryID=@repo"
                )
                params: dict[str, Any] = {
                    "repo": query.repository_id,
                    "limit": 10 * MAX_CROSS_LINKS,
                }
                match = []
                if query.node_ids:
                    match.append(f"{side}NodeID IN UNNEST(@ids)")
                    params["ids"] = list(query.node_ids)
                if query.qualified_names:
                    match.append(f"{side}QualifiedName IN UNNEST(@names)")
                    params["names"] = list(query.qualified_names)
                if match:
                    sql += " AND (" + " OR ".join(match) + ")"
                try:
                    rows = self._single(sql + " LIMIT @limit", params)
                except google_errors.GoogleAPICallError as exc:
                    if _missing_table(exc):
                        return []
                    raise
                for owner, link_id, payload in rows:
                    try:
                        link = CrossLink.from_json(json.loads(_bytes(payload)))
                    except (ValueError, AttributeError):
                        raise IntegrityFailure(f"cross link {link_id} payload") from None
                    if link.owner != owner or link.id != link_id:
                        raise IntegrityFailure(f"cross link {link_id} payload key differs")
                    out.setdefault((owner, link_id), link)
            return [out[key] for key in sorted(out)]

        return await self._run(read)

    # --- search -----------------------------------------------------------

    async def hybrid_search(self, request: SearchRequest) -> list[SearchHit]:
        q = request
        if (
            not 1 <= len(q.repository_ids) <= 32
            or len(q.model) > 256
            or not _valid_kinds(q.kinds)
            or len(q.path_prefix) > 2048
            or len(q.near_path) > 2048
            or len(q.text.encode()) > 8000
            or not 0 <= q.limit <= MAX_SEARCH_LIMIT
        ):
            raise InvalidRequest("search request")
        for repo in q.repository_ids:
            if not valid_repository(repo):
                raise InvalidRequest("repository id")
        lexical, vector = _branches(q)
        if vector:
            assert q.vector is not None
            if len(q.vector) != self.vector_length:
                raise InvalidRequest(
                    f"{len(q.vector)}-dimensional query vector, database vector length is "
                    f"{self.vector_length}"
                )
            try:
                validate_vector(q.vector, len(q.vector))
            except EmbeddingError as exc:
                raise InvalidRequest(str(exc)) from None
        limit = q.limit or 10
        parsed = codesearch.parse_query(q.text)
        weights = _branch_weights(parsed, lexical, vector)

        def read() -> list[SearchHit]:
            hits: list[SearchHit] = []
            with self._db.snapshot(multi_use=True) as snapshot:
                for repo in q.repository_ids:
                    hits.extend(
                        self._search_repository(
                            snapshot, repo, q, limit, parsed, lexical, vector, weights
                        )
                    )
            hits.sort(key=lambda h: (-h.score, h.repository_id, h.node.id if h.node else ""))
            return hits[:limit]

        return await self._run(read)

    def _search_repository(
        self,
        snapshot: Any,
        repo: str,
        q: SearchRequest,
        limit: int,
        parsed: codesearch.Query,
        lexical: bool,
        vector: bool,
        weights: tuple[float, float],
    ) -> list[SearchHit]:
        live = self._live(snapshot, repo)
        terms = parsed.terms
        depth = max(limit * CANDIDATE_FACTOR, MIN_CANDIDATES)
        scores: dict[str, SearchHit] = {}
        # Index candidates: the document hash the index row describes.
        doc_hashes: dict[str, str] = {}

        def collect(sql: str, params: dict[str, Any], weight: float, branch: str) -> None:
            for rank, (node_id, digest, value) in enumerate(
                self._rows(snapshot, sql, params), start=1
            ):
                hit = scores.setdefault(node_id, SearchHit(repository_id=repo))
                hit.score += weight / (RRF_K + rank)
                doc_hashes[node_id] = digest
                if branch == "lexical":
                    hit.lexical = float(value)
                else:
                    hit.vector = 1 - float(value)

        # Level one, lexical: the search index over documents and identifiers.
        if lexical:
            params: dict[str, Any] = {
                "repo": repo,
                "q": " OR ".join(terms),
                "enhance": q.enhance_query,
                "version": codesearch.DOCUMENT_VERSION,
                "limit": depth,
            }
            where = (
                "SEARCH(d.Tokens, @q, enhance_query => @enhance) OR "
                "SEARCH(d.IdentifierTokens, @q, enhance_query => @enhance)"
            )
            if probe := _substring_probe(terms):
                params["sub"] = probe
                where += " OR SEARCH_SUBSTRING(d.IdentifierSubstrings, @sub)"
            sql = (
                "SELECT d.NodeID, d.DocumentHash, SCORE(d.Tokens, @q, enhance_query => @enhance) "
                "+ 2 * SCORE(d.IdentifierTokens, @q, enhance_query => @enhance) AS score "
                "FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsIndex} d "
                f"WHERE d.RepositoryID=@repo AND d.DocumentVersion=@version AND ({where})"
            )
            if q.kinds:
                sql += " AND d.Kind IN UNNEST(@kinds)"
                params["kinds"] = list(q.kinds)
            if q.path_prefix:
                sql += " AND STARTS_WITH(d.FilePath, @prefix)"
                params["prefix"] = q.path_prefix
            collect(
                sql + " ORDER BY score DESC, d.NodeID LIMIT @limit", params, weights[0], "lexical"
            )

        # Level one, vector: approximate nearest neighbours on the vector
        # index, whose query shape (distance the only sort key, a LIMIT, no
        # joins) leaves the open-at-live, kind, path and hash checks to the
        # candidates below; filters widen the candidate pool to compensate.
        if vector:
            candidates, leaves = depth, ANN_LEAVES_TO_SEARCH
            if q.kinds or q.path_prefix:
                candidates *= ANN_FILTER_FACTOR
                leaves *= ANN_FILTER_FACTOR
            sql = (
                "SELECT e.NodeID, e.DocumentHash, APPROX_COSINE_DISTANCE(e.Embedding, @vector, "
                f"options => JSON '{{\"num_leaves_to_search\": {leaves}}}') AS distance "
                "FROM CGSearchEmbeddings@{FORCE_INDEX=CGSearchEmbeddingsVector} e "
                "WHERE e.RepositoryID=@repo AND e.Model=@model AND e.DocumentVersion=@version "
                "ORDER BY distance LIMIT @limit"
            )
            params = {
                "repo": repo,
                "model": q.model,
                "version": codesearch.DOCUMENT_VERSION,
                "limit": candidates,
                "vector": list(q.vector or ()),
            }
            collect(sql, params, weights[1], "vector")

        # Exact tier: nodes the query names. A strong needle (the whole query
        # when it is one token, or a symbol it names) makes an exact match on
        # any kind; a weak one (a plain word of a question) a name match on
        # types and callables only.
        strong, weak = _name_needles(parsed)
        exact = self._named_nodes(snapshot, repo, strong, q.kinds)
        named: set[str] = set()
        named_kinds = _intersect_kinds(q.kinds, NAMED_KINDS)
        if named_kinds is not None:
            named = self._named_nodes(snapshot, repo, weak, named_kinds)
        for node_id in exact:
            scores.setdefault(node_id, SearchHit(repository_id=repo))
        for node_id in named - exact:
            scores.setdefault(node_id, SearchHit(repository_id=repo))
        symbols = set(parsed.symbols)

        nodes = self._read_open_nodes(snapshot, repo, list(scores), live, live)
        whole_query = q.text.strip()
        out: list[SearchHit] = []
        for node_id, hit in scores.items():
            version = nodes.get(node_id)
            if version is None or version.node is None:
                continue  # not open at live
            node = version.node
            if q.path_prefix and not prop(node, "file_path").startswith(q.path_prefix):
                continue
            if q.kinds and node.get("kind") not in q.kinds:
                continue
            document = codesearch.document_of(node)
            if node_id in doc_hashes and (document is None or doc_hashes[node_id] != document.hash):
                continue  # the index row describes text this node no longer has
            qualified = node.get("qualified_name") or ""
            if (
                node_id in exact
                or (whole_query and qualified == whole_query)
                or (qualified and qualified in symbols)
            ):
                hit.exact_match = True
                hit.score += EXACT_MATCH_BOOST
            elif node_id in named:
                hit.name_match = True
                hit.score += NAME_MATCH_BOOST
            hit.node = version
            hit.signature = prop(node, "signature")
            if document is not None:
                hit.matched = _matched_terms(document.text, terms)
            if source := _snippet_source(node):
                hit.snippet = codesearch.snippet(source, terms, SNIPPET_BYTES)
            out.append(hit)
        if not out:
            return []

        # Level two: rerank by connectivity and path proximity.
        ids = [hit.node.id for hit in out if hit.node is not None]
        callers = self._degrees(snapshot, repo, "CGEdgesByTarget", "TargetID", ids, live)
        callees = self._degrees(snapshot, repo, "CGEdgesBySource", "SourceID", ids, live)
        most = max(callers.values(), default=0)
        for hit in out:
            assert hit.node is not None
            hit.callers, hit.callees = callers.get(hit.node.id, 0), callees.get(hit.node.id, 0)
            if most > 0:
                hit.score += RERANK_BOOST * math.log1p(hit.callers) / math.log1p(most)
            file_path = prop(hit.node.node, "file_path")
            if q.near_path:
                hit.score += RERANK_BOOST * _path_proximity(file_path, q.near_path)
            if _path_hint(file_path, parsed.paths):
                hit.score += RERANK_BOOST
        return out

    def _named_nodes(
        self, snapshot: Any, repo: str, needles: set[str], kinds: Sequence[str] | None
    ) -> set[str]:
        """The documents whose lower-cased name is a needle, bounded per needle
        so one common name can't crowd out the others."""
        out: set[str] = set()
        for needle in sorted(needles):
            sql = (
                "SELECT NodeID FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsByName} "
                "WHERE RepositoryID=@repo AND NameLower=@needle"
            )
            params: dict[str, Any] = {"repo": repo, "needle": needle, "limit": EXACT_TIER_LIMIT}
            if kinds:
                sql += " AND Kind IN UNNEST(@kinds)"
                params["kinds"] = list(kinds)
            out.update(
                row[0]
                for row in self._rows(snapshot, sql + " ORDER BY NodeID LIMIT @limit", params)
            )
        return out

    def _degrees(
        self, snapshot: Any, repo: str, index: str, column: str, ids: list[str], gen: int
    ) -> dict[str, int]:
        """Per node, the open semantic edges on one side."""
        out: dict[str, int] = {}
        for start in range(0, len(ids), ID_BATCH):
            for node_id, count in self._rows(
                snapshot,
                f"{NULL_FILTERED_HINT}SELECT {column}, COUNT(*) FROM "
                f"CGRecords@{{FORCE_INDEX={index}}} WHERE RepositoryID=@repo AND "
                f"{column} IS NOT NULL AND {column} IN UNNEST(@ids) AND Kind IN UNNEST(@kinds) "
                f"AND {_open_predicate()} GROUP BY {column}",
                {
                    "repo": repo,
                    "ids": ids[start : start + ID_BATCH],
                    "kinds": list(SEMANTIC_EDGE_KINDS),
                    "gen": gen,
                },
            ):
                out[node_id] = int(count)
        return out


def _branches(q: SearchRequest) -> tuple[bool, bool]:
    """Which level-one branches run for the request."""
    has_text = bool(codesearch.terms(q.text))
    has_vector = q.vector is not None and bool(q.model)
    mode = q.mode or SEARCH_HYBRID
    if mode == SEARCH_HYBRID:
        lexical, vector = has_text, has_vector
    elif mode == SEARCH_LEXICAL:
        lexical, vector = has_text, False
    elif mode == SEARCH_SEMANTIC:
        lexical, vector = False, has_vector
    else:
        raise InvalidRequest(f"search mode {q.mode!r}")
    if not lexical and not vector:
        raise InvalidRequest(f"query text, or a vector with its model, required for mode {mode}")
    return lexical, vector


def _identifier_like(text: str) -> bool:
    """A single token that spells a symbol: camelCase, dots, parentheses or
    underscores."""
    text = text.strip()
    if not text or any(c in text for c in " \t\r\n"):
        return False
    if any(c in text for c in ".:(_$"):
        return True
    return any(codesearch.is_lower(c) for c in text) and any(codesearch.is_upper(c) for c in text)


def _branch_weights(q: codesearch.Query, lexical: bool, vector: bool) -> tuple[float, float]:
    """Favours the lexical branch for identifiers and the vector branch for
    natural language; a lone branch always weighs one."""
    if not (lexical and vector):
        return 1.0, 1.0
    if _identifier_like(q.text):
        return 1.0, 0.5
    if q.sentence:
        return 0.7, 1.0
    return 1.0, 1.0


def _exact_needle(text: str) -> str:
    text = text.strip()
    if not text or any(c in text for c in " \t\r\n"):
        return ""
    return text.lower()


def _name_needles(q: codesearch.Query) -> tuple[set[str], set[str]]:
    """The lower-cased names the exact tier looks up: strong ones (the query
    when it is one token, every symbol it names and the symbol's owner) and
    weak ones (a sentence's plain content words). A word is never both."""
    strong: set[str] = set()
    weak: set[str] = set()
    if needle := _exact_needle(q.text):
        strong.add(needle)
    for symbol in q.symbols:
        for name in (codesearch.simple_name(symbol), codesearch.owner(symbol)):
            if len(name.encode()) >= 2:
                strong.add(name.lower())
    if q.sentence:
        for term in q.terms:
            if len(term.encode()) >= MIN_NEEDLE_LENGTH and term not in strong:
                weak.add(term)
    return strong, weak


def _substring_probe(terms: list[str]) -> str:
    """The longest term long enough for the substring index, so a partial
    identifier such as "AsyncImp" still finds runAsyncImpl."""
    probe = ""
    for term in terms:
        if len(term.encode()) >= SUBSTRING_MINIMUM and len(term.encode()) > len(probe.encode()):
            probe = term
    return probe


def _intersect_kinds(requested: Sequence[str] | None, allowed: Sequence[str]) -> list[str] | None:
    """The requested kinds a word may name; every allowed kind without a
    request, None when the request names only kinds a word can't."""
    if not requested:
        return list(allowed)
    out = [kind for kind in requested if kind in allowed]
    return out or None


def _path_hint(file_path: str, paths: list[str]) -> bool:
    lower = file_path.lower()
    return any(p.strip("/").lower() in lower for p in paths)


def _path_proximity(a: str, b: str) -> float:
    """The share of leading directory segments two paths have in common."""
    da, db = a.split("/")[:-1], b.split("/")[:-1]
    total = max(len(da), len(db))
    if total == 0:
        return 0.0
    shared = 0
    while shared < len(da) and shared < len(db) and da[shared] == db[shared]:
        shared += 1
    return shared / total


def _snippet_source(node: dict[str, Any]) -> str:
    for key in ("source_text", "docstring", "signature"):
        if value := prop(node, key):
            return value
    return ""


def _matched_terms(document: str, terms: list[str]) -> list[str]:
    lower = document.lower()
    return [term for term in terms if term in lower]
