"""
An organization's code repositories: the GitHub repositories it ingests into
the code graph, and their ingestions (``forge_admin.code_repositories``).

An ingestion is a row of ``code_ingestion_jobs``, the code graph worker's
queue: starting one queues it there, and the worker
(apps/forge-codegraph-worker) claims it, builds the graph in its Spanner
database and writes how it went back to the row, which these routes read.
The graph is one per GitHub URL, whichever organizations have the
repository, and follows the branch it was first ingested on, so every
organization adds a URL on that branch.

Reading needs ``organizations:read`` in the organization; adding and
removing repositories and starting and retrying their ingestions need
``repositories:manage`` (its admins and members have it by default).
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import (
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.code_repositories.access import MANAGE, READ, repository_of
from forge_admin.code_repositories.github import (
    InvalidRepository,
    parse_github_url,
    valid_branch,
    valid_commit,
)
from forge_admin.db.audit import UtcDateTime
from forge_admin.db.clock import database_now
from forge_admin.models import CodeIngestionJob, CodeRepository
from forge_admin.models.code_repositories import (
    ACTIVE,
    FAILED,
    QUEUED,
    SUCCEEDED,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["code repositories"])

URL_TAKEN = "The organization already has this repository"
JOB_NOT_FOUND = "The repository has no such ingestion"
OTHER_COMMIT = (
    "An ingestion of another commit is already queued or running; wait for it to end"
)

RepositoryUrl = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
Branch = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]


class RepositoryCreate(BaseModel):
    """
    :ivar url: ``https://github.com/<owner>/<name>``.
    :ivar branch: The branch to ingest; the one the code graph of the URL
        follows, when another organization has it.
    :ivar ingest: Queue its first ingestion right away.
    """

    url: RepositoryUrl
    branch: Branch
    ingest: bool = True


class IngestionCreate(BaseModel):
    """:ivar commit: A full commit SHA on the branch; the branch's head when
    absent."""

    commit: str | None = None


class IngestionRead(Audited):
    """
    One ingestion: ``status`` is ``QUEUED``, ``RUNNING``, ``SUCCEEDED``,
    ``SUPERSEDED`` (a newer commit was published first) or ``FAILED``;
    ``commit_sha`` is the commit asked for, or the branch head the worker
    read; ``metrics`` the counts of the graph it published (files, nodes,
    edges, resolved and unresolved references, …) and ``warning`` in it what
    it had to leave out. ``eligible_at`` is when a waiting one may run (a
    retry after a failure waits); ``attempts`` the tries it was charged.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    repository_id: str
    organization_id: str
    url: str
    branch: str
    commit_sha: str | None
    requested_by: str
    status: str
    attempts: int
    eligible_at: UtcDateTime | None
    codegraph_repository_id: str | None
    run_id: str | None
    generation: int | None
    error_code: str
    error_message: str
    metrics: dict[str, Any] | None
    started_at: UtcDateTime | None
    finished_at: UtcDateTime | None


class RepositoryRead(Audited):
    """
    A repository, with its latest ingestion and its latest successful one
    (its graph's counts), each None until there is one.
    """

    id: str
    organization_id: str
    url: str
    owner: str
    name: str
    branch: str
    latest_ingestion: IngestionRead | None
    last_success: IngestionRead | None


def _job(repository: CodeRepository, user: str, commit: str | None) -> CodeIngestionJob:
    """A queued ingestion of the repository, eligible now by the database's clock."""
    return CodeIngestionJob(
        repository_id=repository.id,
        organization_id=repository.organization_id,
        url=repository.url,
        branch=repository.branch,
        commit_sha=commit,
        requested_by=user,
        status=QUEUED,
        eligible_at=database_now(),
    )


async def _read_jobs(
    session: AsyncSession, repository_ids: list[str], *, succeeded: bool
) -> dict[str, CodeIngestionJob]:
    """:return: Each repository's newest ingestion (of those that succeeded,
    when ``succeeded``), by repository."""
    if not repository_ids:
        return {}
    table = CodeIngestionJob
    ours = table.repository_id.in_(repository_ids)
    if succeeded:
        ours = ours & (table.status == SUCCEEDED)
    newest = (
        select(table.repository_id, func.max(table.created_at).label("at"))
        .where(ours)
        .group_by(table.repository_id)
        .subquery()
    )
    rows = await session.scalars(
        select(table)
        .join(
            newest,
            (table.repository_id == newest.c.repository_id)
            & (table.created_at == newest.c.at),
        )
        .where(ours)
    )
    return {job.repository_id: job for job in rows}


