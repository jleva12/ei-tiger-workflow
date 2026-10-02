"""
Runs of an organization's ADK workflows (``forge.agent/v1``, the agents of
``adk_workflows.py``): started, listed, read, decided, answered,
retried, resubmitted and abandoned here, straight from the run store
(``forge_task_adk_workflows.run_store``, in this API's database), with a job
queued on the async worker's ``adk_workflows`` queue whenever a run is to be
taken (``forge_admin.adk_workflows.runs``); their steps are read from their ADK
sessions.

- Running one needs ``agents:run`` in the organization (its admins and members
  by default). The run takes the ADK workflow as it's saved then, and acts as
  the member who ran it.
- Listing and reading runs, and their steps, needs ``organizations:read``.
- An approval a run waits at is decided by whom its step names:
  ``agents:approve`` (the organization's admins) when they're its admins,
  ``agents:run`` (every member) when any member may.
- A question (human input) is answered by anyone with ``agents:run``; the
  answer must fit what it asks (its ``response_schema``), and is kept as the
  decision's comment, as JSON.
- Retrying, resubmitting and abandoning one needs ``agents:manage_runs`` (the
  organization's admins).

A run is found only through its organization: any other's answers 404. Times
are ISO 8601 in UTC, with their offset (``+00:00``).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from forge_task_adk_workflows.run_store import (
    ABANDONABLE,
    FAILED,
    FINISHED,
    PAUSED,
    Actor,
    NoSuchRun,
    NotAllowed,
    RunStore,
)
from forge_task_adk_workflows.steps import run_steps
from google.adk.sessions import BaseSessionService
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.adk_workflows.documents import ID_PATTERN
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.adk_workflows.runs import (
    APP_NAME,
    AdkRunError,
    checked_answer,
    prepare_run,
    queue_run,
    start_adk_run,
)
from forge_admin.api.routes.adk_workflows import (
    NOT_FOUND,
    UNAVAILABLE,
    AgentId,
    agent_store,
)
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ADK workflow runs"])

READ = "organizations:read"
RUN = "agents:run"
APPROVE = "agents:approve"
MANAGE_RUNS = "agents:manage_runs"
# Who decides an approval, as its step names the approvers: the permission
# they hold in the organization. Anything else is the admins'.
APPROVERS = {"org:admin": APPROVE, "org:member": RUN}
HUMAN_INPUT = "human_input"
NO_SUCH_RUN = "The organization has no such ADK workflow run"
NOT_OPEN = "The run doesn't wait at that approval or question any more"
RUNS_UNAVAILABLE = "The ADK workflow runs' database isn't answering; try again shortly"
SESSIONS_UNAVAILABLE = (
    "The ADK workflow runs' sessions aren't answering; try again shortly"
)

# A run's ID: the run store's are 32 hex digits. Anything else of this shape
# is no run (404), so an old link answers as a run that's gone.
RunId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
RunStatus = Literal[
    "queued", "running", "paused", "waiting", "succeeded", "failed", "abandoned"
]


class AdkRunCreate(BaseModel):
    """A run of an ADK workflow."""

    model_config = ConfigDict(extra="forbid")

    #: What the run starts with: it must fit the start's input schema.
    input: Any = None


class DecisionCreate(BaseModel):
    """A decision on the approval an ADK workflow run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The approval decided, as the run's ``pause`` names it (its ``id``): a
    #: decision never lands on a later approval of the same run.
    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    #: Why, for the record; the run's later steps can read it.
    comment: str = Field(default="", max_length=4000)


class AnswerCreate(BaseModel):
    """An answer to the question (human input) a run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The question answered, as the run's ``pause`` names it (its ``id``).
    request_id: str = Field(min_length=1, max_length=64)
    #: The answer: it must fit the question's ``response_schema``.
    answer: Any


class RunActor(BaseModel):
    """Who started a run, or did something to it."""

    id: str | None
    name: str


class RunPause(BaseModel):
    """What a paused run waits for: a person's decision or answer."""

    #: Names it in a decision or answer (``request_id``).
    id: str
    #: ``approval`` (decided) or ``human_input`` (a question, answered).
    kind: str
    reason: str
    #: The step's: ``kind``, ``approvers``, ``expires_at``, ``step``,
    #: ``step_name``, ``workflow_name``, ``agent_id``, ``message``,
    #: ``session_id``, ``interrupt_id``; a question's ``response_schema`` too.
    details: dict[str, Any]
    requested_at: str
    #: When nobody deciding rejects it; null for never.
    deadline: str | None


class RunFailure(BaseModel):
    """Why a run failed, or the last hiccup it was queued again after."""

    message: str
    category: Literal["failed", "error", "transient", "interrupted"]
    step: str | None
    occurred_at: str


