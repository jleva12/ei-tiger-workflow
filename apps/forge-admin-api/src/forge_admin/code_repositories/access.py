"""Finding a code repository a caller may read or manage."""

import casbin
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import Level, Scope, authorize
from forge_admin.models import CodeRepository

# Reading an organization's code repositories and their ingestions.
READ = "organizations:read"
# Adding and removing them, and starting and retrying their ingestions
# (0014code_repositories grants it to admins and members).
MANAGE = "repositories:manage"
NOT_FOUND = "The organization has no such code repository"


async def repository_of(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    repository_id: str,
    *,
    manage: bool = False,
    lock: bool = False,
) -> CodeRepository:
    """
    Require reading (or managing) the organization's code repositories, and
    find one of them.

    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param user: The caller.
    :param organization_id: The organization.
    :param repository_id: One of its repositories.
    :param manage: Require ``repositories:manage`` rather than reading.
    :param lock: Lock its row until the transaction ends (``FOR UPDATE``), so
        requests that change its ingestions take turns.
    :return: The repository.
    :raises HTTPException: 403 without the permission in the organization;
        404 for another organization's repository, or none.
    """
    await authorize(
        session,
        enforcer,
        user,
        MANAGE if manage else READ,
        Scope(Level.ORG, organization_id),
    )
    found = await session.get(CodeRepository, repository_id, with_for_update=lock)
    if found is None or found.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return found
