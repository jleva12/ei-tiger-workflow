"""
A system design knowledge base's system map: where its applications are on
it (``…/map``, the same for everyone who opens it), and its connections, the
edges of the map: how one of its applications connects to another (calling
its API, depending on it, sending it events or sharing its data), and their
code links, where in each application's code graph it happens
(``forge_admin.knowledge.connections``).

Reading needs ``organizations:read`` in the organization; drawing, removing
and moving applications need ``knowledge_bases:manage``. Code links are written into the
code graph, so adding or removing one needs the worker.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from forge_codegraph import CodeGraph, WorkerError
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import Audited, NodeId, Session, as_read
from forge_admin.auth.access import CurrentUser, Enforcer
from forge_admin.knowledge.access import knowledge_base_of
from forge_admin.knowledge.connections import commit_synced, has_code_links, refused
from forge_admin.knowledge.graph import codegraph_of, graph_ids
from forge_admin.models import (
    CodeRepository,
    KnowledgeBase,
    KnowledgeBaseCodeLink,
    KnowledgeBaseConnection,
    KnowledgeBaseRepository,
)
from forge_admin.models.knowledge import SYSTEM, ConnectionKind, MapLayout

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge bases"])

BASE = "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}"
PATH = f"{BASE}/connections"
NO_CONNECTION = "The knowledge base has no such connection"
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
NodeRef = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ConnectionRead(Audited):
    id: str
    knowledge_base_id: str
    # The application it's from, e.g. the caller.
    source_repository_id: str
    # The application it's to, e.g. the one called.
    target_repository_id: str
    kind: ConnectionKind
    description: str
    # How many code links say where it happens.
    code_links: int = 0


class ConnectionCreate(BaseModel):
    source_repository_id: str = Field(max_length=36)
    target_repository_id: str = Field(max_length=36)
    kind: ConnectionKind = "connects_to"
    description: Description = ""


class ConnectionUpdate(BaseModel):
    kind: ConnectionKind | None = None
    description: Description | None = None


class CodeEnd(BaseModel):
    # The node in the application's code graph.
    node_id: str
    kind: str
    name: str
    qualified_name: str
    # Its file, as the graph has it.
    path: str


class CodeLinkRead(Audited):
    id: str
    connection_id: str
    # In the connection's source application, e.g. the client method.
    source: CodeEnd
    # In its target, e.g. the handler.
    target: CodeEnd
    label: str


class CodeLinkCreate(BaseModel):
    source_node_id: NodeRef
    target_node_id: NodeRef
    label: Label = ""


def _code_link_read(row: KnowledgeBaseCodeLink) -> CodeLinkRead:
    def end(side: str) -> CodeEnd:
        return CodeEnd(
            node_id=getattr(row, f"{side}_node_id"),
            kind=getattr(row, f"{side}_kind"),
            name=getattr(row, f"{side}_name"),
            qualified_name=getattr(row, f"{side}_qualified_name"),
            path=getattr(row, f"{side}_path"),
        )

    return as_read(CodeLinkRead, row, source=end("source"), target=end("target"))


async def _connection(
    session: AsyncSession, knowledge_base_id: str, connection_id: str
) -> KnowledgeBaseConnection:
    """:raises HTTPException: 404 for another knowledge base's connection."""
    found = await session.get(KnowledgeBaseConnection, connection_id)
    if found is None or found.knowledge_base_id != knowledge_base_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_CONNECTION)
    return found


def _graph_of(request: Request) -> CodeGraph | None:
    return getattr(request.app.state, "codegraph", None)


@router.get(PATH)
async def list_connections(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[ConnectionRead]:
    """
    The connections between the knowledge base's applications, oldest
    first, each with how many code links say where it happens.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    counts = (
        select(KnowledgeBaseCodeLink.connection_id, func.count().label("code_links"))
        .group_by(KnowledgeBaseCodeLink.connection_id)
        .subquery()
    )
    rows = await session.execute(
        select(KnowledgeBaseConnection, counts.c.code_links)
        .outerjoin(counts, counts.c.connection_id == KnowledgeBaseConnection.id)
        .where(KnowledgeBaseConnection.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeBaseConnection.created_at, KnowledgeBaseConnection.id)
    )
    return [as_read(ConnectionRead, row, code_links=count or 0) for row, count in rows]


@router.post(PATH, status_code=status.HTTP_201_CREATED)
async def add_connection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: ConnectionCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ConnectionRead:
    """
    Connect one of the knowledge base's applications to another by hand.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another organization's knowledge base, or none; 409 when they're
        already connected that way, or for a RAG knowledge base; 422 for an
        application connected to itself, or one the knowledge base doesn't
        include.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    if body.source_repository_id == body.target_repository_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "An application can't connect to itself",
        )
    included = set(
        await session.scalars(
            select(KnowledgeBaseRepository.repository_id).where(
                KnowledgeBaseRepository.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseRepository.repository_id.in_(
                    [body.source_repository_id, body.target_repository_id]
                ),
            )
        )
    )
    if len(included) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Both must be the knowledge base's applications",
        )
    row = KnowledgeBaseConnection(
        knowledge_base_id=knowledge_base_id,
        source_repository_id=body.source_repository_id,
        target_repository_id=body.target_repository_id,
        kind=body.kind,
        description=body.description,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "They're already connected that way"
        ) from None
    logger.info(
        "%s connected %s to %s (%s) in knowledge base %s",
        user,
        body.source_repository_id,
        body.target_repository_id,
        body.kind,
        knowledge_base_id,
    )
    return as_read(ConnectionRead, row, code_links=0)


