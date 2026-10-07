"""Graph knowledge bases: the code repositories a knowledge base includes
(``knowledge_base_repositories``) and searching their code in the code graph
(the worker's API, ``forge_codegraph``).

A repository is searched once an ingestion of it succeeded: that names its
graph on the worker (``code_ingestion_jobs.codegraph_repository_id``, one
graph per GitHub URL). One never ingested, or whose ingestions all failed,
is in the knowledge base but finds nothing yet.
"""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, Request, status
from forge_codegraph import CodeGraph, Repository, WorkerError, code_passages
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

NOT_SET_UP = (
    "The code graph isn't set up on this server: set FORGE_ADMIN_CODEGRAPH_URL "
    "and FORGE_ADMIN_CODEGRAPH_TOKEN"
)


def codegraph_from(settings: Settings) -> CodeGraph | None:
    """:return: The worker's client; None when the code graph isn't set up."""
    if settings.codegraph_url is None or settings.codegraph_token is None:
        return None
    return CodeGraph.connect(
        settings.codegraph_url,
        settings.codegraph_token.get_secret_value(),
        timeout=settings.codegraph_timeout,
    )


def codegraph_of(request: Request) -> CodeGraph:
    """
    :return: The worker's client.
    :raises HTTPException: 503 when the code graph isn't set up.
    """
    graph: CodeGraph | None = getattr(request.app.state, "codegraph", None)
    if graph is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
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


#: The most repositories one search of the worker covers.
MAX_SEARCHED = 50


async def search_code(
    graph: CodeGraph,
    knowledge_bases: Sequence[tuple[KnowledgeBase, Sequence[Repository]]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Search graph knowledge bases' code, ranked together: one search of the
    worker over their repositories, each hit cited as from the first of the
    knowledge bases that includes its repository.

    :param knowledge_bases: Each knowledge base and its repositories with a
        graph (:func:`searchable`).
    :return: Their passages, best first (``forge_codegraph.code_passages``);
        none without a repository to search.
    :raises WorkerError: The worker refused or could not be reached.
    """
    owners: dict[str, tuple[KnowledgeBase, Repository]] = {}
    for knowledge_base, repositories in knowledge_bases:
        for repository in repositories:
            owners.setdefault(repository.graph_id, (knowledge_base, repository))
    if not owners:
        return []
    if len(owners) > MAX_SEARCHED:
        logger.warning(
            "Searching the first %d of %d repositories", MAX_SEARCHED, len(owners)
        )
    hits = await graph.search(list(owners)[:MAX_SEARCHED], query, limit=limit)
    passages: list[dict[str, Any]] = []
    for hit in hits:
        owner = owners.get(str(hit.get("repository_id", "")))
        if owner is not None:
            knowledge_base, repository = owner
            passages += code_passages(
                [hit],
                knowledge_base_id=knowledge_base.id,
                knowledge_base=knowledge_base.name,
                repositories=[repository],
            )
    return passages


def unavailable(error: WorkerError) -> HTTPException:
    """The HTTP error for a worker call that failed: its 404 and 400 as they
    are; it refusing the admin API's token, or answering nonsense, is the
    deployment's fault (502); unreachable or failing, 503."""
    if error.status in (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND):
        return HTTPException(error.status, error.message or error.code)
    if error.status == 0 or (error.status >= 500 and error.code != "invalid_response"):
        logger.warning("The code graph worker is unavailable: %s", error)
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The code graph is unavailable; try again",
        )
    logger.error("The code graph worker refused a call: %s", error)
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, "The code graph worker refused the request"
    )
