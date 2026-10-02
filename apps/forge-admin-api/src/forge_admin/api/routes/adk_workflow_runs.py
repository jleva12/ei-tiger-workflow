"""
Runs of an organization's ADK workflows (``forge.agent/v1``, the agents of
``organization_agents.py``): submitted to the async worker's ``adk_workflows``
queue (``forge_admin.adk_runs``), followed as the organization's background
tasks, their steps read from their ADK sessions, and what they wait for
decided or answered here.

- Running one needs ``agents:run`` in the organization (its admins and members
  by default). The run takes the ADK workflow as it's saved then, and acts as
  the member who ran it.
- Listing a workflow's runs and reading a run's steps need
  ``organizations:read``.
- An approval a run waits at is decided by whom its step names:
  ``agents:approve`` (the organization's admins) when they're its admins,
  ``agents:run`` (every member) when any member may.
- A question (human input) is answered by anyone with ``agents:run``; the
  answer must fit what it asks (its ``response_schema``), and is sent as
  the decision's comment, as JSON.

A run is found by its background task, which must be the organization's and an
ADK workflow run's; any other answers 404.
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from forge_task_adk_workflows.steps import run_steps
from google.adk.sessions import BaseSessionService
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.adk_runs import (
    AGENT_LABEL,
    APP_NAME,
    SESSION_LABEL,
    AdkRunError,
    checked_answer,
    prepare_run,
    start_adk_run,
)
from forge_admin.api.routes.background_tasks import (
    NO_SUCH_TASK,
    NOT_OPEN,
    DecisionCreate,
    TaskId,
    actor_of,
    background_tasks_client,
    organization_task,
    refusal,
    task_type_of,
)
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.api.routes.organization_agents import (
    NOT_FOUND,
    UNAVAILABLE,
    AgentId,
    agent_store,
)
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.background_tasks import BackgroundTasks, BackgroundTasksError
from forge_admin.embedding import ADK_WORKFLOWS, Embedding, EmbeddingError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ADK workflow runs"])

RUN = "agents:run"
APPROVE = "agents:approve"
# Who decides an approval, as its step names the approvers: the permission
# they hold in the organization. Anything else is the admins'.
APPROVERS = {"org:admin": APPROVE, "org:member": RUN}
NO_SUCH_RUN = "The organization has no such ADK workflow run"
QUEUE_UNAVAILABLE = "The async worker's queue isn't answering; try again shortly"
SESSIONS_UNAVAILABLE = (
    "The ADK workflow runs' sessions aren't answering; try again shortly"
)


class AdkRunCreate(BaseModel):
    """A run of an ADK workflow."""

    model_config = ConfigDict(extra="forbid")

    #: What the run starts with: it must fit the start's input schema.
    input: Any = None


class AdkRunStarted(BaseModel):
    """A run on its way: the job that will run it, and its ADK session."""

    queue: str
    key: str
    agent_id: str
    #: The revision it runs, whatever is saved afterwards.
    revision: int
    #: Its ADK session, where its steps are read from.
    session_id: str


class AdkRunStep(BaseModel):
    """One step of the run's ADK workflow, as the run has it so far."""

    id: str
    name: str
    kind: str
    #: done (with ``error`` too when it took its Error way), failed (the run
    #: with it), waiting (for a person or the timer), running, not_reached.
    status: Literal["done", "failed", "waiting", "running", "not_reached"]
    #: What it handed on (its last, in a loop's body).
    output: Any = None
    #: Why it failed, or the error it took its Error way with:
    #: ``{"message", ...}``.
    error: Any = None
    #: Its first event and the one that finished it, ISO 8601 in UTC.
    started_at: str | None = None
    finished_at: str | None = None


class AdkRunSteps(BaseModel):
    """A run's steps, in its document's order."""

    steps: list[AdkRunStep]
    session_id: str


