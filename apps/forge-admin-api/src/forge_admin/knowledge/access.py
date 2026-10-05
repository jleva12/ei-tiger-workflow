"""Finding a knowledge base a caller may read or manage."""

import casbin
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import Level, Scope, authorize
from forge_admin.models import KnowledgeBase

# Reading an organization's knowledge bases and their documents.
READ = "organizations:read"
# Creating, changing and deleting them, and uploading, filing, retrying and
# removing their documents (0008knowledge grants it to admins and members).
MANAGE = "knowledge_bases:manage"
NOT_FOUND = "The organization has no such knowledge base"


async def knowledge_base_of(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    knowledge_base_id: str,
    *,
    manage: bool = False,
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
    :return: The knowledge base.
    :raises HTTPException: 403 without the permission in the organization;
        404 for another organization's knowledge base, or none.
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
    return found
