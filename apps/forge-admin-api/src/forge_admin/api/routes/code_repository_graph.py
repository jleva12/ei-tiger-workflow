"""
Reading an organization's code repository in the code graph: what the web
console's code graph explorer and Ingestion overview show, relayed from the
code graph worker's API (``forge_codegraph``).

Reading needs ``repositories:read`` in the organization. Every graph read
names a generation; 0 means the live one, and the first answer says which
that is, so the explorer pins it for the reads that follow. Graph reads
answer 409 before an ingestion of the repository succeeded; the stats answer
whatever the worker can tell, and say why when it can't.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import casbin
from fastapi import APIRouter, HTTPException, Query, Request, status
from forge_codegraph import CodeGraph, WorkerError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import NodeId, Session
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.code_repositories.access import repository_of
from forge_admin.db.audit import UtcDateTime
from forge_admin.knowledge.graph import codegraph_of, graph_ids, unavailable
from forge_admin.models import CodeIngestionJob, CodeRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["code repositories"])

#: Reading the organization's code in the code graph (0015repositories_read).
READ = "repositories:read"
#: Seconds the stats wait for the worker: they never fail because of it.
STATS_TIMEOUT = 5.0

Generation = Annotated[int, Query(ge=0, description="0 for the live generation")]
NodeParam = Annotated[str, Query(min_length=1, max_length=1024)]
Cursor = Annotated[str, Query(max_length=8192)]


async def _repository(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    repository_id: str,
) -> CodeRepository:
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    return await repository_of(session, enforcer, user, organization_id, repository_id)


async def _read(
    request: Request,
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    repository_id: str,
    what: str,
    params: dict[str, str | int],
) -> dict[str, Any]:
    repository = await _repository(
        session, enforcer, user, organization_id, repository_id
    )
    graph_id = (await graph_ids(session, [repository.id])).get(repository.id)
    if graph_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{repository.owner}/{repository.name} isn't in the code graph yet: "
            "ingest it first",
        )
    graph = codegraph_of(request)
    try:
        return await graph.read(graph_id, what, params)
    except WorkerError as error:
        raise unavailable(error) from None


@router.get("/organizations/{organization_id}/code-repositories/{repository_id}/graph")
async def graph_view(
    organization_id: NodeId,
    repository_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    kind: Annotated[str, Query(max_length=128)] = "",
    generation: Generation = 0,
    cursor: Cursor = "",
) -> dict[str, Any]:
    """
    A page of the repository's published graph: a sample of nodes, of one
    kind or any, with the edges among them and the generation, branch and
    commit they're from. ``next_cursor`` loads the next page.
    \f
    :raises HTTPException: 403 without ``repositories:read``; 404 for another
        organization's repository, or none; 409 before an ingestion of it
        succeeded; 502 when the worker refuses; 503 when it isn't set up or
        doesn't answer.
    """
    return await _read(
        request,
        session,
        enforcer,
        user,
        organization_id,
        repository_id,
        "graph",
        {"kind": kind, "generation": generation, "cursor": cursor},
    )


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/graph/neighbors"
)
async def graph_neighbors(
    organization_id: NodeId,
    repository_id: NodeId,
    node: NodeParam,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    generation: Generation = 0,
    direction: Literal["in", "out", "both"] = "both",
    limit: Annotated[int, Query(ge=1, le=200)] = 40,
    cursor: Cursor = "",
) -> dict[str, Any]:
    """A node's edges, with the nodes at their other ends, a page at a time."""
    return await _read(
        request,
        session,
        enforcer,
        user,
        organization_id,
        repository_id,
        "neighbors",
        {
            "node": node,
            "generation": generation,
            "direction": direction,
            "limit": limit,
            "cursor": cursor,
        },
    )


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/graph/symbols"
)
async def graph_symbols(
    organization_id: NodeId,
    repository_id: NodeId,
    name: Annotated[str, Query(min_length=1, max_length=512)],
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    generation: Generation = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """The nodes named exactly ``name``: ``{"nodes": [...]}``."""
    return await _read(
        request,
        session,
        enforcer,
        user,
        organization_id,
        repository_id,
        "symbols",
        {"name": name.strip(), "generation": generation, "limit": limit},
    )


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/graph/source"
)
async def graph_source(
    organization_id: NodeId,
    repository_id: NodeId,
    node: NodeParam,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    generation: Generation = 0,
    context: Annotated[int, Query(ge=0, le=200)] = 3,
) -> dict[str, Any]:
    """
    The source a node was read from, with ``context`` lines around it.
    \f
    :raises HTTPException: 404 for a node without source.
    """
    return await _read(
        request,
        session,
        enforcer,
        user,
        organization_id,
        repository_id,
        "source",
        {"node": node, "generation": generation, "context": context},
    )


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/graph/node"
)
async def graph_node(
    organization_id: NodeId,
    repository_id: NodeId,
    node: NodeParam,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    generation: Generation = 0,
) -> dict[str, Any]:
    """
    One node of the repository's published graph.
    \f
    :raises HTTPException: 404 for a node the generation doesn't have.
    """
    return await _read(
        request,
        session,
        enforcer,
        user,
        organization_id,
        repository_id,
        "node",
        {"node": node, "generation": generation},
    )