class AnswerCreate(BaseModel):
    """An answer to the question (human input) a run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The question answered, as the run's ``approval`` names it.
    request_id: str = Field(min_length=1, max_length=64)
    #: The answer: it must fit the question's ``response_schema``.
    answer: Any


def adk_run_queue(request: Request) -> Embedding:
    """
    :return: The async worker's job queues, where runs are submitted.
    :raises HTTPException: 503 when they aren't set up.
    """
    embedding: Embedding | None = request.app.state.embedding
    if embedding is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADK workflow runs aren't set up: set FORGE_ADMIN_EMBEDDING_REDIS_URL",
        )
    return embedding


async def _adk_run(
    request: Request,
    session: AsyncSession,
    enforcer: Any,
    user: str,
    organization_id: str,
    task_id: str,
) -> tuple[BackgroundTasks, dict[str, Any]]:
    """
    Authorize the caller to read the organization, then read the run's task.

    :return: The background tasks client and the task.
    :raises HTTPException: 403 without organizations:read; 404 for a task that
        isn't one of the organization's ADK workflow runs; 503 when background
        tasks aren't set up or the worker is unavailable.
    """
    try:
        client, task = await organization_task(
            request, session, enforcer, user, organization_id, task_id
        )
    except HTTPException as error:
        if error.detail == NO_SUCH_TASK:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN) from None
        raise
    if task_type_of(task) != ADK_WORKFLOWS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    return client, task


def _open(task: dict[str, Any], request_id: str) -> dict[str, Any]:
    """
    :return: The details of the approval the run waits at, when it's the one named.
    :raises HTTPException: 409 when it isn't open (answered, or the run moved on).
    """
    approval = task.get("approval")
    if not isinstance(approval, dict) or approval.get("id") != request_id:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    details = approval.get("details")
    return details if isinstance(details, dict) else {}


async def _decide(
    client: BackgroundTasks,
    session: AsyncSession,
    user: str,
    task_id: str,
    *,
    request_id: str,
    approved: bool,
    comment: str,
) -> dict[str, Any]:
    try:
        return await client.decide(
            task_id,
            request_id=request_id,
            approved=approved,
            comment=comment,
            actor=await actor_of(session, user),
        )
    except BackgroundTasksError as error:
        raise refusal(error) from None


@router.post(
    "/organizations/{organization_id}/agents/{agent_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_adk_workflow(
    organization_id: NodeId,
    agent_id: AgentId,
    body: AdkRunCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRunStarted:
    """
    Run an ADK workflow, as it's saved now (with the saved ADK workflows it
    runs), as the caller. The run is one of the organization's background tasks.
    \f
    :param organization_id: The organization.
    :param agent_id: The ADK workflow.
    :param body: The input.
    :return: The job that will run it, and its ADK session.
    :raises HTTPException: 403 without agents:run in the organization; 404 when
        the organization has no such ADK workflow; 422 for input that doesn't
        fit its start, or a document that doesn't build (the reason naming the
        node); 503 when runs or MongoDB aren't set up or answering.
    """
    await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    store = agent_store(request)

    async def find(wanted: str) -> dict[str, Any] | None:
        return await store.get(organization_id, wanted)

    try:
        record = await find(agent_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        saved = await prepare_run(record, body.input, find)
    except AdkRunError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except PyMongoError as error:
        logger.warning("The agents' MongoDB failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    queue = adk_run_queue(request)
    try:
        started = await start_adk_run(
            queue,
            record,
            saved=saved,
            input=body.input,
            run_as=user,
            run_as_name=await name_of(session, user),
            trigger={"type": "manual", "by": user},
        )
    except EmbeddingError as error:
        logger.warning("Couldn't submit a run of ADK workflow %s: %s", agent_id, error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, QUEUE_UNAVAILABLE
        ) from None
    logger.info(
        "%s ran ADK workflow %s at revision %s in organization %s",
        user,
        agent_id,
        record["revision"],
        organization_id,
    )
    return AdkRunStarted(**started, agent_id=agent_id, revision=record["revision"])


@router.get("/organizations/{organization_id}/agents/{agent_id}/runs")
async def list_adk_workflow_runs(
    organization_id: NodeId,
    agent_id: AgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """
    List an ADK workflow's runs, newest first: the organization's background
    tasks that ran it, each with its status and whether it waits.
    \f
    :return: ``{"items": [...], "total"}``, as the organization's background tasks.
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when background tasks aren't set up or the worker is unavailable.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    client = background_tasks_client(request)
    try:
        return await client.tasks(
            tenant=organization_id,
            task_types=[ADK_WORKFLOWS],
            labels={AGENT_LABEL: agent_id},
            limit=limit,
            offset=offset,
        )
    except BackgroundTasksError as error:
        raise refusal(error) from None


