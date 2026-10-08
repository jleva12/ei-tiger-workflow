"""
The organizations' workflows for outside apps, over REST
(``{api_prefix}/runtime/workflows``; A2A is ``a2a.py``):

- ``GET /workflows/{ref}``: the workflow at that version: its name, what it
  does, the input (and files) its start takes, and where to run it.
- ``POST /workflows/{ref}/runs``: run it with an input, and files when its
  start takes them (a multipart form, or base64 in JSON; ``files.py``);
  ``wait`` seconds for it to pause or end (``workflow_runtime_wait`` at
  most), else answer at once.
- ``GET /workflows/{ref}/runs/{run}``: the run, waiting as long again.
- ``POST …/answers``: answer the question it waits at; ``…/decisions``:
  approve or reject the approval (or tool call) it waits at; ``…/cancel``:
  give it up.

``{ref}`` is ``ag_x`` (its latest published version), ``ag_x@3`` or
``ag_x@draft`` (``versions.py``, outside rules). Who may call is
``forge_admin.auth.runtime_access``'s rule: anyone when the runtime is
public, else an API key or sign-in with agents:run in the workflow's
organization. A run is its starter's to read, answer and cancel (an
anonymous one is anyone's who has its ID); others need organizations:read,
agents:run, or agents:manage_runs there. Approvals are only ever decided by
callers holding what the step asks of its approvers: never anonymous ones.
Anything else answers 404, so runs don't leak.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from forge_task_adk_workflows.files import FILE_TYPES, RunFile, files_rule
from forge_task_adk_workflows.run_store import ABANDONABLE, PAUSED, Actor
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError
from starlette.routing import NoMatchFound

from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.files import (
    FileContent,
    Limits,
    SentFile,
    artifacts_of,
    read_run_body,
    run_body,
    run_files,
    storage_errors,
)
from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import (
    AdkRunError,
    checked_answer,
    prepare_run,
    queue_run,
    settle,
    start_adk_run,
    start_schema,
)
from forge_admin.adk_workflows.versions import (
    ResolvedWorkflow,
    WorkflowNotFound,
    resolve_workflow,
    workflow_finder,
)
from forge_admin.api.routes.adk_workflow_runs import (
    APPROVE,
    APPROVERS,
    HUMAN_INPUT,
    MANAGE_RUNS,
    READ,
    RUN,
    run_queue,
    run_store,
    store_errors,
    version_of,
)
from forge_admin.api.routes.adk_workflows import UNAVAILABLE, agent_store
from forge_admin.api.routes.common import name_of
from forge_admin.auth.access import Level, Scope, authorize
from forge_admin.auth.runtime_access import Caller, RuntimeCaller, authorize_call
from forge_admin.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflow runtime"])

#: The ADK session user of runs no one known started.
ANONYMOUS = "anonymous"
NO_SUCH_RUN = "There's no such run of this workflow"
NOT_OPEN = "The run doesn't wait at that approval or question any more"

WorkflowRef = Annotated[
    str,
    Path(
        pattern=r"^ag_[a-z0-9]{6,40}(@(draft|[1-9][0-9]*))?$",
        description="ag_x (its latest published version), ag_x@3 or ag_x@draft",
    ),
]
RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Wait = Annotated[
    float,
    Field(
        default=0,
        ge=0,
        description="Seconds to wait for the run to pause or end before answering "
        "(at most the server's limit); 0 answers at once.",
    ),
]


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What the run starts with: it must fit the start's input schema.
    input: Any = None
    wait: Wait = 0
    #: The files it starts with, when its start takes files; a multipart
    #: form sends them as ``files`` parts instead.
    files: list[FileContent] = []


class AnswerSend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The question answered, as the run's ``pause`` names it.
    request_id: str = Field(min_length=1, max_length=64)
    #: It must fit the question's ``response_schema``.
    answer: Any
    wait: Wait = 0


class DecisionSend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The approval (or tool call) decided, as the run's ``pause`` names it.
    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    comment: str = Field(default="", max_length=4000)
    wait: Wait = 0


class FilesTaken(BaseModel):
    """The files a workflow's start takes."""

    #: Whether a run may start with files.
    allowed: bool
    #: Their types (``pdf``, ``word``, …), each with its extensions; none
    #: takes any type.
    types: dict[str, list[str]]
    #: The most files a run starts with, and bytes each may be.
    max_count: int
    max_bytes: int