class GraphEmbeddings(BaseModel):
    # The model and dimensions the worker embeds with; empty when it doesn't.
    model: str = ""
    dimensions: int = 0
    # Searchable nodes whose stored vector matches what they are now.
    current: int = 0
    # Vectors stored for the repository with that model, current or not.
    stored: int = 0


class GraphTotals(BaseModel):
    """The live (published) graph: generation 0 until one is published."""

    branch: str = ""
    generation: int = 0
    commit_sha: str = ""
    run_id: str = ""
    nodes: int = 0
    edges: int = 0
    node_kinds: dict[str, int] = Field(default_factory=dict)
    edge_kinds: dict[str, int] = Field(default_factory=dict)
    # Nodes search can find: declarations and code chunks.
    searchable: int = 0
    embeddings: GraphEmbeddings = Field(default_factory=GraphEmbeddings)


class GraphRun(BaseModel):
    """One run of the code graph worker, as it reports it."""

    run_id: str
    # ACCEPTED, RUNNING, SUCCEEDED, FAILED, or SUPERSEDED by a newer run.
    phase: str
    branch: str = ""
    commit_sha: str = ""
    # The Forge user who asked for it.
    requested_by: str = ""
    # The generation it published; 0 until it does.
    generation: int = 0
    accepted_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    attempts: int = 0
    failed_attempts: int = 0
    error_code: str = ""
    # Why it failed, in the worker's words.
    error_message: str = ""
    # What it could not analyse although it went on, one warning per line.
    warning_message: str = ""
    # What it found and wrote: files, symbols, resolved references, nodes
    # and edges added, updated and retired, stage durations in ms…
    metrics: dict[str, Any] | None = None
    # Its embedding pass: model, dimensions, status, embedded, requests…
    index: dict[str, Any] | None = None

    @classmethod
    def of(cls, run: dict[str, Any]) -> "GraphRun":
        """:param run: The worker's run."""
        request = run.get("request") or {}
        return cls(
            run_id=(run.get("key") or {}).get("run_id", ""),
            phase=run.get("phase", ""),
            branch=request.get("branch", ""),
            commit_sha=request.get("target_commit_sha", ""),
            requested_by=request.get("requested_by", ""),
            generation=run.get("generation", 0),
            accepted_at=run.get("accepted_at"),
            started_at=run.get("started_at"),
            finished_at=run.get("finished_at"),
            attempts=run.get("attempts", 0),
            failed_attempts=run.get("failed_attempts", 0),
            error_code=run.get("error_code", ""),
            error_message=run.get("error_message", ""),
            warning_message=run.get("warning_message", ""),
            metrics=run.get("metrics"),
            index=run.get("index"),
        )


