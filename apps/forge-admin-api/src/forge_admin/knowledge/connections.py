"""A system design knowledge base's connections: how its applications (the
code repositories it includes) connect, drawn by hand on its system map
(``knowledge_base_connections``), and where in their code
(``knowledge_base_code_links``).

The knowledge base's code links are written into the code graph as
cross-repository links, which the graph's queries follow from one repository
into the other. The worker keeps one set per knowledge base (owner
``kb:<id>``), which the knowledge base's whole set replaces whenever it
changes: when a code link is added or removed, and when a connection, an
application or the knowledge base with code links goes. Callers sync before
they commit, so a refusal leaves nothing changed.
"""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, status
from forge_codegraph import CodeGraph, WorkerError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.knowledge.graph import graph_ids, unavailable
from forge_admin.models import (
    CodeRepository,
    KnowledgeBase,
    KnowledgeBaseCodeLink,
    KnowledgeBaseConnection,
)
from forge_admin.models.knowledge import SYSTEM

logger = logging.getLogger(__name__)

#: Each connection kind as the code graph's cross-repository link kinds name it.
CROSS_KINDS: dict[str, str] = {
    "connects_to": "connects_to",
    "calls": "calls_api",
    "depends_on": "depends_on",
    "events": "sends_event",
    "shares_data": "shares_data",
}


def owner_of(knowledge_base_id: str) -> str:
    """:return: Who keeps a knowledge base's links in the code graph."""
    return f"kb:{knowledge_base_id}"


async def has_code_links(
    session: AsyncSession,
    knowledge_base_id: str,
    *,
    connection_id: str | None = None,
    repository_id: str | None = None,
) -> bool:
    """:return: Whether the knowledge base has code links: any, a
    connection's, or those of connections to or from a repository."""
    query = (
        select(func.count())
        .select_from(KnowledgeBaseCodeLink)
        .join(
            KnowledgeBaseConnection,
            KnowledgeBaseConnection.id == KnowledgeBaseCodeLink.connection_id,
        )
        .where(KnowledgeBaseConnection.knowledge_base_id == knowledge_base_id)
    )
    if connection_id is not None:
        query = query.where(KnowledgeBaseConnection.id == connection_id)
    if repository_id is not None:
        query = query.where(
            (KnowledgeBaseConnection.source_repository_id == repository_id)
            | (KnowledgeBaseConnection.target_repository_id == repository_id)
        )
    return bool(await session.scalar(query))


async def knowledge_bases_linking(
    session: AsyncSession, repository_id: str
) -> list[str]:
    """:return: The knowledge bases with code links to or from a repository."""
    return list(
        await session.scalars(
            select(KnowledgeBaseConnection.knowledge_base_id)
            .join(
                KnowledgeBaseCodeLink,
                KnowledgeBaseCodeLink.connection_id == KnowledgeBaseConnection.id,
            )
            .where(
                (KnowledgeBaseConnection.source_repository_id == repository_id)
                | (KnowledgeBaseConnection.target_repository_id == repository_id)
            )
            .distinct()
        )
    )


async def sync_code_links(
    session: AsyncSession, codegraph: CodeGraph | None, knowledge_base_id: str
) -> None:
    """
    Replace the knowledge base's cross-repository links in the code graph
    with its code links as the session sees them (flush first). Code links
    whose repository has no graph any more are left out until it has one.

    :raises WorkerError: The worker refused the set, or can't be reached.
    """
    if codegraph is None:
        # Nothing reached a graph without a worker.
        return
    rows = (
        await session.execute(
            select(KnowledgeBaseCodeLink, KnowledgeBaseConnection)
            .join(
                KnowledgeBaseConnection,
                KnowledgeBaseConnection.id == KnowledgeBaseCodeLink.connection_id,
            )
            .where(KnowledgeBaseConnection.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeBaseCodeLink.id)
        )
    ).all()
    graphs = await graph_ids(
        session,
        list(
            {
                repository
                for _, connection in rows
                for repository in (
                    connection.source_repository_id,
                    connection.target_repository_id,
                )
            }
        ),
    )
    owner = owner_of(knowledge_base_id)
    links: list[dict[str, Any]] = []
    for code, connection in rows:
        source = graphs.get(connection.source_repository_id)
        target = graphs.get(connection.target_repository_id)
        if source is None or target is None or source == target:
            continue
        links.append(
            {
                "id": code.id,
                "owner": owner,
                "kind": CROSS_KINDS[connection.kind],
                "source": {
                    "repository_id": source,
                    "node_id": code.source_node_id,
                    "qualified_name": code.source_qualified_name,
                    "kind": code.source_kind,
                },
                "target": {
                    "repository_id": target,
                    "node_id": code.target_node_id,
                    "qualified_name": code.target_qualified_name,
                    "kind": code.target_kind,
                },
                "label": code.label,
                "provenance": "manual",
                "created_by": code.created_by,
            }
        )
    await codegraph.put_cross_links(owner, links)


def _outdated(error: WorkerError) -> HTTPException | None:
    """A worker from before cross-repository links answers their route with
    its router's plain-text 404: say so rather than blame the request."""
    if error.status == status.HTTP_404_NOT_FOUND and error.code == "invalid_response":
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The code graph worker doesn't know cross-repository links yet: "
            "restart it on the current version",
        )
    return None


