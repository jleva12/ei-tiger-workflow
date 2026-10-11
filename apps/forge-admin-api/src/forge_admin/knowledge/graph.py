"""System design knowledge bases: the code repositories a knowledge base includes
(``knowledge_base_repositories``) and searching their code in the code graph
(``forge_codegraph_query``, which reads the worker's Spanner database).

A repository is searched once an ingestion of it succeeded: that names its
graph (``code_ingestion_jobs.codegraph_repository_id``, one graph per GitHub
URL). One never ingested, or whose ingestions all failed, is in the
knowledge base but finds nothing yet.
"""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, Request, status
from forge_codegraph import Repository, Searched, search_passages
from forge_codegraph_query import CodeGraph, GraphError, VectorLengthMismatch, status_of
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.config import Settings
from forge_admin.models import (
    CodeIngestionJob,
    CodeRepository,
    KnowledgeBase,
    KnowledgeBaseRepository,
)
from forge_admin.models.code_repositories import SUCCEEDED

logger = logging.getLogger(__name__)


def not_set_up(settings: Settings) -> str:
    """Why the code graph is off, naming what to set."""
    missing = ", ".join(
        f"FORGE_ADMIN_CODEGRAPH__{name.upper().replace('.', '__')}"
        for name in settings.codegraph.missing.split(", ")
        if name
    )
    return f"The code graph isn't set up on this server: set {missing}"


def codegraph_from(settings: Settings) -> CodeGraph | None:
    """:return: The code graph; None when it isn't set up, or its client
    can't be made (no Google credentials, a malformed key): logged, and the
    rest of the admin API serves on."""
    if not settings.codegraph.enabled:
        return None
    try:
        return CodeGraph.open(settings.codegraph)
    except Exception as error:
        logger.error(
            "The code graph can't be opened, so it's off: %s", str(error)[:300]
        )
        return None


async def check_codegraph(graph: CodeGraph, patience: float) -> None:
    """Logs whether the worker's database answers, with embeddings of the
    configured length, within ``patience`` seconds."""
    try:
        await graph.wait(patience)
    except VectorLengthMismatch as error:
        logger.error("The code graph can't be searched: %s", error)
    except Exception as error:
        logger.warning("The code graph database isn't answering: %s", str(error)[:200])
    else:
        logger.info("The code graph is ready (semantic search: %s)", graph.semantic)


def codegraph_of(request: Request) -> CodeGraph:
    """
    :return: The code graph.
    :raises HTTPException: 503 when it isn't set up.
    """
    graph: CodeGraph | None = getattr(request.app.state, "codegraph", None)
    if graph is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, not_set_up(request.app.state.settings)
        )
    return graph


async def graph_ids(
    session: AsyncSession, repository_ids: Sequence[str]
) -> dict[str, str]:
    """:return: The graph of each repository an ingestion of succeeded, by
    repository; one never ingested is missing."""
    if not repository_ids:
        return {}
    rows = await session.execute(
        select(
            CodeIngestionJob.repository_id,
            func.max(CodeIngestionJob.codegraph_repository_id),
        )
        .where(
            CodeIngestionJob.repository_id.in_(list(repository_ids)),
            CodeIngestionJob.status == SUCCEEDED,
            CodeIngestionJob.codegraph_repository_id.is_not(None),
        )
        .group_by(CodeIngestionJob.repository_id)
    )
    return {repository: graph for repository, graph in rows if graph}


async def linked(
    session: AsyncSession, knowledge_base_ids: Sequence[str]
) -> dict[str, list[CodeRepository]]:
    """:return: Each knowledge base's repositories, by URL, by knowledge base."""
    found: dict[str, list[CodeRepository]] = {i: [] for i in knowledge_base_ids}
    if not knowledge_base_ids:
        return found
    rows = await session.execute(
        select(KnowledgeBaseRepository.knowledge_base_id, CodeRepository)
        .join(
            CodeRepository, CodeRepository.id == KnowledgeBaseRepository.repository_id
        )
        .where(KnowledgeBaseRepository.knowledge_base_id.in_(list(knowledge_base_ids)))
        .order_by(CodeRepository.url)
    )
    for knowledge_base_id, repository in rows:
        found[knowledge_base_id].append(repository)
    return found


async def searchable(
    session: AsyncSession, knowledge_base_ids: Sequence[str]
) -> dict[str, list[Repository]]:
    """:return: Each knowledge base's repositories with a graph to search, by
    knowledge base."""
    repositories = await linked(session, knowledge_base_ids)
    graphs = await graph_ids(
        session, list({r.id for rs in repositories.values() for r in rs})
    )
    return {
        knowledge_base_id: [
            Repository(id=r.id, graph_id=graphs[r.id], name=f"{r.owner}/{r.name}")
            for r in rs
            if r.id in graphs
        ]
        for knowledge_base_id, rs in repositories.items()
    }


async def search_code(
    graph: CodeGraph,
    knowledge_bases: Sequence[tuple[KnowledgeBase, Sequence[Repository]]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Search system design knowledge bases' code, ranked together
    (``forge_codegraph.search_passages``, as ADK workflows search it).

    :param knowledge_bases: Each knowledge base and its repositories with a
        graph (:func:`searchable`).
    :raises GraphError: The code graph refused the search or didn't answer.
    """
    return await search_passages(
        graph,
        [
            Searched(kb.id, kb.name, repositories)
            for kb, repositories in knowledge_bases
        ],
        query,
        limit=limit,
    )


def unavailable(error: GraphError) -> HTTPException:
    """The HTTP error for a code graph call that failed, its message as it
    is: an argument out of range (400), a repository, node or generation
    that isn't there (404), the database not answering (503)."""
    code = status_of(error)
    if code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.warning("The code graph failed a call: %s", error)
    if code == status.HTTP_500_INTERNAL_SERVER_ERROR:
        return HTTPException(code, "The code graph failed to read; try again")
    return HTTPException(code, error.detail)
