"""Finding a knowledge base a caller may read or manage."""

import casbin
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import Level, Scope, authorize
from forge_admin.models import KnowledgeBase
from forge_admin.models.knowledge import RAG, SYSTEM

# Reading an organization's knowledge bases and their documents.
READ = "organizations:read"
# Creating, changing and deleting them, and uploading, filing, retrying and
# removing their documents (0008knowledge grants it to admins and members).
MANAGE = "knowledge_bases:manage"
NOT_FOUND = "The organization has no such knowledge base"
NOT_OF_KIND = {
    RAG: "A system design knowledge base holds code repositories, not documents",
    SYSTEM: "A RAG knowledge base holds documents, not code repositories",
}


async def knowledge_base_of(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    knowledge_base_id: str,
    *,
    manage: bool = False,
    kind: str | None = RAG,
) -> KnowledgeBase:
    """
    Require reading (or managing) the organization's knowledge bases, and
    find one of them.

    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param user: The caller.
    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases.
    :param manage: Require ``knowledge_bases:manage`` rather than reading.
    :param kind: Require a knowledge base of this kind: by default RAG, for
        the routes of documents and collections; None for any.
    :return: The knowledge base.
    :raises HTTPException: 403 without the permission in the organization;
        404 for another organization's knowledge base, or none; 409 for one
        of another kind.
    """
    await authorize(
        session,
        enforcer,
        user,
        MANAGE if manage else READ,
        Scope(Level.ORG, organization_id),
    )
    found = await session.get(KnowledgeBase, knowledge_base_id)
    if found is None or found.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if kind is not None and found.kind != kind:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OF_KIND[kind])
    return found
