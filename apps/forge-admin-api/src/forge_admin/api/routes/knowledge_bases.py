"""
An organization's knowledge bases: named sets of documents its chat agents
search with a knowledge base tool (``forge_admin.knowledge``). Their
documents and collections are ``knowledge_documents`` and
``knowledge_collections``.

Reading needs ``organizations:read`` in the organization; creating, changing
and deleting need ``knowledge_bases:manage`` (its admins and members have it
by default). Searching one answers the passages its agents would find.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.knowledge.access import MANAGE, READ, knowledge_base_of
from forge_admin.knowledge.queue import KnowledgeQueue, QueueError
from forge_admin.knowledge.search import MAX_RESULTS, KnowledgeSearch, SearchError
from forge_admin.knowledge.storage import DocumentStore, StorageError
from forge_admin.models import KnowledgeBase, KnowledgeDocument

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge bases"])

NAME_TAKEN = "The organization already has a knowledge base by that name"
Query = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class KnowledgeBaseCreate(BaseModel):
    name: Name
    description: str = Field(default="", max_length=4000)


class KnowledgeBaseUpdate(BaseModel):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=4000)


class KnowledgeBaseRead(Audited):
    id: str
    organization_id: str
    name: str
    description: str
    # Its documents: how many, how many are searchable (SUCCEEDED) and how
    # many FAILED, their chunks and their bytes.
    documents: int = 0
    ready: int = 0
    failed: int = 0
    chunks: int = 0
    size_bytes: int = 0


class SearchRequest(BaseModel):
    query: Query
    limit: int = Field(default=8, ge=1, le=MAX_RESULTS)


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    # The document's name as uploaded; its title when it's gone since.
    filename: str
    # The headings the passage is under, outermost first.
    section_path: list[str]
    text: str
    # How well it matched; higher is better.
    score: float


class SearchResults(BaseModel):
    hits: list[SearchHit]


# The counts of a knowledge base without documents.
NO_DOCUMENTS = {"documents": 0, "ready": 0, "failed": 0, "chunks": 0, "size_bytes": 0}


async def _counts(
    session: AsyncSession, knowledge_base_ids: list[str]
) -> dict[str, dict[str, int]]:
    """:return: Each knowledge base's document counts, by its ID."""
    if not knowledge_base_ids:
        return {}
    rows = await session.execute(
        select(
            KnowledgeDocument.knowledge_base_id,
            func.count(KnowledgeDocument.id),
            func.sum(case((KnowledgeDocument.phase == "SUCCEEDED", 1), else_=0)),
            func.sum(case((KnowledgeDocument.phase == "FAILED", 1), else_=0)),
            func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0),
            func.coalesce(func.sum(KnowledgeDocument.size_bytes), 0),
        )
        .where(KnowledgeDocument.knowledge_base_id.in_(knowledge_base_ids))
        .group_by(KnowledgeDocument.knowledge_base_id)
    )
    return {
        kb_id: {
            "documents": int(documents),
            "ready": int(ready or 0),
            "failed": int(failed or 0),
            "chunks": int(chunks or 0),
            "size_bytes": int(size or 0),
        }
        for kb_id, documents, ready, failed, chunks, size in rows
    }


async def _read(
    session: AsyncSession, knowledge_base: KnowledgeBase
) -> KnowledgeBaseRead:
    counts = await _counts(session, [knowledge_base.id])
    return as_read(
        KnowledgeBaseRead, knowledge_base, **counts.get(knowledge_base.id, NO_DOCUMENTS)
    )


@router.get("/organizations/{organization_id}/knowledge-bases")
async def list_knowledge_bases(
    organization_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[KnowledgeBaseRead]:
    """
    List an organization's knowledge bases, by name, with their documents'
    counts.
    \f
    :raises HTTPException: 403 without organizations:read in the organization.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    found = list(
        await session.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.organization_id == organization_id)
            .order_by(KnowledgeBase.name)
        )
    )
    counts = await _counts(session, [kb.id for kb in found])
    return [
        as_read(KnowledgeBaseRead, kb, **counts.get(kb.id, NO_DOCUMENTS))
        for kb in found
    ]


@router.post(
    "/organizations/{organization_id}/knowledge-bases",
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    organization_id: NodeId,
    body: KnowledgeBaseCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> KnowledgeBaseRead:
    """
    Add a knowledge base to the organization, empty.
    \f
    :raises HTTPException: 403 without knowledge_bases:manage in the
        organization; 409 when the name is taken.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    knowledge_base = KnowledgeBase(
        organization_id=organization_id, name=body.name, description=body.description
    )
    session.add(knowledge_base)
    await commit_or_conflict(session, NAME_TAKEN)
    logger.info(
        "%s created knowledge base %s (%s) in organization %s",
        user,
        knowledge_base.name,
        knowledge_base.id,
        organization_id,
    )
    return await _read(session, knowledge_base)


@router.get("/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> KnowledgeBaseRead:
    """
    Read one knowledge base, with its documents' counts.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        404 for another organization's knowledge base, or none.
    """
    knowledge_base = await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id
    )
    return await _read(session, knowledge_base)


@router.patch("/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}")
async def update_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: KnowledgeBaseUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> KnowledgeBaseRead:
    """
    Rename a knowledge base or change its description. Agents that use it
    keep using it: they name it by ID.
    \f
    :raises HTTPException: 403 without knowledge_bases:manage in the
        organization; 404 for another organization's knowledge base, or none;
        409 when the name is taken.
    """
    knowledge_base = await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    if body.name is not None:
        knowledge_base.name = body.name
    if body.description is not None:
        knowledge_base.description = body.description
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, knowledge_base)