class AdkRun(BaseModel):
    """An ADK workflow run, as lists show it."""

    id: str
    organization_id: str
    agent_id: str
    agent_name: str
    #: The ADK workflow's revision it runs, whatever is saved afterwards.
    revision: int
    #: Its ADK session, where its steps are read from.
    session_id: str
    status: RunStatus
    #: 1, and one more each time it's retried or queued again after a hiccup.
    attempt: int
    requested_by: RunActor
    #: The run it was resubmitted from.
    resubmit_of: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    #: From its start to its finish, once it has both.
    duration_ms: int | None
    #: A waiting run's time, and why.
    waiting_until: str | None
    waiting_reason: str | None
    pause: RunPause | None
    error: RunFailure | None


class AdkRunPage(BaseModel):
    """A page of runs, newest first, and how many match."""

    items: list[AdkRun]
    total: int


class RunEvent(BaseModel):
    """A line of a run's activity."""

    id: int
    at: str
    #: created, started, resumed, note, paused, decided, answered, declined,
    #: timed_out, waiting, succeeded, failed, retried, recovered, abandoned.
    kind: str
    message: str
    #: Who, when a person did it.
    actor: RunActor | None
    attributes: dict[str, Any] | None


class RunActions(BaseModel):
    """What the run's status allows (permissions aside)."""

    retry: bool
    resubmit: bool
    abandon: bool
    decide: bool
    answer: bool


class AdkRunDetail(AdkRun):
    """An ADK workflow run with what it runs, its result and its activity."""

    input: Any = None
    #: The graph's result, once it ended (succeeded, or failed with one).
    result: Any = None
    #: The ADK workflow as it ran.
    document: dict[str, Any] | None
    #: What started it: ``{"type": "manual", "by": <user>}``.
    trigger: dict[str, Any] | None
    #: Oldest first.
    events: list[RunEvent]
    actions: RunActions


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


# ---------------------------------------------------------------- reading runs


def _iso(when: datetime | None) -> str | None:
    return when.astimezone(UTC).isoformat() if when is not None else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _pause(run: dict[str, Any]) -> RunPause | None:
    pause = run.get("pause")
    if run["status"] != PAUSED or not isinstance(pause, dict):
        return None
    details = pause.get("details")
    return RunPause(
        id=str(pause.get("id") or ""),
        kind=str(pause.get("kind") or ""),
        reason=str(pause.get("reason") or ""),
        details=details if isinstance(details, dict) else {},
        requested_at=str(pause.get("requested_at") or ""),
        deadline=_text(pause.get("deadline")),
    )


def _failure(run: dict[str, Any]) -> RunFailure | None:
    error = run.get("error")
    if not isinstance(error, dict):
        return None
    category = error.get("category")
    return RunFailure(
        message=str(error.get("message") or ""),
        category=category
        if category in ("failed", "error", "transient", "interrupted")
        else "error",
        step=_text(error.get("step")),
        occurred_at=str(error.get("occurred_at") or ""),
    )


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    started, finished = run.get("started_at"), run.get("finished_at")
    duration = (
        (finished - started) // timedelta(milliseconds=1)
        if started is not None and finished is not None
        else None
    )
    return {
        "id": run["id"],
        "organization_id": run["organization_id"],
        "agent_id": run["agent_id"],
        "agent_name": run["agent_name"],
        "revision": run["revision"],
        "session_id": run["session_id"],
        "status": run["status"],
        "attempt": run["attempt"],
        "requested_by": RunActor(
            id=run["requested_by"] or None, name=run["requested_by_name"]
        ),
        "resubmit_of": run.get("resubmit_of"),
        "created_at": _iso(run["created_at"]),
        "updated_at": _iso(run["updated_at"]),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_ms": duration,
        "waiting_until": _iso(run.get("waiting_until")),
        "waiting_reason": run.get("waiting_reason"),
        "pause": _pause(run),
        "error": _failure(run),
    }


def as_run(run: dict[str, Any]) -> AdkRun:
    """:return: A run as the store has it (all of it, or a list's summary), as lists show it."""
    return AdkRun.model_validate(_summary(run))


def actions_of(run: dict[str, Any]) -> RunActions:
    """:return: What the run's status allows."""
    current = run["status"]
    pause = _pause(run)
    return RunActions(
        retry=current == FAILED,
        resubmit=current in FINISHED,
        abandon=current in ABANDONABLE,
        decide=pause is not None and pause.kind != HUMAN_INPUT,
        answer=pause is not None and pause.kind == HUMAN_INPUT,
    )