async def _reads(
    session: AsyncSession, repositories: list[CodeRepository]
) -> list[RepositoryRead]:
    ids = [r.id for r in repositories]
    latest = await _read_jobs(session, ids, succeeded=False)
    success = await _read_jobs(session, ids, succeeded=True)

    def job(found: CodeIngestionJob | None) -> IngestionRead | None:
        return None if found is None else as_read(IngestionRead, found)

    return [
        as_read(
            RepositoryRead,
            r,
            latest_ingestion=job(latest.get(r.id)),
            last_success=job(success.get(r.id)),
        )
        for r in repositories
    ]


@router.get("/organizations/{organization_id}/code-repositories")
async def list_code_repositories(
    organization_id: NodeId, user: CurrentUser, session: Session, enforcer: Enforcer
) -> list[RepositoryRead]:
    """
    The organization's code repositories, by URL, each with its latest
    ingestion and its latest successful one. Poll it to follow ingestions.
    \f
    :raises HTTPException: 403 without ``organizations:read``.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    found = await session.scalars(
        select(CodeRepository)
        .where(CodeRepository.organization_id == organization_id)
        .order_by(CodeRepository.url)
    )
    return await _reads(session, list(found))


@router.post(
    "/organizations/{organization_id}/code-repositories",
    status_code=status.HTTP_201_CREATED,
)
async def add_code_repository(
    organization_id: NodeId,
    body: RepositoryCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> RepositoryRead:
    """
    Add a GitHub repository to the organization, and queue its first
    ingestion unless ``ingest`` is false.
    \f
    :raises HTTPException: 403 without ``repositories:manage``; 409 when the
        organization has it already, or when its code graph follows another
        branch (the message names it); 422 for a URL that isn't a GitHub
        repository's, or a branch name Git wouldn't take.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    repository = await add_repository(session, organization_id, body, user)
    await commit_or_conflict(session, URL_TAKEN)
    logger.info(
        "%s added code repository %s (%s, branch %s) to organization %s",
        user,
        repository.id,
        repository.url,
        repository.branch,
        organization_id,
    )
    return (await _reads(session, [repository]))[0]


async def add_repository(
    session: AsyncSession, organization_id: str, body: RepositoryCreate, user: str
) -> CodeRepository:
    """
    Add a GitHub repository to the organization, and queue its first
    ingestion unless ``body.ingest`` is false; flushed, not committed. The
    caller holds ``repositories:manage``.

    :return: The repository.
    :raises HTTPException: 409 when the organization has it already, or when
        its code graph follows another branch; 422 for a URL that isn't a
        GitHub repository's, or a branch name Git wouldn't take.
    """
    try:
        github = parse_github_url(body.url)
    except InvalidRepository as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    if not valid_branch(body.branch):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Not a branch name Git accepts"
        )
    taken = await session.scalar(
        select(CodeRepository.id).where(
            CodeRepository.organization_id == organization_id,
            CodeRepository.url == github.url,
        )
    )
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, URL_TAKEN)
    # One graph per URL, on one branch: what other organizations have it on
    # (locked, so two first additions on different branches take turns).
    # The worker refuses a job on another branch too.
    other = await session.scalar(
        select(CodeRepository.branch)
        .where(CodeRepository.url == github.url, CodeRepository.branch != body.branch)
        .limit(1)
        .with_for_update()
    )
    if other is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"The code graph of {github.url} follows branch {other!r}; add it "
            "with that branch",
        )
    repository = CodeRepository(
        organization_id=organization_id,
        url=github.url,
        owner=github.owner,
        name=github.name,
        branch=body.branch,
    )
    session.add(repository)
    await session.flush()
    if body.ingest:
        session.add(_job(repository, user, None))
    return repository


@router.get("/organizations/{organization_id}/code-repositories/{repository_id}")
async def get_code_repository(
    organization_id: NodeId,
    repository_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> RepositoryRead:
    """
    One code repository, with its latest ingestion and its latest successful
    one.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's repository, or none.
    """
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id
    )
    return (await _reads(session, [repository]))[0]