@router.delete(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete a knowledge base with everything in it: each document's file from
    the bucket and its chunks from what the agents search (a delete job for
    the async worker), then its records and collections. Each step is safe to
    repeat, so a deletion that fails part way leaves the knowledge base with
    the documents not yet removed, and deleting it again finishes the job.
    Agents that use it no longer find anything in it.
    \f
    :raises HTTPException: 403 without knowledge_bases:manage in the
        organization; 404 for another organization's knowledge base, or none;
        502 when storage refuses; 503 when uploads aren't set up, or storage
        or the worker is unavailable.
    """
    knowledge_base = await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    documents = list(
        await session.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == knowledge_base_id
            )
        )
    )
    if documents:
        store: DocumentStore | None = request.app.state.documents
        queue: KnowledgeQueue | None = request.app.state.knowledge_queue
        if store is None or queue is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Document uploads aren't set up: set FORGE_ADMIN_DOCUMENTS_BUCKET "
                "and FORGE_ADMIN_EMBEDDING_REDIS_URL",
            )
        for document in documents:
            await _remove(session, store, queue, document)
    name = knowledge_base.name
    await session.delete(knowledge_base)
    await session.commit()
    logger.info(
        "%s deleted knowledge base %s (%s) with %d documents",
        user,
        name,
        knowledge_base_id,
        len(documents),
    )


async def _remove(
    session: AsyncSession,
    store: DocumentStore,
    queue: KnowledgeQueue,
    document: KnowledgeDocument,
) -> None:
    """Remove one document's file and chunks, then its record (committed, so
    a deletion that fails later doesn't do it again)."""
    key = store.key_of(document.storage_uri)
    try:
        if key is not None:
            await store.delete(key)
    except StorageError as error:
        if error.code == "unreachable":
            logger.warning("Document storage unavailable: %s", error)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Document storage is unavailable; try again",
            ) from None
        logger.error("Document storage refused a removal: %s", error)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Document storage refused the removal"
        ) from None
    try:
        await queue.delete_document(
            knowledge_base_id=document.knowledge_base_id, document_id=document.id
        )
    except QueueError as error:
        logger.warning("The async worker's queue is unavailable: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker is unavailable; try again",
        ) from None
    await session.delete(document)
    await session.commit()


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/search"
)
async def search_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: SearchRequest,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SearchResults:
    """
    Search a knowledge base as its agents do: the passages of its documents
    that best match the query, by meaning and by its words (hybrid BM25 and
    vector search), best first. Only documents that finished ingesting are
    found.
    \f
    :raises HTTPException: 403 without organizations:read in the
        organization; 404 for another organization's knowledge base, or none;
        503 when searching isn't set up, or MongoDB or the embedding model is
        unavailable.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    search: KnowledgeSearch | None = request.app.state.knowledge_search
    if search is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Knowledge base search isn't set up: set FORGE_ADMIN_MONGO_URI",
        )
    try:
        passages = await search.search(
            [knowledge_base_id], body.query, limit=body.limit
        )
    except SearchError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Knowledge base search is unavailable; try again",
        ) from None
    names = {
        document_id: filename
        for document_id, filename in (
            await session.execute(
                select(KnowledgeDocument.id, KnowledgeDocument.filename).where(
                    KnowledgeDocument.id.in_({p.document_id for p in passages})
                )
            )
        )
    }
    return SearchResults(
        hits=[
            SearchHit(
                chunk_id=passage.chunk_id,
                document_id=passage.document_id,
                filename=names.get(passage.document_id, passage.title),
                section_path=passage.section_path,
                text=passage.text,
                score=passage.score,
            )
            for passage in passages
        ]
    )
