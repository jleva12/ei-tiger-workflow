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
from sqlalchemy import ColumnElement, case, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.api.routes.knowledge_documents import ACTIVE, refresh
from forge_admin.auth.access import (
    UUID_PATTERN,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.knowledge.access import MANAGE, NOT_FOUND, READ, knowledge_base_of
from forge_admin.knowledge.queue import KnowledgeQueue, QueueError
from forge_admin.knowledge.search import (
    MAX_RESULTS,
    KnowledgeSearch,
    Passage,
    SearchError,
)
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
    # Its searchable documents embedded with another model than searches
    # embed questions with (or before models were recorded): found by their
    # words alone, not their meaning, until they're re-indexed (.../reindex).
    stale: int = 0
    # What searches embed questions with; None when search isn't set up.
    embedding_model: str | None = None


class SearchRequest(BaseModel):
    query: Query
    limit: int = Field(default=8, ge=1, le=MAX_RESULTS)


class SearchHit(BaseModel):
    chunk_id: str
    # What an answer cites it by, in brackets ("[KQM4821]"): the same in
    # every search.
    ref: str
    document_id: str
    # The document's name as uploaded; its title when it's gone since.
    filename: str
    # The headings the passage is under, outermost first.
    section_path: list[str]
    # Where in the document it is, in words ("page 4"); empty when unknown.
    location: str = ""
    text: str
    # How relevant it is: the reranker's score, or the similarity of its
    # meaning to the query's (-1 to 1); higher is better. Hits are in their
    # ranked order, which also weighs their words.
    score: float


class SearchResults(BaseModel):
    hits: list[SearchHit]


# The most knowledge bases one search names.
MAX_SEARCHED = 50
KnowledgeBaseId = Annotated[str, StringConstraints(pattern=rf"^{UUID_PATTERN}$")]


class OrganizationSearchRequest(BaseModel):
    query: Query
    # The knowledge bases searched, ranked together; every one of the
    # organization's when left out.
    knowledge_base_ids: list[KnowledgeBaseId] | None = Field(
        default=None, min_length=1, max_length=MAX_SEARCHED
    )
    limit: int = Field(default=8, ge=1, le=MAX_RESULTS)


class SearchedKnowledgeBase(BaseModel):
    id: str
    name: str


class OrganizationSearchHit(SearchHit):
    # The knowledge base it's in.
    knowledge_base_id: str
    knowledge_base: str


class OrganizationSearchResults(BaseModel):
    # What was searched, in order.
    searched: list[SearchedKnowledgeBase]
    hits: list[OrganizationSearchHit]


class ReindexRequest(BaseModel):
    # Only the stale documents (see KnowledgeBaseRead.stale); false
    # re-indexes every finished one, e.g. after the parsers or chunking changed.
    stale_only: bool = True


class ReindexResult(BaseModel):
    # The documents submitted to be ingested again.
    submitted: int


# The counts of a knowledge base without documents.
NO_DOCUMENTS = {
    "documents": 0,
    "ready": 0,
    "failed": 0,
    "chunks": 0,
    "size_bytes": 0,
    "stale": 0,
}


def _search_model(request: Request) -> str | None:
    """:return: What searches embed questions with; None when search isn't set up."""
    search: KnowledgeSearch | None = request.app.state.knowledge_search
    return search.model_id if search is not None else None


def _stale(model: str | None) -> ColumnElement[bool]:
    """Searchable, but embedded with another model than ``model`` (or one
    not recorded); nothing is stale without a model to compare with."""
    if model is None:
        return false()
    return (KnowledgeDocument.phase == "SUCCEEDED") & (
        KnowledgeDocument.embedding_model != model
    )


async def _counts(
    session: AsyncSession, knowledge_base_ids: list[str], model: str | None
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
            func.sum(case((_stale(model), 1), else_=0)),
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
            "stale": int(stale or 0),
        }
        for kb_id, documents, ready, failed, chunks, size, stale in rows
    }


async def _read(
    session: AsyncSession, knowledge_base: KnowledgeBase, model: str | None
) -> KnowledgeBaseRead:
    counts = await _counts(session, [knowledge_base.id], model)
    return as_read(
        KnowledgeBaseRead,
        knowledge_base,
        **counts.get(knowledge_base.id, NO_DOCUMENTS),
        embedding_model=model,
    )


