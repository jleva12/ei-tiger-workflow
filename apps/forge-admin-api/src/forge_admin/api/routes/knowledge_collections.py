"""Collections a knowledge base files its documents in, like folders, nested
like folders: each is at the top of the knowledge base or in another
collection. They organize the documents for people; the agents search every
document of the knowledge base whatever its collection. A name is unique
among its siblings. Reading them takes ``organizations:read`` in the
organization; changing them takes ``knowledge_bases:manage``, as uploading
does. Uploading a folder recreates its tree of folders as collections
(``POST .../document-collections/paths``), reusing the collections that
already have those names.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeCreate,
    NodeId,
    NodeUpdate,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import CurrentUser, Enforcer
from forge_admin.knowledge.access import knowledge_base_of
from forge_admin.models import KnowledgeCollection, KnowledgeDocument

router = APIRouter(tags=["knowledge base documents"])

CollectionId = Annotated[str, Path(max_length=36)]
NAME_TAKEN = "A collection with this name is already there"
NO_COLLECTION = "The knowledge base has no such collection"
# How deep a folder may nest, and how many folders one upload may name.
MAX_DEPTH = 32
MAX_PATHS = 1000


class CollectionRead(Audited):
    id: str
    knowledge_base_id: str
    # The collection it's in; None at the top of the knowledge base.
    parent_id: str | None
    name: str
    description: str
    # What's filed in it now, not counting its collections'.
    document_count: int = 0
    size_bytes: int = 0


class CollectionCreate(NodeCreate):
    # The collection to create it in; the top of the knowledge base when absent.
    parent_id: str | None = Field(default=None, max_length=36)


class CollectionPaths(BaseModel):
    # Where the folders go: a collection, or the top of the knowledge base when absent.
    parent_id: str | None = Field(default=None, max_length=36)
    # Each folder as the names from the top of the upload down, e.g.
    # ["Runbooks", "payments"].
    paths: list[Annotated[list[Name], Field(min_length=1, max_length=MAX_DEPTH)]] = (
        Field(min_length=1, max_length=MAX_PATHS)
    )


class CollectionAtPath(BaseModel):
    path: list[str]
    collection: CollectionRead


class CollectionsAtPaths(BaseModel):
    # One per path asked for, in order.
    collections: list[CollectionAtPath]


async def _counts(
    session: AsyncSession, knowledge_base_id: str, collection_id: str | None = None
) -> dict[str, tuple[int, int]]:
    """:return: Each collection's document count and bytes, by its ID."""
    query = (
        select(
            KnowledgeDocument.collection_id,
            func.count(KnowledgeDocument.id),
            func.coalesce(func.sum(KnowledgeDocument.size_bytes), 0),
        )
        .where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.collection_id.is_not(None),
        )
        .group_by(KnowledgeDocument.collection_id)
    )
    if collection_id is not None:
        query = query.where(KnowledgeDocument.collection_id == collection_id)
    return {
        str(key): (int(count), int(size))
        for key, count, size in await session.execute(query)
    }


def _read(
    collection: KnowledgeCollection, counts: dict[str, tuple[int, int]]
) -> CollectionRead:
    count, size = counts.get(collection.id, (0, 0))
    return as_read(CollectionRead, collection, document_count=count, size_bytes=size)


async def _collection_of(
    session: AsyncSession,
    knowledge_base_id: str,
    collection_id: str,
    *,
    missing: int = 404,
) -> KnowledgeCollection:
    """:raises HTTPException: ``missing`` for another knowledge base's collection, or none."""
    found = await session.get(KnowledgeCollection, collection_id)
    if found is None or found.knowledge_base_id != knowledge_base_id:
        raise HTTPException(missing, NO_COLLECTION)
    return found


async def _all_collections(
    session: AsyncSession, knowledge_base_id: str
) -> list[KnowledgeCollection]:
    return list(
        await session.scalars(
            select(KnowledgeCollection)
            .where(KnowledgeCollection.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeCollection.name)
        )
    )