def refused(error: WorkerError) -> HTTPException:
    """The HTTP error for a cross-link call the worker refused."""
    return _outdated(error) or unavailable(error)


async def commit_synced(
    session: AsyncSession,
    codegraph: CodeGraph | None,
    knowledge_base_ids: Sequence[str],
) -> None:
    """
    Flush, write the knowledge bases' code links into the code graph, then
    commit; when the worker refuses or can't be reached, nothing is
    committed.

    :raises HTTPException: The worker's refusal (:func:`refused`).
    """
    await session.flush()
    try:
        for knowledge_base_id in knowledge_base_ids:
            await sync_code_links(session, codegraph, knowledge_base_id)
    except WorkerError as error:
        await session.rollback()
        raise refused(error) from None
    await session.commit()


async def connections_of(
    session: AsyncSession, knowledge_base_ids: Sequence[str]
) -> dict[str, list[dict[str, str]]]:
    """:return: Each knowledge base's connections, ``{source, kind, target,
    description}`` with the ends as ``owner/name``, oldest first, by
    knowledge base; for its agents (``forge_codegraph.system_summary``)."""
    found: dict[str, list[dict[str, str]]] = {i: [] for i in knowledge_base_ids}
    if not knowledge_base_ids:
        return found
    connections = list(
        await session.scalars(
            select(KnowledgeBaseConnection)
            .where(KnowledgeBaseConnection.knowledge_base_id.in_(list(found)))
            .order_by(KnowledgeBaseConnection.created_at, KnowledgeBaseConnection.id)
        )
    )
    ids = {
        r for c in connections for r in (c.source_repository_id, c.target_repository_id)
    }
    names = {
        repository.id: f"{repository.owner}/{repository.name}"
        for repository in await session.scalars(
            select(CodeRepository).where(CodeRepository.id.in_(ids))
        )
    }
    for connection in connections:
        found[connection.knowledge_base_id].append(
            {
                "source": names.get(connection.source_repository_id, "?"),
                "kind": connection.kind,
                "target": names.get(connection.target_repository_id, "?"),
                "description": connection.description,
            }
        )
    return found


def _end(row: KnowledgeBaseCodeLink, side: str) -> dict[str, str]:
    return {
        "node_id": getattr(row, f"{side}_node_id"),
        "kind": getattr(row, f"{side}_kind"),
        "name": getattr(row, f"{side}_name"),
        "qualified_name": getattr(row, f"{side}_qualified_name"),
        "path": getattr(row, f"{side}_path"),
    }


async def organization_map(
    session: AsyncSession, organization_id: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    What an organization's system design knowledge bases say about how its
    repositories connect, for the code graph's MCP server.

    :return: The owners of their cross-repository links in the graph
        (``kb:<id>``, one per system design knowledge base), and every
        connection on their maps, oldest first: ``{id, knowledge_base_id,
        knowledge_base, kind, description, source, target, code_links}``,
        the ends ``{url, owner, name}``, each code link ``{source, target,
        label}`` with node ends ``{node_id, kind, name, qualified_name,
        path}``.
    """
    bases = {
        kb.id: kb
        for kb in await session.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.organization_id == organization_id,
                KnowledgeBase.kind == SYSTEM,
            )
        )
    }
    owners = sorted(owner_of(i) for i in bases)
    if not bases:
        return owners, []
    connections = list(
        await session.scalars(
            select(KnowledgeBaseConnection)
            .where(KnowledgeBaseConnection.knowledge_base_id.in_(list(bases)))
            .order_by(KnowledgeBaseConnection.created_at, KnowledgeBaseConnection.id)
        )
    )
    if not connections:
        return owners, []
    repositories = {
        r.id: r
        for r in await session.scalars(
            select(CodeRepository).where(
                CodeRepository.id.in_(
                    {
                        i
                        for c in connections
                        for i in (c.source_repository_id, c.target_repository_id)
                    }
                )
            )
        )
    }
    links: dict[str, list[dict[str, Any]]] = {}
    for row in await session.scalars(
        select(KnowledgeBaseCodeLink)
        .where(KnowledgeBaseCodeLink.connection_id.in_([c.id for c in connections]))
        .order_by(KnowledgeBaseCodeLink.created_at, KnowledgeBaseCodeLink.id)
    ):
        links.setdefault(row.connection_id, []).append(
            {
                "source": _end(row, "source"),
                "target": _end(row, "target"),
                "label": row.label,
            }
        )

    def end(repository_id: str) -> dict[str, str]:
        r = repositories[repository_id]
        return {"url": r.url, "owner": r.owner, "name": r.name}

    return owners, [
        {
            "id": c.id,
            "knowledge_base_id": c.knowledge_base_id,
            "knowledge_base": bases[c.knowledge_base_id].name,
            "kind": c.kind,
            "description": c.description,
            "source": end(c.source_repository_id),
            "target": end(c.target_repository_id),
            "code_links": links.get(c.id, []),
        }
        for c in connections
        if c.source_repository_id in repositories
        and c.target_repository_id in repositories
    ]