@router.delete(
    "/organizations/{organization_id}/code-repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_code_repository(
    organization_id: NodeId,
    repository_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Remove a repository from the organization, and its ingestions. One the
    worker is running stops at its next write. The code graph itself stays:
    other organizations may have the repository.
    \f
    :raises HTTPException: 403 without ``repositories:manage``; 404 for
        another organization's repository, or none.
    """
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id, manage=True
    )
    await session.delete(repository)
    await session.commit()
    logger.info(
        "%s removed code repository %s (%s) from organization %s",
        user,
        repository_id,
        repository.url,
        organization_id,
    )


@router.post(
    "/organizations/{organization_id}/code-repositories/{repository_id}/ingestions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_code_repository(
    organization_id: NodeId,
    repository_id: NodeId,
    body: IngestionCreate,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> IngestionRead:
    """
    Queue an ingestion of the repository's branch: of ``commit``, or of the
    branch's head when the worker gets to it. A repository has one ingestion
    queued or running at a time: asking again answers 200 with it (and one
    waiting to retry is made eligible now), unless it's of another commit.
    \f
    :raises HTTPException: 403 without ``repositories:manage``; 404 for
        another organization's repository, or none; 409 when another commit's
        ingestion is queued or running; 422 for a commit that isn't a full
        SHA.
    """
    if body.commit is not None and not valid_commit(body.commit):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "commit must be a full commit SHA, lowercase hex",
        )
    # Locked, so requests to ingest it take turns.
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id, manage=True, lock=True
    )
    active = await _active_job(session, repository.id)
    if active is not None:
        if body.commit is not None and body.commit != active.commit_sha:
            raise HTTPException(status.HTTP_409_CONFLICT, OTHER_COMMIT)
        if active.status == QUEUED:
            active.eligible_at = database_now()  # type: ignore[assignment]
        await session.commit()
        await session.refresh(active)
        response.status_code = status.HTTP_200_OK
        return as_read(IngestionRead, active)
    job = _job(repository, user, body.commit)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    logger.info(
        "%s queued ingestion %s of code repository %s (%s) in organization %s",
        user,
        job.id,
        repository.id,
        repository.url,
        organization_id,
    )
    return as_read(IngestionRead, job)


async def _active_job(
    session: AsyncSession, repository_id: str
) -> CodeIngestionJob | None:
    return await session.scalar(
        select(CodeIngestionJob)
        .where(
            CodeIngestionJob.repository_id == repository_id,
            CodeIngestionJob.status.in_(ACTIVE),
        )
        .order_by(CodeIngestionJob.created_at.desc())
        .limit(1)
    )


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/ingestions"
)
async def list_code_repository_ingestions(
    organization_id: NodeId,
    repository_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[IngestionRead]:
    """
    The repository's ingestions, newest first.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another organization's repository, or none.
    """
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id
    )
    found = await session.scalars(
        select(CodeIngestionJob)
        .where(CodeIngestionJob.repository_id == repository.id)
        .order_by(CodeIngestionJob.created_at.desc(), CodeIngestionJob.id)
        .limit(limit)
        .offset(offset)
    )
    return [as_read(IngestionRead, job) for job in found]


async def _job_of(
    session: AsyncSession, repository: CodeRepository, job_id: str
) -> CodeIngestionJob:
    job = await session.get(CodeIngestionJob, job_id)
    if job is None or job.repository_id != repository.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, JOB_NOT_FOUND)
    return job


@router.get(
    "/organizations/{organization_id}/code-repositories/{repository_id}/ingestions/{job_id}"
)
async def get_code_repository_ingestion(
    organization_id: NodeId,
    repository_id: NodeId,
    job_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> IngestionRead:
    """
    One ingestion of the repository.
    \f
    :raises HTTPException: 403 without ``organizations:read``; 404 for
        another repository's ingestion, or none.
    """
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id
    )
    return as_read(IngestionRead, await _job_of(session, repository, job_id))


@router.post(
    "/organizations/{organization_id}/code-repositories/{repository_id}/ingestions/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_code_repository_ingestion(
    organization_id: NodeId,
    repository_id: NodeId,
    job_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> IngestionRead:
    """
    Queue a failed ingestion again, with its attempts reset: after fixing
    what failed it, such as the GitHub token's access. It ingests the commit
    it was on, and resumes the run the worker had started.
    \f
    :raises HTTPException: 403 without ``repositories:manage``; 404 for
        another repository's ingestion, or none; 409 when it didn't fail, or
        another ingestion is queued or running.
    """
    repository = await repository_of(
        session, enforcer, user, organization_id, repository_id, manage=True, lock=True
    )
    job = await _job_of(session, repository, job_id)
    if job.status != FAILED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a failed ingestion can be retried"
        )
    if await _active_job(session, repository.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another ingestion of the repository is queued or running",
        )
    # The claim token stays: a worker still holding an old claim can't
    # write to it again. The commit and the run stay, so it resumes.
    job.status = QUEUED
    job.attempts = 0
    job.error_code = ""
    job.error_message = ""
    job.finished_at = None
    job.eligible_at = database_now()  # type: ignore[assignment]
    await session.commit()
    await session.refresh(job)
    logger.info(
        "%s retried ingestion %s of code repository %s in organization %s",
        user,
        job.id,
        repository.id,
        organization_id,
    )
    return as_read(IngestionRead, job)
