"""An organization's background tasks: the jobs the async worker runs for it (its
workflow runs), with each one's attempts, failures and audit trail as the worker's task framework
recorded them, relayed from its background tasks API
(``forge_admin.background_tasks``).

Anyone who can see the organization (``organizations:read``) can read its background tasks;
resubmitting, restarting and abandoning one needs ``background_tasks:manage``
there. Deciding the approval a workflow run waits at needs what its step
asks of the approvers: ``workflows:approve`` (the organization's admins) when
they are its admins, ``workflows:run`` (every member) when any member may. An
ADK workflow run's approvals and questions are decided and answered at its
own routes instead (``adk_workflow_runs.py``), with the ADK workflows'
permissions.

A task belongs to the organization its ``tenant`` label names: the organization whose
workflow ran, which the jobs it leads to inherit.
Every route about one task reads it first, and answers 404 for another organization's
task. Answers are the worker API's own JSON.
"""

import logging
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import NodeId, Session
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.background_tasks import BackgroundTasks, BackgroundTasksError
from forge_admin.embedding import ADK_WORKFLOWS
from forge_admin.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["background tasks"])

MANAGE = "background_tasks:manage"
NO_SUCH_TASK = "The organization has no such background task"
NOT_OPEN = "That approval isn't open any more: someone decided it, or the run moved on"
# Who decides a workflow's approval, as its step names the approvers: the
# permission they hold in the organization. Anything else is the admins'.
APPROVERS = {"org:admin": "workflows:approve", "org:member": "workflows:run"}

TaskId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
TaskStatus = Literal[
    "PENDING",
    "RUNNING",
    "STOPPING",
    "PAUSING",
    "PAUSED",
    "AWAITING_VALIDATION",
    "STOPPED",
    "COMPLETED",
    "FAILED",
    "ABANDONED",
    "UNKNOWN",
]
TASK_TYPE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class DecisionCreate(BaseModel):
    """A decision on the approval a workflow run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The approval decided, as the task's ``approval`` names it: a decision
    #: never lands on a later approval of the same run.
    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    #: Why, for the record; the run's later steps can read it.
    comment: str = Field(default="", max_length=4000)


def background_tasks_client(request: Request) -> BackgroundTasks:
    client: BackgroundTasks | None = request.app.state.background_tasks
    if client is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Background tasks aren't set up: set FORGE_ADMIN_ASYNC_WORKER_URL and "
            "FORGE_ADMIN_ASYNC_WORKER_TOKEN",
        )
    return client


def refusal(error: BackgroundTasksError) -> HTTPException:
    """
    Explain the background tasks API's refusal to the caller.

    :return: 503 when it's unavailable; its 404 or 409 with its message; 502
        when it refused Forge's token or answered unexpectedly.
    """
    if error.status == 0 or error.status >= 500:
        logger.warning("Background tasks API unavailable: %s", error)
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The async worker is unavailable; try again",
        )
    if error.status == status.HTTP_401_UNAUTHORIZED:
        logger.error(
            "The background tasks API refused Forge's token: "
            "FORGE_ADMIN_ASYNC_WORKER_TOKEN must equal its HYBRID_API__TOKEN"
        )
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The async worker refused Forge"
        )
    if error.status == status.HTTP_404_NOT_FOUND:
        return HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_TASK)
    if error.status == status.HTTP_409_CONFLICT:
        return HTTPException(status.HTTP_409_CONFLICT, error.message)
    logger.error("Background tasks API answered unexpectedly: %s", error)
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, "The async worker answered unexpectedly"
    )


async def organization_task(
    request: Request,
    session: AsyncSession,
    enforcer: Any,
    user: str,
    organization_id: str,
    task_id: str,
    permission: str = "organizations:read",
) -> tuple[BackgroundTasks, dict[str, Any]]:
    """
    Authorize the caller in the organization, then read the task, which must be the
    organization's.

    :return: The client and the task, as the worker has it.
    :raises HTTPException: 403 without the permission in the organization; 404 for no
        such organization, or a task that isn't the organization's; 503 when background tasks
        aren't set up or the worker is unavailable.
    """
    await authorize(
        session, enforcer, user, permission, Scope(Level.ORG, organization_id)
    )
    client = background_tasks_client(request)
    try:
        task = await client.task(task_id)
    except BackgroundTasksError as error:
        raise refusal(error) from None
    labels = task.get("labels")
    if not isinstance(labels, dict) or labels.get("tenant") != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_TASK)
    return client, task


def task_type_of(task: dict[str, Any]) -> str:
    """:return: The task's type (``workflows``, ``adk_workflows``...)."""
    labels = task.get("labels")
    found = task.get("task_type") or (
        labels.get("task_type") if isinstance(labels, dict) else None
    )
    return found if isinstance(found, str) else ""


async def actor_of(session: AsyncSession, user: str) -> dict[str, str]:
    """Who acts, as the worker records it: their ID and, when known, name."""
    person = await session.get(User, user)
    name = f"{person.first_name} {person.last_name}".strip() if person else ""
    return {"id": user, "display_name": name}