def as_detail(run: dict[str, Any], events: list[dict[str, Any]]) -> AdkRunDetail:
    """:return: A run as the store has it, with its activity, as its page shows it."""
    payload = run.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    document, trigger = payload.get("document"), payload.get("trigger")
    return AdkRunDetail.model_validate(
        {
            **_summary(run),
            "input": payload.get("input"),
            "result": run.get("result"),
            "document": document if isinstance(document, dict) else None,
            "trigger": trigger if isinstance(trigger, dict) else None,
            "events": [
                RunEvent(
                    id=event["id"],
                    at=_iso(event["at"]) or "",
                    kind=event["kind"],
                    message=event["message"],
                    actor=RunActor(id=event["actor_id"], name=event["actor_name"] or "")
                    if event["actor_id"] or event["actor_name"]
                    else None,
                    attributes=event["attributes"],
                )
                for event in events
            ],
            "actions": actions_of(run),
        }
    )


# ---------------------------------------------------------------- the store


def run_store(request: Request) -> RunStore:
    """:return: The ADK workflow runs, in this API's database."""
    store: RunStore = request.app.state.adk_runs
    return store


def run_queue(request: Request) -> Embedding:
    """
    :return: The async worker's job queues, where runs are taken from.
    :raises HTTPException: 503 when they aren't set up.
    """
    embedding: Embedding | None = request.app.state.embedding
    if embedding is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADK workflow runs aren't set up: set FORGE_ADMIN_EMBEDDING_REDIS_URL",
        )
    return embedding


@contextmanager
def store_errors() -> Iterator[None]:
    """
    Answer the run store's refusals: 404 for no such run (in the
    organization), 409 when the run's status doesn't allow it, 503 when its
    database isn't answering.
    """
    try:
        yield
    except NoSuchRun:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN) from None
    except NotAllowed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    except (SQLAlchemyError, OSError) as error:
        logger.warning("The ADK workflow runs' database failed: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, RUNS_UNAVAILABLE
        ) from None


async def _read(
    request: Request,
    session: AsyncSession,
    enforcer: Any,
    user: str,
    organization_id: str,
    run_id: str,
) -> dict[str, Any]:
    """
    Authorize the caller to read the organization, then read the run.

    :return: The run, all of it.
    :raises HTTPException: 403 without organizations:read; 404 for a run that
        isn't the organization's; 503 when the run store isn't answering.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    with store_errors():
        run = await run_store(request).get(run_id, organization_id=organization_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    return run


def _waits_at(run: dict[str, Any], request_id: str) -> RunPause:
    """
    :return: The pause the run waits at, when it's the one named.
    :raises HTTPException: 409 when it isn't (decided, or the run moved on).
    """
    pause = _pause(run)
    if pause is None or pause.id != request_id:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    return pause


async def _actor(session: AsyncSession, user: str) -> Actor:
    return Actor(user, await name_of(session, user))


# ---------------------------------------------------------------- routes


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
) -> AdkRun:
    """
    Run an ADK workflow, as it's saved now (with the saved ADK workflows it
    runs), as the caller: the run, queued for a worker.
    \f
    :param organization_id: The organization.
    :param agent_id: The ADK workflow.
    :param body: The input.
    :return: The run.
    :raises HTTPException: 403 without agents:run in the organization; 404 when
        the organization has no such ADK workflow; 422 for input that doesn't
        fit its start, or a document that doesn't build (the reason naming the
        node); 503 when runs or MongoDB aren't set up or answering.
    """
    await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    store = agent_store(request)
    queue = run_queue(request)

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
    run_as_name = await name_of(session, user)
    with store_errors():
        run = await start_adk_run(
            run_store(request),
            queue,
            record,
            saved=saved,
            input=body.input,
            run_as=user,
            run_as_name=run_as_name,
            trigger={"type": "manual", "by": user},
        )
    logger.info(
        "%s ran ADK workflow %s at revision %s in organization %s: run %s",
        user,
        agent_id,
        record["revision"],
        organization_id,
        run["id"],
    )
    return as_run(run)


@router.get("/organizations/{organization_id}/adk-runs")
async def list_adk_runs(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    agent_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
    status_filter: Annotated[list[RunStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdkRunPage:
    """
    List the organization's ADK workflow runs, newest first: all of them, or
    one ADK workflow's, in any status or some.
    \f
    :param agent_id: Only this ADK workflow's runs.
    :param status_filter: Only runs in these statuses (``?status=`` repeated).
    :return: ``{"items": [...], "total"}``: the page, and how many match.
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when the run store isn't answering.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    with store_errors():
        items, total = await run_store(request).page(
            organization_id,
            agent_id=agent_id,
            statuses=status_filter,
            limit=limit,
            offset=offset,
        )
    return AdkRunPage(items=[as_run(run) for run in items], total=total)