class CodeGraphStats(BaseModel):
    # ok; not_ingested before the worker took up an ingestion of it, or
    # when it no longer has it; unavailable when the worker isn't set up or
    # didn't answer.
    status: Literal["ok", "not_ingested", "unavailable"]
    # Why it's unavailable or missing.
    error: str = ""
    totals: GraphTotals | None = None
    # Its latest runs, newest first.
    runs: list[GraphRun] = Field(default_factory=list)


class RepositoryStatsRead(BaseModel):
    repository_id: str
    code_graph: CodeGraphStats
    as_of: UtcDateTime


@router.get("/organizations/{organization_id}/code-repositories/{repository_id}/stats")
async def code_repository_stats(
    organization_id: NodeId,
    repository_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    runs: Annotated[int, Query(ge=1, le=50)] = 10,
) -> RepositoryStatsRead:
    """
    What the code graph holds of the repository, to audit its ingestions:
    its live graph (nodes, edges, searchable nodes and their embeddings)
    with the worker's latest runs and what each found and wrote. A worker
    that isn't set up or doesn't answer leaves them unavailable, with why.
    \f
    :param runs: How many of the latest runs to include.
    :raises HTTPException: 403 without ``repositories:read``; 404 for another
        organization's repository, or none.
    """
    repository = await _repository(
        session, enforcer, user, organization_id, repository_id
    )
    # The graph the worker files the repository's runs under, which it
    # names the job from its first claim: there may be runs before one
    # succeeded.
    graph_id = await session.scalar(
        select(CodeIngestionJob.codegraph_repository_id)
        .where(
            CodeIngestionJob.repository_id == repository.id,
            CodeIngestionJob.codegraph_repository_id.is_not(None),
        )
        .order_by(CodeIngestionJob.created_at.desc())
        .limit(1)
    )
    graph: CodeGraph | None = getattr(request.app.state, "codegraph", None)
    return RepositoryStatsRead(
        repository_id=repository.id,
        code_graph=await _code_graph(graph, graph_id, runs),
        as_of=datetime.now(UTC),
    )


async def _code_graph(
    graph: CodeGraph | None, graph_id: str | None, runs: int
) -> CodeGraphStats:
    if graph is None:
        return CodeGraphStats(
            status="unavailable",
            error="The code graph isn't set up on this server "
            "(FORGE_ADMIN_CODEGRAPH_URL)",
        )
    if graph_id is None:
        return CodeGraphStats(status="not_ingested")
    try:
        async with asyncio.timeout(STATS_TIMEOUT):
            answers: list[dict[str, Any] | BaseException] = await asyncio.gather(
                graph.read(graph_id, "stats"),
                graph.read(graph_id, "runs", {"limit": runs}),
                return_exceptions=True,
            )
    except TimeoutError:
        return CodeGraphStats(status="unavailable", error="The worker didn't answer")
    totals, listed = answers
    if isinstance(totals, BaseException):
        return _refused(totals)
    if isinstance(listed, BaseException):
        return _refused(listed)
    try:
        return CodeGraphStats(
            status="ok",
            totals=GraphTotals.model_validate(totals),
            runs=[GraphRun.of(run) for run in listed.get("runs") or []],
        )
    except ValueError:
        logger.warning("The code graph worker's stats didn't parse", exc_info=True)
        return CodeGraphStats(
            status="unavailable", error="The worker's answer didn't parse"
        )


def _refused(error: BaseException) -> CodeGraphStats:
    """:raises BaseException: ``error``, unless it's the worker's."""
    if not isinstance(error, WorkerError):
        raise error
    if error.status == status.HTTP_404_NOT_FOUND and error.code != "invalid_response":
        return CodeGraphStats(
            status="not_ingested",
            error="The code graph no longer has this repository",
        )
    if error.status == 0:
        return CodeGraphStats(status="unavailable", error="The worker didn't answer")
    logger.warning("The code graph worker refused the stats: %s", error)
    return CodeGraphStats(
        status="unavailable",
        error=error.message or f"The worker answered {error.status}",
    )