@router.patch(f"{PATH}/{{connection_id}}")
async def update_connection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    connection_id: NodeId,
    body: ConnectionUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ConnectionRead:
    """
    Change how two applications connect, or the note on it. A new kind
    changes its code links' kind in the code graph too.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's connection; 409 when they're already
        connected that way; 503 when its code links can't be updated.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _connection(session, knowledge_base_id, connection_id)
    coded = (
        body.kind is not None
        and body.kind != row.kind
        and await has_code_links(
            session, knowledge_base_id, connection_id=connection_id
        )
    )
    if body.kind is not None:
        row.kind = body.kind
    if body.description is not None:
        row.description = body.description
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "They're already connected that way"
        ) from None
    await commit_synced(
        session, _graph_of(request), [knowledge_base_id] if coded else []
    )
    count = await session.scalar(
        select(func.count())
        .select_from(KnowledgeBaseCodeLink)
        .where(KnowledgeBaseCodeLink.connection_id == connection_id)
    )
    return as_read(ConnectionRead, row, code_links=count or 0)


@router.delete(f"{PATH}/{{connection_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_connection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    connection_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a connection, with its code links, which leave the code graph
    with it. Nothing changes in either application.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's connection; 503 when its code links can't
        be taken out of the code graph.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    row = await _connection(session, knowledge_base_id, connection_id)
    coded = await has_code_links(
        session, knowledge_base_id, connection_id=connection_id
    )
    await session.delete(row)
    await commit_synced(
        session, _graph_of(request), [knowledge_base_id] if coded else []
    )
    logger.info(
        "%s removed connection %s from knowledge base %s",
        user,
        connection_id,
        knowledge_base_id,
    )


@router.get(f"{PATH}/{{connection_id}}/code-links")
async def list_code_links(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    connection_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[CodeLinkRead]:
    """
    Where a connection happens in the code: a declaration in each
    application's code graph, oldest first.
    \f
    :raises HTTPException: 404 for another knowledge base's connection.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    await _connection(session, knowledge_base_id, connection_id)
    rows = await session.scalars(
        select(KnowledgeBaseCodeLink)
        .where(KnowledgeBaseCodeLink.connection_id == connection_id)
        .order_by(KnowledgeBaseCodeLink.created_at, KnowledgeBaseCodeLink.id)
    )
    return [_code_link_read(row) for row in rows]


async def _node(
    codegraph: CodeGraph, repository: CodeRepository, graph_id: str, node_id: str
) -> dict[str, Any]:
    """
    A node of the application's live code graph, as the worker has it.

    :raises HTTPException: 422 when the graph has no such node.
    """
    missing = HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"{repository.owner}/{repository.name}'s code graph has no node {node_id}",
    )
    try:
        version = await codegraph.read(graph_id, "node", {"node": node_id})
    except WorkerError as error:
        if error.status in (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST):
            if error.code == "invalid_response":
                raise refused(error) from None
            raise missing from None
        raise refused(error) from None
    node = (version.get("fact") or {}).get("node")
    if not isinstance(node, dict) or version.get("gen_to"):
        raise missing
    return node


def _end_into(row: KnowledgeBaseCodeLink, side: str, node: dict[str, Any]) -> None:
    path = (node.get("properties") or {}).get("file_path") or {}
    setattr(row, f"{side}_node_id", str(node["id"]))
    setattr(row, f"{side}_kind", str(node.get("kind", ""))[:64])
    setattr(row, f"{side}_name", str(node.get("name", ""))[:255])
    setattr(row, f"{side}_qualified_name", str(node.get("qualified_name", "")))
    setattr(row, f"{side}_path", str(path.get("string", ""))[:1000])