class WorkflowInfo(BaseModel):
    """A workflow at a version, as outside apps call it."""

    id: str
    #: ``ag_x@3`` or ``ag_x@draft``: the version this is.
    ref: str
    version: int | Literal["draft"]
    name: str
    description: str
    #: The JSON Schema of the input its start takes; ``{}`` takes anything.
    input_schema: dict[str, Any]
    #: The files it takes.
    files: FilesTaken
    runs_url: str
    a2a_card_url: str


class RuntimePause(BaseModel):
    """What a run waits for."""

    #: Names it in an answer or decision (``request_id``).
    id: str
    #: ``human_input`` (a question: send ``…/answers``) or ``approval``
    #: (send ``…/decisions``), a tool call's confirmation among them.
    kind: str
    #: Whether it's a tool call waiting to be confirmed.
    confirmation: bool
    reason: str
    #: The step it waits at, by name.
    step: str | None
    #: What a question's answer must fit.
    response_schema: dict[str, Any] | None = None
    #: A confirmation's tool, and what the model wants to send it.
    tool: str | None = None
    args: dict[str, Any] | None = None
    #: Who decides an approval: ``org:admin``, ``org:member``.
    approvers: str | None = None
    #: When nobody deciding rejects it; null for never.
    deadline: str | None


class RuntimeFailure(BaseModel):
    message: str
    category: str
    step: str | None


class RuntimeRun(BaseModel):
    """A run, as outside apps follow it."""

    id: str
    workflow_id: str
    workflow_name: str
    #: Which version ran: a published one's number, or "draft".
    version: int | Literal["draft"] | None
    #: queued, running, paused (see ``pause``), waiting (a timer), succeeded,
    #: failed or abandoned.
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    #: A waiting run's time, and why.
    waiting_until: str | None
    waiting_reason: str | None
    #: What it ended with.
    result: Any = None
    error: RuntimeFailure | None
    pause: RuntimePause | None
    #: The files it started with.
    files: list[RunFile] = []
    #: Where to follow it, and act on it: ``self``, ``answers``,
    #: ``decisions``, ``cancel``.
    links: dict[str, str]


def _iso(value: Any) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def runtime_pause(run: dict[str, Any]) -> RuntimePause | None:
    """:return: What a paused run waits for, as outside apps see it."""
    pause = run.get("pause")
    if run.get("status") != PAUSED or not isinstance(pause, dict):
        return None
    details = pause.get("details")
    details = details if isinstance(details, dict) else {}
    schema, args = details.get("response_schema"), details.get("args")
    return RuntimePause(
        id=str(pause.get("id") or ""),
        kind=str(pause.get("kind") or ""),
        confirmation=bool(details.get("confirmation")),
        reason=str(pause.get("reason") or ""),
        step=details.get("step_name") or details.get("step"),
        response_schema=schema if isinstance(schema, dict) else None,
        tool=details.get("tool") if details.get("confirmation") else None,
        args=args if isinstance(args, dict) else None,
        approvers=details.get("approvers")
        if pause.get("kind") != HUMAN_INPUT
        else None,
        deadline=pause.get("deadline")
        if isinstance(pause.get("deadline"), str)
        else None,
    )


def runtime_run(request: Request, run: dict[str, Any]) -> RuntimeRun:
    """:return: A run as outside apps follow it, with where to act on it."""
    base = str(
        request.url_for("get_workflow_run", ref=run["agent_id"], run_id=run["id"])
    )
    links = {"self": base}
    pause = runtime_pause(run)
    if pause is not None:
        links["answers" if pause.kind == HUMAN_INPUT else "decisions"] = (
            f"{base}/{'answers' if pause.kind == HUMAN_INPUT else 'decisions'}"
        )
    if run["status"] in ABANDONABLE:
        links["cancel"] = f"{base}/cancel"
    error = run.get("error")
    return RuntimeRun(
        id=run["id"],
        workflow_id=run["agent_id"],
        workflow_name=run["agent_name"],
        version=version_of(run.get("version")),
        status=run["status"],
        created_at=_iso(run["created_at"]) or "",
        started_at=_iso(run.get("started_at")),
        finished_at=_iso(run.get("finished_at")),
        waiting_until=_iso(run.get("waiting_until")),
        waiting_reason=run.get("waiting_reason"),
        result=run.get("result"),
        error=RuntimeFailure(
            message=str(error.get("message") or ""),
            category=str(error.get("category") or "error"),
            step=error.get("step"),
        )
        if isinstance(error, dict)
        else None,
        pause=pause,
        files=run_files(run.get("payload")),
        links=links,
    )


