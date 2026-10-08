"""
A system design knowledge base's code repositories (``knowledge_base_repositories``):
the organization's code repositories (``code_repositories``) it includes,
whose code its agents search in the code graph. A repository may be in any
number of knowledge bases; removing it from one leaves it in the
organization, with its code graph.

Reading needs ``organizations:read`` in the organization; adding and
removing need ``knowledge_bases:manage``, and adding a repository the
organization doesn't have yet ``repositories:manage`` too.
"""

import logging
from typing import Annotated, Self

import casbin
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.code_repositories import (
    Branch,
    RepositoryCreate,
    RepositoryRead,
    RepositoryUrl,
    _reads,
    add_repository,
)
from forge_admin.api.routes.common import NodeId, Session, commit_or_conflict
from forge_admin.auth.access import (
    UUID_PATTERN,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.code_repositories import access as repositories
from forge_admin.code_repositories.github import InvalidRepository, parse_github_url
from forge_admin.knowledge.access import knowledge_base_of
from forge_admin.knowledge.connections import commit_synced, has_code_links
from forge_admin.knowledge.graph import linked
from forge_admin.models import CodeRepository, KnowledgeBaseRepository
from forge_admin.models.knowledge import SYSTEM

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge bases"])

NOT_INCLUDED = "The knowledge base doesn't include this repository"
RepositoryId = Annotated[str, StringConstraints(pattern=rf"^{UUID_PATTERN}$")]


class KnowledgeRepositoryAdd(BaseModel):
    """
    One of the organization's repositories, by ``repository_id``; or a GitHub
    repository by ``url`` and ``branch``, which is added to the organization
    (its first ingestion queued unless ``ingest`` is false) when it doesn't
    have it yet.
    """

    repository_id: RepositoryId | None = None
    url: RepositoryUrl | None = None
    branch: Branch | None = None
    ingest: bool = True

    @model_validator(mode="after")
    def _one_repository(self) -> Self:
        if (self.repository_id is None) == (self.url is None):
            raise ValueError("Give repository_id, or url and branch")
        if self.url is not None and self.branch is None:
            raise ValueError("A repository added by url needs its branch")
        return self


@router.get(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/repositories"
)
async def list_knowledge_base_repositories(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[RepositoryRead]:
    """
    The system design knowledge base's repositories, by URL, each with its latest
    ingestion and its latest successful one. Poll it to follow ingestions.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's knowledge base, or none; 409 for a RAG one.
    """
    await knowledge_base_of(
        session, enforcer, user, organization_id, knowledge_base_id, kind=SYSTEM
    )
    found = await linked(session, [knowledge_base_id])
    return await _reads(session, found[knowledge_base_id])


@router.post(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/repositories",
    status_code=status.HTTP_201_CREATED,
)
async def add_knowledge_base_repository(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    body: KnowledgeRepositoryAdd,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> RepositoryRead:
    """
    Include a repository in the system design knowledge base: one of the
    organization's, or a GitHub repository, which the organization gets
    (with its first ingestion queued, unless ``ingest`` is false) when it
    doesn't have it already. Including one it includes already answers 200.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``, or
        ``repositories:manage`` to add a repository to the organization;
        404 for another organization's knowledge base or repository, or
        none; 409 for a RAG knowledge base, or a URL the organization has on
        another branch, or whose code graph follows another; 422 for a URL
        that isn't a GitHub repository's, or a branch name Git wouldn't take.
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
    repository = await _repository(session, enforcer, user, organization_id, body)
    included = await session.get(
        KnowledgeBaseRepository, (knowledge_base_id, repository.id)
    )
    if included is not None:
        await session.commit()  # a repository added by URL just now is kept
        response.status_code = status.HTTP_200_OK
        return (await _reads(session, [repository]))[0]
    session.add(
        KnowledgeBaseRepository(
            knowledge_base_id=knowledge_base_id, repository_id=repository.id
        )
    )
    await commit_or_conflict(
        session, "The repository was added at the same time; retry"
    )
    logger.info(
        "%s included code repository %s (%s) in knowledge base %s",
        user,
        repository.id,
        repository.url,
        knowledge_base_id,
    )
    return (await _reads(session, [repository]))[0]


async def _repository(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    organization_id: str,
    body: KnowledgeRepositoryAdd,
) -> CodeRepository:
    """The organization's repository the body names, added to it when it's a
    URL it doesn't have yet."""
    if body.repository_id is not None:
        return await repositories.repository_of(
            session, enforcer, user, organization_id, body.repository_id
        )
    assert body.url is not None and body.branch is not None
    try:
        url = parse_github_url(body.url).url
    except InvalidRepository as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    existing = await session.scalar(
        select(CodeRepository).where(
            CodeRepository.organization_id == organization_id,
            CodeRepository.url == url,
        )
    )
    if existing is not None:
        if existing.branch != body.branch:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"The organization has {url} on branch {existing.branch!r}; "
                "include that one",
            )
        return existing
    await authorize(
        session, enforcer, user, repositories.MANAGE, Scope(Level.ORG, organization_id)
    )
    repository = await add_repository(
        session,
        organization_id,
        RepositoryCreate(url=body.url, branch=body.branch, ingest=body.ingest),
        user,
    )
    logger.info(
        "%s added code repository %s (%s, branch %s) to organization %s",
        user,
        repository.id,
        repository.url,
        repository.branch,
        organization_id,
    )
    return repository


@router.delete(
    "/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}/repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_knowledge_base_repository(
    organization_id: NodeId,
    knowledge_base_id: NodeId,
    repository_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a repository from the system design knowledge base, with its
    connections to and from the knowledge base's other applications. It
    stays in the organization, with its ingestions and code graph, and in
    other knowledge bases that include it.
    \f
    :raises HTTPException: 403 without ``knowledge_bases:manage``; 404 for
        another organization's knowledge base, or none, or a repository it
        doesn't include; 409 for a RAG knowledge base.
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
    included = await session.get(
        KnowledgeBaseRepository, (knowledge_base_id, repository_id)
    )
    if included is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_INCLUDED)
    # Its connections go with it, and their code links leave the code graph.
    coded = await has_code_links(
        session, knowledge_base_id, repository_id=repository_id
    )
    await session.delete(included)
    await commit_synced(
        session,
        getattr(request.app.state, "codegraph", None),
        [knowledge_base_id] if coded else [],
    )
    logger.info(
        "%s removed code repository %s from knowledge base %s",
        user,
        repository_id,
        knowledge_base_id,
    )