@router.get("/organizations/{organization_id}/background-tasks")
async def list_background_tasks(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    task_type: Annotated[list[str] | None, Query()] = None,
    exclude_task_type: Annotated[list[str] | None, Query()] = None,
    status_filter: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """
    List an organization's background tasks, newest first.
    \f
    :param task_type: Only these task types (``workflows``, ...).
    :param exclude_task_type: Every task type but these: ``workflows`` for its
        tasks without its workflow runs, which the Workflows page lists.
    :param status_filter: Only tasks in these statuses.
    :return: ``{"items": [...], "total"}``: each task's job, status, attempts,
        times and latest failure, and how many match in all.
    :raises HTTPException: 403 without ``organizations:read`` in the organization; 422 for an
        invalid task type; 503 when background tasks aren't set up or the
        worker is unavailable.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    task_types = task_type or []
    excluded = exclude_task_type or []
    for name in (*task_types, *excluded):
        if not TASK_TYPE.fullmatch(name):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown task type {name!r}"
            )
    client = background_tasks_client(request)
    try:
        return await client.tasks(
            tenant=organization_id,
            task_types=task_types,
            exclude_task_types=excluded,
            statuses=status_filter or [],
            limit=limit,
            offset=offset,
        )
    except BackgroundTasksError as error:
        raise refusal(error) from None


@router.get("/organizations/{organization_id}/background-tasks/{task_id}")
async def get_background_task(
    organization_id: NodeId,
    task_id: TaskId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Read one of an organization's background tasks: what it was asked to do, how each
    attempt ran and failed, its audit trail, and the actions its state allows.
    \f
    :raises HTTPException: 403 without ``organizations:read`` in the organization; 404 for a
        task that isn't the organization's.
    """
    _, task = await organization_task(
        request, session, enforcer, user, organization_id, task_id
    )
    return task


@router.post(
    "/organizations/{organization_id}/background-tasks/{task_id}/resubmit",
    status_code=status.HTTP_202_ACCEPTED,
)
async def resubmit_background_task(
    organization_id: NodeId,
    task_id: TaskId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Run an organization's background task again, with the same input, as a new task.
    \f
    :return: ``{"queue", "key"}`` of the job that will run it.
    :raises HTTPException: 403 without ``background_tasks:manage`` in the organization;
        404 for a task that isn't the organization's; 409 while it's still running.
    """
    client, _ = await organization_task(
        request, session, enforcer, user, organization_id, task_id, MANAGE
    )
    try:
        answer = await client.resubmit(task_id, await actor_of(session, user))
    except BackgroundTasksError as error:
        raise refusal(error) from None
    logger.info("%s resubmitted background task %s", user, task_id)
    return answer


@router.post(
    "/organizations/{organization_id}/background-tasks/{task_id}/restart",
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_background_task(
    organization_id: NodeId,
    task_id: TaskId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Retry an organization's failed or stopped background task as its next attempt,
    skipping what it already finished.
    \f
    :return: ``{"queue", "key"}`` of the job that will run it.
    :raises HTTPException: 403 without ``background_tasks:manage`` in the organization;
        404 for a task that isn't the organization's; 409 unless it failed or stopped.
    """
    client, _ = await organization_task(
        request, session, enforcer, user, organization_id, task_id, MANAGE
    )
    try:
        answer = await client.restart(task_id, await actor_of(session, user))
    except BackgroundTasksError as error:
        raise refusal(error) from None
    logger.info("%s restarted background task %s", user, task_id)
    return answer


@router.post("/organizations/{organization_id}/background-tasks/{task_id}/abandon")
async def abandon_background_task(
    organization_id: NodeId,
    task_id: TaskId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Give up on an organization's failed or stopped background task: no more attempts,
    automatic or not.
    \f
    :return: The task, abandoned.
    :raises HTTPException: 403 without ``background_tasks:manage`` in the organization;
        404 for a task that isn't the organization's; 409 unless it failed or stopped.
    """
    client, _ = await organization_task(
        request, session, enforcer, user, organization_id, task_id, MANAGE
    )
    try:
        answer = await client.abandon(task_id, await actor_of(session, user))
    except BackgroundTasksError as error:
        raise refusal(error) from None
    logger.info("%s abandoned background task %s", user, task_id)
    return answer


@router.post(
    "/organizations/{organization_id}/background-tasks/{task_id}/decisions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_background_task(
    organization_id: NodeId,
    task_id: TaskId,
    body: DecisionCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Approve or reject what an organization's workflow run waits for: the run carries
    on down the approval step's approved or rejected way, on a worker, with
    who decided and why.
    \f
    :return: ``{"queue", "key"}`` of the job that will carry it on.
    :raises HTTPException: 403 without the permission the step asks of its
        approvers; 404 for a task that isn't the organization's; 409 when the
        approval isn't open (decided already, or the run moved on).
    """
    client, task = await organization_task(
        request, session, enforcer, user, organization_id, task_id
    )
    if task_type_of(task) == ADK_WORKFLOWS:
        # Its own route decides it, with the ADK workflows' permissions.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An ADK workflow run's approvals are decided at "
            f"/organizations/{organization_id}/adk-runs/{task_id}/decisions",
        )
    approval = task.get("approval")
    if not isinstance(approval, dict) or approval.get("id") != body.request_id:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    details = approval.get("details")
    approvers = details.get("approvers") if isinstance(details, dict) else None
    permission = APPROVERS.get(str(approvers), APPROVERS["org:admin"])
    await authorize(
        session, enforcer, user, permission, Scope(Level.ORG, organization_id)
    )
    try:
        answer = await client.decide(
            task_id,
            request_id=body.request_id,
            approved=body.approved,
            comment=body.comment.strip(),
            actor=await actor_of(session, user),
        )
    except BackgroundTasksError as error:
        raise refusal(error) from None
    logger.info(
        "%s %s background task %s",
        user,
        "approved" if body.approved else "rejected",
        task_id,
    )
    return answer