# ------------------------------------------------------------ shared with A2A


def workflows_of(request: Request) -> AgentStore:
    """:raises HTTPException: 503 when MongoDB isn't set up."""
    return agent_store(request)


@contextmanager
def mongo_errors() -> Iterator[None]:
    try:
        yield
    except PyMongoError as error:
        logger.warning("The agents' MongoDB failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


async def found_workflow(request: Request, ref: str) -> ResolvedWorkflow:
    """
    The workflow at the version named, once the caller may call it.

    :raises HTTPException: 404 for no such workflow or version (or none
        published, with how to run its draft); 401/403 as
        ``authorize_call`` says; 503.
    """
    with mongo_errors():
        try:
            resolved = await resolve_workflow(
                workflows_of(request), ref, organization_id=None, outside=True
            )
        except WorkflowNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
    await authorize_call(request, resolved.record["organization_id"])
    return resolved


def wait_of(settings: Settings, wanted: float) -> float:
    """:return: How long to wait: what was asked, up to the server's limit."""
    return max(0.0, min(wanted, settings.workflow_runtime_wait))


async def holds(
    request: Request, caller: RuntimeCaller, organization_id: str, permission: str
) -> bool:
    """
    :return: Whether a known caller holds a permission in the organization,
        whatever the runtime's public setting (an API key only in its own).
    """
    if caller.subject is None:
        return False
    if caller.organization_id is not None and caller.organization_id != organization_id:
        return False
    async with request.app.state.sessionmaker() as session:
        try:
            await authorize(
                session,
                request.app.state.enforcer,
                caller.subject,
                permission,
                Scope(Level.ORG, organization_id),
            )
        except HTTPException:
            return False
    return True


def started_by(run: dict[str, Any], caller: RuntimeCaller) -> bool:
    """:return: Whether the caller started the run (an anonymous run: anyone anonymous)."""
    return run.get("requested_by") == (caller.subject or ANONYMOUS)


async def may(
    request: Request, caller: RuntimeCaller, run: dict[str, Any], permission: str
) -> bool:
    """:return: Whether the caller may act on the run: its starter, or holding the permission."""
    return started_by(run, caller) or await holds(
        request, caller, run["organization_id"], permission
    )


async def caller_actor(request: Request, caller: RuntimeCaller) -> Actor:
    """:return: Who the caller is, for a run's record: a key or person by name."""
    if caller.subject is None:
        return Actor(ANONYMOUS, "Anonymous caller")
    async with request.app.state.sessionmaker() as session:
        return Actor(caller.subject, await name_of(session, caller.subject))


async def start_runtime_run(
    request: Request,
    caller: RuntimeCaller,
    resolved: ResolvedWorkflow,
    input: Any,
    *,
    protocol: Literal["rest", "a2a"],
    run_id: str | None = None,
    trigger: dict[str, Any] | None = None,
    files: list[SentFile] | None = None,
) -> dict[str, Any]:
    """
    Start a run of a workflow for an outside caller, as them, with the files
    it's sent saved as artifacts of its session.

    :raises HTTPException: 422 for input or a file that doesn't fit, or a
        workflow that doesn't build (naming the node); 503.
    """
    record = resolved.run_record
    organization_id = record["organization_id"]
    artifacts = artifacts_of(request, files or [])
    queue = run_queue(request)
    with mongo_errors():
        try:
            prepared = await prepare_run(
                record,
                input,
                workflow_finder(workflows_of(request), organization_id),
                RunResources(
                    organization_id,
                    chat_agents=getattr(request.app.state, "chat_agents", None),
                    sessions=request.app.state.sessionmaker,
                ),
                files,
            )
        except AdkRunError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
            ) from None
    actor = await caller_actor(request, caller)
    with store_errors(), storage_errors():
        run = await start_adk_run(
            run_store(request),
            queue,
            record,
            prepared=prepared,
            input=input,
            run_as=actor.id or ANONYMOUS,
            run_as_name=actor.name,
            trigger={
                "type": "runtime",
                "protocol": protocol,
                "caller": caller.subject,
                "ref": resolved.ref,
                **(trigger or {}),
            },
            version=resolved.version,
            run_id=run_id,
            artifacts=artifacts,
        )
    logger.info(
        "%s ran workflow %s over %s with %d files: run %s",
        actor.id,
        resolved.ref,
        protocol,
        len(files or []),
        run["id"],
    )
    return run