@router.get("/organizations/{organization_id}/adk-runs/{run_id}")
async def get_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRunDetail:
    """
    Read one ADK workflow run: what it runs and with what input, where it is,
    what it waits for, its result or failure, its activity, and the actions
    its status allows.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        404 for a run that isn't the organization's; 503 when the run store
        isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    with store_errors():
        events = await run_store(request).events(run_id)
    return as_detail(run, events)


@router.get("/organizations/{organization_id}/adk-runs/{run_id}/steps")
async def get_adk_run_steps(
    organization_id: NodeId,
    run_id: RunId,
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
        404 for a run that isn't the organization's; 503 when the run store
        or the sessions' database isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    payload = run.get("payload")
    run_as = payload.get("run_as") if isinstance(payload, dict) else None
    document = payload.get("document") if isinstance(payload, dict) else None
    session_id = run["session_id"]
    if not (isinstance(run_as, str) and isinstance(document, dict)):
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
    "/organizations/{organization_id}/adk-runs/{run_id}/decisions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    body: DecisionCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Approve or reject the approval an ADK workflow run waits at: the run is
    queued to carry on down the step's approved or rejected way, with who
    decided and why.
    \f
    :return: The run, queued.
    :raises HTTPException: 403 without the permission the step asks of its
        approvers; 404 for a run that isn't the organization's; 409 when the
        approval isn't open, or it's a question (answered at ``.../answers``).
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    pause = _waits_at(run, body.request_id)
    if pause.kind == HUMAN_INPUT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for an answer, not a decision: send it to "
            f"/organizations/{organization_id}/adk-runs/{run_id}/answers",
        )
    permission = APPROVERS.get(str(pause.details.get("approvers")), APPROVE)
    await authorize(
        session, enforcer, user, permission, Scope(Level.ORG, organization_id)
    )
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        decided = await run_store(request).decide(
            run_id,
            request_id=body.request_id,
            approved=body.approved,
            comment=body.comment.strip(),
            actor=actor,
            organization_id=organization_id,
        )
    await queue_run(queue, run_id)
    logger.info(
        "%s %s ADK workflow run %s",
        user,
        "approved" if body.approved else "rejected",
        run_id,
    )
    return as_run(decided)


@router.post(
    "/organizations/{organization_id}/adk-runs/{run_id}/answers",
    status_code=status.HTTP_202_ACCEPTED,
)
async def answer_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    body: AnswerCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Answer the question (human input) an ADK workflow run waits at: the run
    is queued to carry on with the answer.
    \f
    :return: The run, queued.
    :raises HTTPException: 403 without agents:run in the organization; 404 for
        a run that isn't the organization's; 409 when the question isn't open,
        or it's an approval (decided at ``.../decisions``); 422 for an answer
        that doesn't fit what it asks.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    pause = _waits_at(run, body.request_id)
    if pause.kind != HUMAN_INPUT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for a decision, not an answer: send it to "
            f"/organizations/{organization_id}/adk-runs/{run_id}/decisions",
        )
    await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    try:
        comment = checked_answer(pause.details, body.answer)
    except AdkRunError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        answered = await run_store(request).decide(
            run_id,
            request_id=body.request_id,
            approved=True,
            comment=comment,
            actor=actor,
            organization_id=organization_id,
        )
    await queue_run(queue, run_id)
    logger.info("%s answered ADK workflow run %s", user, run_id)
    return as_run(answered)


@router.post(
    "/organizations/{organization_id}/adk-runs/{run_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Retry a failed ADK workflow run as its next attempt: it carries on from
    where it was, on a worker.
    \f
    :return: The run, queued.
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 unless it failed.
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        retried = await run_store(request).retry(
            run_id, actor=actor, organization_id=organization_id
        )
    await queue_run(queue, run_id)
    logger.info("%s retried ADK workflow run %s", user, run_id)
    return as_run(retried)


@router.post(
    "/organizations/{organization_id}/adk-runs/{run_id}/resubmit",
    status_code=status.HTTP_202_ACCEPTED,
)
async def resubmit_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Run a finished ADK workflow run's ADK workflow again, as it ran (the same
    documents and input, another invocation of its ADK session), as a new run.
    \f
    :return: The new run, queued.
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 unless it's finished.
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        again = await run_store(request).resubmit(
            run_id, actor=actor, organization_id=organization_id
        )
    await queue_run(queue, again["id"])
    logger.info("%s resubmitted ADK workflow run %s as %s", user, run_id, again["id"])
    return as_run(again)


@router.post("/organizations/{organization_id}/adk-runs/{run_id}/abandon")
async def abandon_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Give an ADK workflow run up: queued, paused, waiting or failed, it never
    carries on.
    \f
    :return: The run, abandoned.
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 while it's running,
        or once it's finished (but failed).
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    actor = await _actor(session, user)
    with store_errors():
        abandoned = await run_store(request).abandon(
            run_id, actor=actor, organization_id=organization_id
        )
    logger.info("%s abandoned ADK workflow run %s", user, run_id)
    return as_run(abandoned)