@router.get("/organizations/{organization_id}/adk-runs/{task_id}/steps")
async def get_adk_run_steps(
    organization_id: NodeId,
    task_id: TaskId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRunSteps:
    """
    Read a run's steps from its ADK session: each step of the ADK workflow it
    runs, in order, with its status, what it handed on or why it failed, and
    when it started and finished.
    \f
    :return: ``{"steps": [...], "session_id"}``.
    :raises HTTPException: 403 without organizations:read in the organization;
        404 for a task that isn't one of its ADK workflow runs; 503 when
        background tasks aren't set up, or the worker or the sessions'
        database is unavailable.
    """
    _, task = await _adk_run(request, session, enforcer, user, organization_id, task_id)
    labels, payload = task.get("labels"), task.get("payload")
    session_id = labels.get(SESSION_LABEL) if isinstance(labels, dict) else None
    run_as = payload.get("run_as") if isinstance(payload, dict) else None
    document = payload.get("document") if isinstance(payload, dict) else None
    if not (
        isinstance(session_id, str)
        and isinstance(run_as, str)
        and isinstance(document, dict)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    sessions: BaseSessionService = request.app.state.adk_run_sessions
    try:
        found = await sessions.get_session(
            app_name=APP_NAME, user_id=run_as, session_id=session_id
        )
    except (SQLAlchemyError, OSError) as error:
        logger.warning("Couldn't read ADK session %s: %s", session_id, error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, SESSIONS_UNAVAILABLE
        ) from None
    steps = [AdkRunStep.model_validate(step) for step in run_steps(found, document)]
    return AdkRunSteps(steps=steps, session_id=session_id)


@router.post(
    "/organizations/{organization_id}/adk-runs/{task_id}/decisions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_adk_run(
    organization_id: NodeId,
    task_id: TaskId,
    body: DecisionCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Approve or reject the approval an ADK workflow run waits at: the run
    carries on down the step's approved or rejected way, on a worker, with who
    decided and why.
    \f
    :return: ``{"queue", "key"}`` of the job that will carry it on.
    :raises HTTPException: 403 without the permission the step asks of its
        approvers; 404 for a task that isn't one of the organization's ADK
        workflow runs; 409 when the approval isn't open, or it's a question
        (answered at ``.../answers``).
    """
    client, task = await _adk_run(
        request, session, enforcer, user, organization_id, task_id
    )
    details = _open(task, body.request_id)
    if details.get("kind") == "human_input":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for an answer, not a decision: send it to "
            f"/organizations/{organization_id}/adk-runs/{task_id}/answers",
        )
    permission = APPROVERS.get(str(details.get("approvers")), APPROVE)
    await authorize(
        session, enforcer, user, permission, Scope(Level.ORG, organization_id)
    )
    answer = await _decide(
        client,
        session,
        user,
        task_id,
        request_id=body.request_id,
        approved=body.approved,
        comment=body.comment.strip(),
    )
    logger.info(
        "%s %s ADK workflow run %s",
        user,
        "approved" if body.approved else "rejected",
        task_id,
    )
    return answer


@router.post(
    "/organizations/{organization_id}/adk-runs/{task_id}/answers",
    status_code=status.HTTP_202_ACCEPTED,
)
async def answer_adk_run(
    organization_id: NodeId,
    task_id: TaskId,
    body: AnswerCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> dict[str, Any]:
    """
    Answer the question (human input) an ADK workflow run waits at: the run
    carries on with the answer, on a worker.
    \f
    :return: ``{"queue", "key"}`` of the job that will carry it on.
    :raises HTTPException: 403 without agents:run in the organization; 404 for
        a task that isn't one of its ADK workflow runs; 409 when the question
        isn't open, or it's an approval (decided at ``.../decisions``); 422
        for an answer that doesn't fit what it asks.
    """
    client, task = await _adk_run(
        request, session, enforcer, user, organization_id, task_id
    )
    details = _open(task, body.request_id)
    if details.get("kind") != "human_input":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for a decision, not an answer: send it to "
            f"/organizations/{organization_id}/adk-runs/{task_id}/decisions",
        )
    await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    try:
        comment = checked_answer(details, body.answer)
    except AdkRunError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    answer = await _decide(
        client,
        session,
        user,
        task_id,
        request_id=body.request_id,
        approved=True,
        comment=comment,
    )
    logger.info("%s answered ADK workflow run %s", user, task_id)
    return answer