async def answer_run(
    request: Request,
    caller: RuntimeCaller,
    run: dict[str, Any],
    request_id: str,
    answer: Any,
) -> dict[str, Any]:
    """
    Answer the question a run waits at, and queue it to carry on.

    :raises HTTPException: 404 when the caller may not; 409 when the question
        isn't open, or it's an approval; 422 for an answer that doesn't fit.
    """
    if not await may(request, caller, run, RUN):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    pause = runtime_pause(run)
    if pause is None or pause.id != request_id:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    if pause.kind != HUMAN_INPUT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for a decision, not an answer: send it to …/decisions",
        )
    try:
        comment = checked_answer(run["pause"].get("details") or {}, answer)
    except AdkRunError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    queue = run_queue(request)
    actor = await caller_actor(request, caller)
    with store_errors():
        answered = await run_store(request).decide(
            run["id"],
            request_id=request_id,
            approved=True,
            comment=comment,
            actor=actor,
        )
    await queue_run(queue, run["id"])
    return answered


async def decide_run(
    request: Request,
    caller: RuntimeCaller,
    run: dict[str, Any],
    request_id: str,
    approved: bool,
    comment: str,
) -> dict[str, Any]:
    """
    Decide the approval (or tool call) a run waits at, and queue it to carry on.

    :raises HTTPException: 404 when the caller may not read it; 403 for an
        anonymous caller, or one without what the step asks of its
        approvers; 409 when it isn't open, or it's a question.
    """
    if not await may(request, caller, run, READ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    pause = runtime_pause(run)
    if pause is None or pause.id != request_id:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    if pause.kind == HUMAN_INPUT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The run waits for an answer, not a decision: send it to …/answers",
        )
    if caller.subject is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Approvals are decided by the organization's members or its API "
            "keys, never anonymously: send one",
        )
    permission = APPROVERS.get(str(pause.approvers), APPROVE)
    if not await holds(request, caller, run["organization_id"], permission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {permission}")
    queue = run_queue(request)
    actor = await caller_actor(request, caller)
    with store_errors():
        decided = await run_store(request).decide(
            run["id"],
            request_id=request_id,
            approved=approved,
            comment=comment.strip(),
            actor=actor,
        )
    await queue_run(queue, run["id"])
    return decided


# ------------------------------------------------------------ routes


async def _run_of(
    request: Request, caller: RuntimeCaller, ref: str, run_id: str
) -> dict[str, Any]:
    """
    One of a workflow's runs, once the caller may call the workflow and read it.

    :raises HTTPException: 404 for no such run of it, or one the caller may
        not read.
    """
    workflow_id = ref.partition("@")[0]
    with store_errors():
        run = await run_store(request).get(run_id)
    if run is None or run["agent_id"] != workflow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    await authorize_call(request, run["organization_id"])
    if not await may(request, caller, run, READ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    return run


async def _settled(
    request: Request, run: dict[str, Any], wait: float
) -> dict[str, Any]:
    if wait <= 0:
        return run
    with store_errors():
        settled = await settle(
            run_store(request),
            run["id"],
            wait=wait_of(request.app.state.settings, wait),
            is_disconnected=request.is_disconnected,
        )
    return settled or run


def _url(request: Request, name: str, **params: str) -> str:
    return str(request.url_for(name, **params))


@router.get("/{ref}", summary="A workflow, as outside apps call it")
async def get_workflow(request: Request, ref: WorkflowRef) -> WorkflowInfo:
    """
    The workflow at the version named: its name, what it does, and the input
    its start takes.
    \f
    :raises HTTPException: 404 for no such workflow or version.
    """
    resolved = await found_workflow(request, ref)
    document = resolved.document
    workflow_id = resolved.record["_id"]
    return WorkflowInfo(
        id=workflow_id,
        ref=resolved.ref,
        version=resolved.version,
        name=str(document.get("name") or workflow_id),
        description=str(document.get("description") or ""),
        input_schema=start_schema(document) or {},
        files=files_taken(request, document),
        runs_url=_url(request, "start_workflow_run", ref=ref),
        a2a_card_url=_card_url(request, ref),
    )


def files_taken(request: Request, document: dict[str, Any]) -> FilesTaken:
    """:return: The files a workflow's start takes, and how many and big."""
    rule = files_rule(document)
    limits = Limits.of(request.app.state.settings)
    return FilesTaken(
        allowed=rule.allowed,
        types={
            t: [ext.removeprefix(".") for ext in FILE_TYPES[t].extensions]
            for t in rule.types
            if t in FILE_TYPES
        },
        max_count=limits.count,
        max_bytes=limits.size,
    )


def _card_url(request: Request, ref: str) -> str:
    """:return: Where its A2A card is; empty when the app serves no A2A."""
    try:
        return _url(request, "a2a_card", app_name=ref)
    except NoMatchFound:
        return ""


@router.post(
    "/{ref}/runs",
    status_code=status.HTTP_201_CREATED,
    summary="Run a workflow",
    openapi_extra=run_body(
        RunStart,
        "The input, how long to wait, and the files it starts with when its "
        "start takes files: JSON (files base64), or a multipart form with the "
        "input as JSON text and each file a files part.",
    ),
)
async def start_workflow_run(
    request: Request,
    response: Response,
    ref: WorkflowRef,
    caller: Caller,
) -> RuntimeRun:
    """
    Run the workflow at the version named, as the caller, with an input that
    fits its start, and files when it takes them. With ``wait``, the answer
    comes once the run pauses or ends, or the wait runs out.
    \f
    :raises HTTPException: 404; 413 for a file too big; 422 for input or a
        file that doesn't fit, or a workflow that doesn't build; 503 when runs
        or files aren't set up.
    """
    resolved = await found_workflow(request, ref)
    body, files = await read_run_body(
        request, RunStart, Limits.of(request.app.state.settings)
    )
    run = await start_runtime_run(
        request, caller, resolved, body.input, protocol="rest", files=files
    )
    run = await _settled(request, run, body.wait)
    shown = runtime_run(request, run)
    response.headers["Location"] = shown.links["self"]
    return shown


@router.get("/{ref}/runs/{run_id}", summary="Follow a run")
async def get_workflow_run(
    request: Request,
    ref: WorkflowRef,
    run_id: RunId,
    caller: Caller,
    wait: Annotated[float, Query(ge=0)] = 0,
) -> RuntimeRun:
    """
    One of the workflow's runs. With ``wait``, the answer comes once it
    pauses or ends, or the wait runs out.
    \f
    :raises HTTPException: 404 for no such run, or one the caller may not read.
    """
    run = await _run_of(request, caller, ref, run_id)
    return runtime_run(request, await _settled(request, run, wait))


@router.post(
    "/{ref}/runs/{run_id}/answers", summary="Answer the question a run waits at"
)
async def answer_workflow_run(
    request: Request, ref: WorkflowRef, run_id: RunId, body: AnswerSend, caller: Caller
) -> RuntimeRun:
    """
    Answer the question (human input) the run waits at: it carries on with
    the answer.
    \f
    :raises HTTPException: 404; 409 when the question isn't open, or it's an
        approval; 422 for an answer that doesn't fit what it asks.
    """
    run = await _run_of(request, caller, ref, run_id)
    answered = await answer_run(request, caller, run, body.request_id, body.answer)
    return runtime_run(request, await _settled(request, answered, body.wait))


@router.post(
    "/{ref}/runs/{run_id}/decisions", summary="Decide the approval a run waits at"
)
async def decide_workflow_run(
    request: Request,
    ref: WorkflowRef,
    run_id: RunId,
    body: DecisionSend,
    caller: Caller,
) -> RuntimeRun:
    """
    Approve or reject the approval (or tool call) the run waits at.
    \f
    :raises HTTPException: 403 anonymously, or without what the step asks of
        its approvers; 404; 409 when it isn't open, or it's a question.
    """
    run = await _run_of(request, caller, ref, run_id)
    decided = await decide_run(
        request, caller, run, body.request_id, body.approved, body.comment
    )
    return runtime_run(request, await _settled(request, decided, body.wait))


@router.post("/{ref}/runs/{run_id}/cancel", summary="Give a run up")
async def cancel_workflow_run(
    request: Request, ref: WorkflowRef, run_id: RunId, caller: Caller
) -> RuntimeRun:
    """
    Give the run up: it never carries on.
    \f
    :raises HTTPException: 404 for no such run, or one the caller may not
        cancel; 409 when it's running or finished.
    """
    run = await _run_of(request, caller, ref, run_id)
    if not await may(request, caller, run, MANAGE_RUNS):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    actor = await caller_actor(request, caller)
    with store_errors():
        abandoned = await run_store(request).abandon(run_id, actor=actor)
    return runtime_run(request, abandoned)