@router.post(
    f"{PATH}/{{connection_id}}/code-links", status_code=status.HTTP_201_CREATED
)
async def add_code_link(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    connection_id: NodeId,
    body: CodeLinkCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CodeLinkRead:
    """
    Say where a connection happens in the code: a node in the source
    application's code graph (e.g. the method that calls the API) and one in
    the target's (the handler that serves it). Both are read from their live
    graphs, and the link is written into the code graph, where queries
    follow it from one application into the other.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another knowledge base's connection; 409 when either application
        isn't in the code graph yet, or the nodes are linked already; 422
        for a node its graph doesn't have, or two applications with one
        graph; 503 without the worker.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    connection = await _connection(session, knowledge_base_id, connection_id)
    codegraph = codegraph_of(request)
    source = await session.get(CodeRepository, connection.source_repository_id)
    target = await session.get(CodeRepository, connection.target_repository_id)
    assert source is not None and target is not None  # the foreign keys
    graphs = await graph_ids(session, [source.id, target.id])
    for repository in (source, target):
        if repository.id not in graphs:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{repository.owner}/{repository.name} isn't in the code graph "
                "yet: ingest it first",
            )
    if graphs[source.id] == graphs[target.id]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Both applications are the same code graph",
        )
    row = KnowledgeBaseCodeLink(connection_id=connection.id, label=body.label)
    _end_into(
        row,
        "source",
        await _node(codegraph, source, graphs[source.id], body.source_node_id),
    )
    _end_into(
        row,
        "target",
        await _node(codegraph, target, graphs[target.id], body.target_node_id),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Those two are already linked"
        ) from None
    await commit_synced(session, codegraph, [knowledge_base_id])
    logger.info(
        "%s linked %s in %s to %s in %s for knowledge base %s",
        user,
        row.source_node_id,
        source.url,
        row.target_node_id,
        target.url,
        knowledge_base_id,
    )
    return _code_link_read(row)


@router.delete(
    f"{PATH}/{{connection_id}}/code-links/{{code_link_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_code_link(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    connection_id: NodeId,
    code_link_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a code link, and take it out of the code graph.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another connection's code link; 503 when the code graph can't be
        updated.
    """
    await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    await _connection(session, knowledge_base_id, connection_id)
    row = await session.get(KnowledgeBaseCodeLink, code_link_id)
    if row is None or row.connection_id != connection_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The connection has no such code link"
        )
    await session.delete(row)
    await commit_synced(session, _graph_of(request), [knowledge_base_id])


#: How far from the map's origin a place may be, either way.
MAP_EXTENT = 1_000_000.0
Coordinate = Annotated[float, Field(ge=-MAP_EXTENT, le=MAP_EXTENT, allow_inf_nan=False)]


class MapPoint(BaseModel):
    x: Coordinate
    y: Coordinate


class SystemMapRead(BaseModel):
    # The layout last picked; None until someone arranges the map.
    layout: MapLayout | None
    # Each placed application's place, by its repository ID; one not yet
    # placed is missing.
    positions: dict[str, MapPoint]


class SystemMapUpdate(BaseModel):
    # A layout picked; left as it is when absent.
    layout: MapLayout | None = None
    # Places for some of its applications, by repository ID; the others keep
    # theirs. One the knowledge base doesn't include (removed meanwhile) is
    # ignored.
    positions: dict[Annotated[str, StringConstraints(max_length=36)], MapPoint] = Field(
        default_factory=dict, max_length=1000
    )


async def _system_map(
    session: AsyncSession, knowledge_base: KnowledgeBase
) -> SystemMapRead:
    rows = await session.scalars(
        select(KnowledgeBaseRepository).where(
            KnowledgeBaseRepository.knowledge_base_id == knowledge_base.id,
            KnowledgeBaseRepository.map_x.is_not(None),
            KnowledgeBaseRepository.map_y.is_not(None),
        )
    )
    return SystemMapRead(
        layout=knowledge_base.map_layout,  # type: ignore[arg-type]
        positions={
            row.repository_id: MapPoint(x=row.map_x or 0.0, y=row.map_y or 0.0)
            for row in rows
        },
    )


@router.get(f"{BASE}/map")
async def get_system_map(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SystemMapRead:
    """
    The knowledge base's system map as people left it: each placed
    application's place, and the layout last picked. Everyone sees the same.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    knowledge_base = await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    return await _system_map(session, knowledge_base)


@router.patch(f"{BASE}/map")
async def update_system_map(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: SystemMapUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SystemMapRead:
    """
    Move some of the knowledge base's applications on its system map (one
    dragged, or all of them arranged again), or pick its layout. Only the
    applications given move, so two people moving different ones don't undo
    each other.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    knowledge_base = await knowledge_base_of(
        session,
        enforcer,
        user,
        organization_id,
        knowledge_base_id,
        manage=True,
        kind=SYSTEM,
    )
    if body.layout is not None:
        knowledge_base.map_layout = body.layout
    if body.positions:
        rows = await session.scalars(
            select(KnowledgeBaseRepository).where(
                KnowledgeBaseRepository.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseRepository.repository_id.in_(list(body.positions)),
            )
        )
        for row in rows:
            point = body.positions[row.repository_id]
            row.map_x, row.map_y = point.x, point.y
    await session.commit()
    return await _system_map(session, knowledge_base)