async def _check_name_free(
    session: AsyncSession,
    knowledge_base_id: str,
    parent_id: str | None,
    name: str,
    *,
    besides: frozenset[str] = frozenset(),
) -> None:
    """
    Refuse a name a sibling has, in any case. The unique key catches the
    rest, except at the top of a knowledge base, where MySQL counts the null parents
    as distinct.

    :param besides: Collections that don't count: the one renamed, or the
        one deleted and the one moving up.
    :raises HTTPException: 409 when a sibling has the name.
    """
    siblings = await session.scalars(
        select(KnowledgeCollection).where(
            KnowledgeCollection.knowledge_base_id == knowledge_base_id,
            KnowledgeCollection.parent_id.is_(None)
            if parent_id is None
            else KnowledgeCollection.parent_id == parent_id,
        )
    )
    if any(
        s.name.casefold() == name.casefold() and s.id not in besides for s in siblings
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, NAME_TAKEN)


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/document-collections"
)
async def list_collections(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[CollectionRead]:
    """
    List all of a knowledge base's document collections, at every level, by name: each
    with the collection it's in and what's filed in it.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The collections.
    """
    await knowledge_base_of(session, enforcer, user, organization_id, knowledge_base_id)
    collections = await _all_collections(session, knowledge_base_id)
    counts = await _counts(session, knowledge_base_id)
    return [_read(collection, counts) for collection in collections]


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/document-collections",
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: CollectionCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CollectionRead:
    """
    Create a document collection at the top of a knowledge base, or in one of its
    collections (``parent_id``).
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param body: Its name, unique among its siblings, description and parent.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new collection, empty.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 409
        when a sibling has the name; 422 for a parent the knowledge base doesn't have.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    if body.parent_id is not None:
        await _collection_of(session, knowledge_base_id, body.parent_id, missing=422)
    await _check_name_free(session, knowledge_base_id, body.parent_id, body.name)
    collection = KnowledgeCollection(
        knowledge_base_id=knowledge_base_id,
        parent_id=body.parent_id,
        name=body.name,
        description=body.description,
    )
    session.add(collection)
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(collection, {})


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/document-collections/paths"
)
async def ensure_collection_paths(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: CollectionPaths,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CollectionsAtPaths:
    """
    Make sure each folder path exists as nested collections under the parent,
    and return the collection at the end of each: an uploaded folder's tree,
    recreated. A folder that a collection already has a name for, in any
    case, is that collection; the rest are created. Nothing is created unless
    every path can be.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param body: The parent, and the paths.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The collection at the end of each path, in the order asked.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 409
        when another upload creates the same folder at the same moment; 422
        for a parent the knowledge base doesn't have.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    if body.parent_id is not None:
        await _collection_of(session, knowledge_base_id, body.parent_id, missing=422)
    existing = await _all_collections(session, knowledge_base_id)
    # (parent, folded name) -> collection
    children = {(c.parent_id, c.name.casefold()): c for c in existing}
    ends: list[KnowledgeCollection] = []
    for path in body.paths:
        parent = body.parent_id
        found: KnowledgeCollection | None = None
        for name in path:
            found = children.get((parent, name.casefold()))
            if found is None:
                found = KnowledgeCollection(
                    knowledge_base_id=knowledge_base_id,
                    parent_id=parent,
                    name=name,
                    description="",
                )
                session.add(found)
                await session.flush()
                children[(parent, name.casefold())] = found
            parent = found.id
        assert found is not None  # every path has a name
        ends.append(found)
    await commit_or_conflict(session, NAME_TAKEN)
    counts = await _counts(session, knowledge_base_id)
    return CollectionsAtPaths(
        collections=[
            CollectionAtPath(path=path, collection=_read(end, counts))
            for path, end in zip(body.paths, ends, strict=True)
        ]
    )


@router.patch(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/document-collections/{collection_id}"
)
async def update_collection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    collection_id: CollectionId,
    body: NodeUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CollectionRead:
    """
    Rename or describe a document collection.
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param collection_id: The collection.
    :param body: The fields to change; absent ones are left alone.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The collection.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for another knowledge base's collection, or none; 409 when a sibling has the
        new name.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    collection = await _collection_of(session, knowledge_base_id, collection_id)
    if body.name is not None:
        await _check_name_free(
            session,
            knowledge_base_id,
            collection.parent_id,
            body.name,
            besides=frozenset({collection.id}),
        )
        collection.name = body.name
    if body.description is not None:
        collection.description = body.description
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(collection, await _counts(session, knowledge_base_id, collection.id))


@router.delete(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/document-collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_collection(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    collection_id: CollectionId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete a document collection. What's in it moves up to its parent: its
    collections, and its documents, which stay searchable (at the top of the
    knowledge base they're unfiled).
    \f
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param collection_id: The collection.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :raises HTTPException: 403 without ``knowledge_bases:manage`` in the organization; 404
        for another knowledge base's collection, or none; 409 when a collection moving
        up has the name of one already there.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, manage=True
    )
    collection = await _collection_of(session, knowledge_base_id, collection_id)
    parent = collection.parent_id
    moving = list(
        await session.scalars(
            select(KnowledgeCollection).where(
                KnowledgeCollection.parent_id == collection.id
            )
        )
    )
    for child in moving:
        await _check_name_free(
            session,
            knowledge_base_id,
            parent,
            child.name,
            besides=frozenset({child.id, collection.id}),
        )
    # Moved explicitly, and audited as this change, rather than left to the
    # foreign keys' SET NULL.
    await session.execute(
        update(KnowledgeCollection)
        .where(KnowledgeCollection.parent_id == collection.id)
        .values(parent_id=parent)
    )
    await session.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.collection_id == collection.id)
        .values(collection_id=parent)
    )
    await session.delete(collection)
    await commit_or_conflict(session, NAME_TAKEN)