@router.get("/organizations/{organization_id}/knowledge-bases")
async def list_knowledge_bases(
    organization_id: NodeId,
    request: Request,
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
    model = _search_model(request)
    counts = await _counts(session, [kb.id for kb in found], model)
    return [
        as_read(
            KnowledgeBaseRead,
            kb,
            **counts.get(kb.id, NO_DOCUMENTS),
            embedding_model=model,
        )
        for kb in found
    ]


@router.post(
    "/organizations/{organization_id}/knowledge-bases",
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    organization_id: NodeId,
    body: KnowledgeBaseCreate,
    request: Request,
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
    return await _read(session, knowledge_base, _search_model(request))


@router.get("/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}")
async def get_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    request: Request,
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
    return await _read(session, knowledge_base, _search_model(request))


@router.patch("/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}")
async def update_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: KnowledgeBaseUpdate,
    request: Request,
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
    return await _read(session, knowledge_base, _search_model(request))


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
    names = await _filenames(session, passages)
    return SearchResults(
        hits=[
            SearchHit(
                chunk_id=passage.chunk_id,
                ref=passage.ref,
                document_id=passage.document_id,
                filename=names.get(passage.document_id, passage.document),
                section_path=passage.section_path,
                location=passage.location,
                text=passage.text,
                score=passage.score,
            )
            for passage in passages
        ]
    )


@router.post("/organizations/{organization_id}/knowledge-bases/search")
async def search_knowledge_bases(
    organization_id: NodeId,
    body: OrganizationSearchRequest,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> OrganizationSearchResults:
    """
    Search some of an organization's knowledge bases, or all of them, as
    one: the passages of their documents that best match the query, ranked
    together, best first, each with the knowledge base it's from. Passages
    of documents removed since they were indexed are left out.
    \f
    :raises HTTPException: 403 without organizations:read in the
        organization; 404 for a knowledge base it doesn't have; 503 when
        searching isn't set up, or the store or the embedding model is
        unavailable.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    query = select(KnowledgeBase).where(
        KnowledgeBase.organization_id == organization_id
    )
    wanted = list(dict.fromkeys(body.knowledge_base_ids or []))
    if wanted:
        query = query.where(KnowledgeBase.id.in_(wanted))
    found = {
        kb.id: kb for kb in await session.scalars(query.order_by(KnowledgeBase.name))
    }
    if missing := [i for i in wanted if i not in found]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{NOT_FOUND}: {missing[0]}")
    searched = [found[i] for i in wanted] if wanted else list(found.values())
    if not searched:
        return OrganizationSearchResults(searched=[], hits=[])
    search: KnowledgeSearch | None = request.app.state.knowledge_search
    if search is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Knowledge base search isn't set up: set FORGE_ADMIN_MONGO_URI",
        )
    try:
        passages = await search.search(
            [kb.id for kb in searched], body.query, limit=body.limit
        )
    except SearchError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Knowledge base search is unavailable; try again",
        ) from None
    names = await _filenames(session, passages)
    return OrganizationSearchResults(
        searched=[SearchedKnowledgeBase(id=kb.id, name=kb.name) for kb in searched],
        hits=[
            OrganizationSearchHit(
                chunk_id=passage.chunk_id,
                ref=passage.ref,
                knowledge_base_id=passage.knowledge_base_id,
                knowledge_base=found[passage.knowledge_base_id].name,
                document_id=passage.document_id,
                filename=names[passage.document_id],
                section_path=passage.section_path,
                location=passage.location,
                text=passage.text,
                score=passage.score,
            )
            for passage in passages
            if passage.document_id in names and passage.knowledge_base_id in found
        ],
    )


async def _filenames(session: AsyncSession, passages: list[Passage]) -> dict[str, str]:
    """:return: The passages' documents' names as uploaded, by ID; one removed since is missing."""
    rows = await session.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.filename).where(
            KnowledgeDocument.id.in_({p.document_id for p in passages})
        )
    )
    return {document_id: filename for document_id, filename in rows}


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_knowledge_base(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    body: ReindexRequest | None = None,
) -> ReindexResult:
    """
    Ingest a knowledge base's documents again, as the worker would ingest
    them now: with the embedding model searches use, and its current parsers
    and chunking. By default only its stale documents (embedded with another
    model): after the model changes, until they're re-indexed, they're found
    by their words alone. Each stays searchable, as it was, until its new
    chunks replace the old; one already being ingested is left to that.
    Embeddings of text that didn't change are reused, so re-indexing with
    the same model costs no embedding calls.
    \f
    :raises HTTPException: 403 without knowledge_bases:manage in the
        organization; 404 for another organization's knowledge base, or none;
        503 when uploads or search aren't set up (stale_only needs search's
        model), or the worker is unavailable.
    """
    stale_only = (body or ReindexRequest()).stale_only
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    queue: KnowledgeQueue | None = request.app.state.knowledge_queue
    if queue is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document uploads aren't set up: set FORGE_ADMIN_EMBEDDING_REDIS_URL",
        )
    model = _search_model(request)
    if stale_only and model is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Knowledge base search isn't set up, so no document is stale: "
            "set FORGE_ADMIN_MONGO_URI, or re-index every document",
        )
    # Phases current first: a job that finished since is re-indexed too.
    await refresh(
        session,
        queue,
        list(
            await session.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                    KnowledgeDocument.phase.in_(ACTIVE),
                )
            )
        ),
    )
    wanted = KnowledgeDocument.phase.not_in(ACTIVE)
    if stale_only:
        wanted = _stale(model)
    documents = list(
        await session.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == knowledge_base_id, wanted)
            .order_by(KnowledgeDocument.created_at)
        )
    )
    submitted = 0
    try:
        for document in documents:
            # A job still queued or running under the key isn't submitted
            # again; the document then follows that one.
            await queue.ingest_document(
                knowledge_base_id=knowledge_base_id,
                document_id=document.id,
                uri=document.storage_uri,
                filename=document.filename,
                media_type=document.media_type,
                uploaded_by=document.created_by,
                force=True,
            )
            # Its chunks and model stay as they are until the job replaces them.
            document.phase, document.error, document.finished_at = "QUEUED", "", None
            submitted += 1
    except QueueError as error:
        logger.warning("The async worker's queue is unavailable: %s", error)
        await session.commit()  # what was submitted is
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"The async worker is unavailable after {submitted} of "
            f"{len(documents)} documents; try again",
        ) from None
    await session.commit()
    logger.info(
        "%s re-indexed %d %sdocuments of knowledge base %s",
        user,
        submitted,
        "stale " if stale_only else "",
        knowledge_base_id,
    )
    return ReindexResult(submitted=submitted)
