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
  the member who ran it. When its start allows files, the run is sent them as
  a multipart form (or base64 in JSON; ``forge_admin.adk_workflows.files``),
  and they're saved as artifacts of its session for its steps to read.
- Listing and reading runs, their steps and the files they started with,
  needs ``organizations:read``. Each run says how it was started
  (``provenance``: what started it, through what, for whom, via which agent,
  with which API key and credential; ``root_run_id``), its page where from
  (``origin``), and runs are listed by it. A start the API refused (403, 422)
  makes no run, but is kept: ``…/adk-run-refusals`` lists them.
- A run may wait at several approvals and questions at once (``gates``: the
  ways of it at an approval each, a Decision board's choices), each decided
  on its own by its ``id``, while the run is paused, waiting for a time, or
  carrying on with a decision made at another. ``pause`` is the first, while
  it's paused. A workflow its LLM agent called waits at a gate too
  (``child_run``, its ``details.child_run_id`` the run the call started),
  which nobody decides: that run's end does (409 to a decision). Abandoning
  that run is its end too: the run waiting for it is queued to carry on.
- An approval a run waits at is decided by whom its step names:
  ``agents:approve`` (the organization's admins) when they're its admins,
  ``agents:run`` (every member) when any member may.
- A question (human input) is answered by anyone with ``agents:run``; the
  answer must fit what it asks (its ``response_schema``), and is kept as the
  decision's comment, as JSON.
- Retrying, resubmitting and abandoning one needs ``agents:manage_runs`` (the
  organization's admins). A run abandoned while it runs stops on its worker
  (at its next step, or within a minute): it's abandoned then. A run an A2A
  task started isn't retried: A2A's final states are final, so its task
  would go on saying it failed whatever became of the run; it's resubmitted
  instead (a run of its own).

A run is found only through its organization: any other's answers 404. Times
are ISO 8601 in UTC, with their offset (``+00:00``).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from forge_task_adk_workflows.files import RunFile
from forge_task_adk_workflows.run_store import (
    ABANDONED,
    CANCELABLE,
    CHILD_RUN,
    FAILED,
    FINISHED,
    PAUSED,
    RUNNING,
    TRIGGER_TYPES,
    Actor,
    NoSuchRun,
    NotAllowed,
    Provenance,
    RunStore,
    open_gates,
)
from forge_task_adk_workflows.runs import MAX_NOTES, STATE_KEY
from forge_task_adk_workflows.steps import run_progress
from forge_task_adk_workflows.trace import run_trace, saved_files
from google.adk.sessions import BaseSessionService
from google.adk.sessions import Session as AdkSession
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.adk_workflows.documents import ID_PATTERN
from forge_admin.adk_workflows.files import (
    NOT_SET_UP,
    FileContent,
    Limits,
    artifacts_of,
    attachment,
    read_run_body,
    run_body,
    run_files,
    storage_errors,
)
from forge_admin.adk_workflows.provenance import console_start
from forge_admin.adk_workflows.queue import Embedding
from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import (
    APP_NAME,
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_PATTERN,
    AdkRunError,
    AdkRunRefused,
    access_now,
    checked_answer,
    idempotent_run_id,
    prepare_run,
    queue_run,
    refuse_start,
    same_input,
    start_adk_run,
    start_adk_run_once,
)
from forge_admin.adk_workflows.versions import (
    WorkflowNotFound,
    resolve_workflow,
    workflow_finder,
)
from forge_admin.api.routes.adk_workflows import (
    NOT_FOUND,
    UNAVAILABLE,
    AgentId,
    agent_store,
)
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import (
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
    groups_of_caller,
)
from forge_admin.chat_agents.access import agent_user

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
NO_SUCH_FILE = "The run has no such file"
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
    #: Which version runs: a published one's number, or "draft"; null for
    #: what the builder shows: its draft when it has one, else its latest
    #: published.
    version: Annotated[int, Field(ge=1)] | Literal["draft"] | None = None
    #: The files it starts with, when its start takes files; a multipart
    #: form sends them as ``files`` parts instead.
    files: list[FileContent] = []


class DecisionCreate(BaseModel):
    """A decision on the approval an ADK workflow run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The approval decided, as the run's ``gates`` name it (its ``id``): a
    #: decision never lands on a later approval of the same run.
    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    #: Why, for the record; the run's later steps can read it.
    comment: str = Field(default="", max_length=4000)


class AnswerCreate(BaseModel):
    """An answer to the question (human input) a run waits at."""

    model_config = ConfigDict(extra="forbid")

    #: The question answered, as the run's ``gates`` name it (its ``id``).
    request_id: str = Field(min_length=1, max_length=64)
    #: The answer: it must fit the question's ``response_schema``.
    answer: Any


class RunActor(BaseModel):
    """Who started a run, or did something to it."""

    id: str | None
    name: str


#: What started a run (``forge_task_adk_workflows.run_store.TRIGGER_TYPES``).
TriggerType = Literal[
    "manual",
    "builder_test",
    "assistant",
    "api_key",
    "runtime_rest",
    "a2a",
    "chat_agent",
    "intake",
    "workflow",
    "resubmit",
]


class RunPerson(BaseModel):
    """Whom a run is for."""

    #: The person; else the API key (``apikey:<id>``) or caller
    #: (``anonymous``) that started it.
    id: str
    #: Their name, when it's known (only for whom their credential named).
    name: str
    #: Whether their own credential said who they are: false for an ID a
    #: caller passed on (a public runtime's caller, an API key's end user, a
    #: chat page's visitor); null when unknown (a run from before).
    verified: bool | None


class RunVia(BaseModel):
    """The agent a run was started through."""

    #: A chat agent (``ca_…``: its Workflow tool, or an intake it completed),
    #: or the workflow whose run started it (``ag_…``).
    agent_id: str
    #: Which version of it: a number, or "draft"; null when unknown.
    version: str | None


class RunProvenance(BaseModel):
    """How a run was started, for an audit; each part null when unknown (a
    run from before that couldn't tell)."""

    #: ``manual`` (the console), ``builder_test`` (the builder's test panel),
    #: ``assistant`` (its tools, for a person), ``api_key`` (an organization's
    #: key on the admin API), ``runtime_rest`` and ``a2a`` (the runtime),
    #: ``chat_agent`` (its Workflow tool), ``intake`` (an intake it
    #: completed), ``workflow`` (another run), ``resubmit``.
    trigger_type: TriggerType
    #: ``console``, ``api``, ``a2a``, ``page`` (a chat page), ``assistant``,
    #: ``worker`` (a run starting another).
    channel: str | None
    on_behalf_of: RunPerson | None
    via: RunVia | None
    #: The organization's API key it was started with.
    api_key_id: str | None
    #: ``jwt``, ``api_key``, ``anonymous`` (a public runtime's caller), or
    #: ``local`` (the development user).
    auth_method: str | None


class RunOrigin(BaseModel):
    """Where the request that started a run came from (none for a run
    another started: its root's)."""

    client_ip: str | None
    user_agent: str | None
    #: The request's ID (``X-Request-ID``), as the API's logs have it.
    request_id: str | None


class RunPause(BaseModel):
    """What a run waits for: a person's decision or answer (one of its gates)."""

    #: Names it in a decision or answer (``request_id``).
    id: str
    #: ``approval`` (decided), ``human_input`` (a question, answered), or
    #: ``child_run`` (a workflow's call waiting for the run it started, its
    #: ``details.child_run_id``: that run's end decides it).
    kind: str
    reason: str
    #: The step's: ``kind``, ``approvers``, ``expires_at``, ``step``,
    #: ``step_name``, ``workflow_name``, ``agent_id``, ``message``,
    #: ``session_id``, ``interrupt_id``; a question's ``response_schema`` too;
    #: a Decision board's choice's ``board`` (``{name, message, choice,
    #: label, choices}``).
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
    #: Which version ran: a published one's number, or "draft"; null for
    #: runs from before workflows had versions.
    version: int | Literal["draft"] | None = None
    #: The ADK session it started in (one it started over in is read for its steps).
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
    #: The first of its gates, while it's paused.
    pause: RunPause | None
    #: Every approval and question it waits at, each decided on its own:
    #: while it's paused, waiting for a time, or carrying on meanwhile.
    gates: list[RunPause] = []
    error: RunFailure | None
    #: When someone abandoned it while it ran: its worker stops it, and it's
    #: abandoned then (unless it ended first).
    cancel_requested_at: str | None = None
    #: The run that started it: a workflow its LLM agent called as a tool.
    parent_run_id: str | None = None
    #: A run queued again after a hiccup isn't tried again before then (its backoff).
    next_attempt_at: str | None = None
    #: How it was started: what, through what, for whom, via which agent;
    #: null for a run from before that couldn't tell.
    provenance: RunProvenance | None = None
    #: The top of its parent chain: itself, for a run nothing started.
    root_run_id: str | None = None


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
    #: The files it started with: artifacts of its session (``…/files/{name}``).
    files: list[RunFile] = []
    #: The graph's result, once it ended (succeeded, or failed with one).
    result: Any = None
    #: The ADK workflow as it ran.
    document: dict[str, Any] | None
    #: What started it, as its payload says: ``{"type": "manual", "by":
    #: <user>}``; a resubmission's ``{"type": "resubmit", "by", "run_id"}``.
    trigger: dict[str, Any] | None
    #: A resubmission's first run's trigger.
    original_trigger: dict[str, Any] | None = None
    #: Where the request that started it came from.
    origin: RunOrigin | None = None
    #: Oldest first.
    events: list[RunEvent]
    #: Its activity misses notes: the run noted as many as it may, or some
    #: couldn't be written.
    activity_incomplete: bool = False
    #: The runs it started (workflows its LLM agents called as tools), oldest
    #: first; abandoning it abandons those still going.
    children: list[AdkRun] = Field(default_factory=list)
    actions: RunActions


class AdkRunRefusal(BaseModel):
    """A start the organization refused: no run was made."""

    id: int
    organization_id: str
    #: The workflow it'd have run, and which version (when it was known).
    agent_id: str
    version: str | None
    #: What the start was answered: 403 (they may not), 422 (what it'd run
    #: doesn't fit or build).
    status_code: int
    reason: str
    #: Who asked, as a run would have it.
    requested_by: RunActor | None
    provenance: RunProvenance | None
    origin: RunOrigin | None
    at: str


class AdkRunRefusalPage(BaseModel):
    """A page of refused starts, newest first, and how many match."""

    items: list[AdkRunRefusal]
    total: int


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
    #: The tools its LLM agents called: ``{"id", "agent", "name", "args",
    #: "status", "response", "error", "at"}`` (``error``: what a failed
    #: call's answer says went wrong).
    calls: list[dict[str, Any]] = []
    #: What it was handed (its ``previous``), and the step that handed it on.
    input: Any = None
    input_from: str | None = None
    #: What it worked out and sent, kept before it sent it: a saved
    #: workflow's inputs, an Agents-page agent's message and inputs, an HTTP
    #: request (``{method, url, headers (names), body}``); null for others.
    called_with: Any = None
    #: What it set in the session's state, in order: ``{"key", "value", "at"}``.
    state_changes: list[dict[str, Any]] = []
    #: A step before it handed on to it (by a way it took): unfinished, it's
    #: the step the run is on, though it has recorded nothing yet.
    handed_to: bool = False


class AdkRunSteps(BaseModel):
    """A run's steps, in its document's order."""

    steps: list[AdkRunStep]
    #: The session they're read from: the one the run runs in, or an earlier one asked for.
    session_id: str
    #: The sessions the run ran in before it started over, oldest first.
    earlier_sessions: list[str] = Field(default_factory=list)
    #: The document's edges the run has gone along (their IDs).
    taken_edges: list[str] = Field(default_factory=list)


class AdkRunTrace(BaseModel):
    """
    Everything a run did in its ADK session, for the organization's admins:
    each event (``forge_task_adk_workflows.trace``) and the session's state
    before and after the run, with what the worker keeps of the run.
    """

    #: Each event of the run, in order: its step, message, calls and answers,
    #: output, state and artifact changes, route, error and token usage.
    events: list[dict[str, Any]]
    #: The session's state as the first listed event found it.
    state_before: dict[str, Any]
    #: It once the run's events applied.
    state_after: dict[str, Any]
    #: The session's state now.
    state: dict[str, Any]
    #: A later run in the same session changed the state since.
    later_changes: bool
    #: How many of the run's earliest events aren't listed.
    omitted: int
    #: The run's ADK invocation.
    invocation_id: str | None
    #: The session they're read from, and those the run ran in before.
    session_id: str
    earlier_sessions: list[str] = Field(default_factory=list)
    #: What the worker keeps of the run (``runs.STATE_KEY``): its session and
    #: invocation, the steps it noted, the tool calls it journals, its restarts.
    worker: dict[str, Any] = Field(default_factory=dict)


class RunFileVersion(BaseModel):
    """A version of a file in a run's session."""

    version: int
    #: When it was saved, ISO 8601 in UTC, when the storage says.
    saved_at: str | None = None
    media_type: str | None = None
    #: Its size, when it was saved with it (an agent's ``save_file`` does).
    size_bytes: int | None = None
    #: The step whose event saved it, and who (its agent); None for a file the
    #: run started with, or one saved out of the run's events.
    step: str | None = None
    saved_by: str | None = None


class RunSessionFile(BaseModel):
    """A file in a run's session: one it started with, or one it saved."""

    name: str
    #: ``input`` for a file the run started with, ``run`` for one it saved.
    origin: Literal["input", "run"]
    #: Its latest version's.
    media_type: str | None = None
    #: Oldest first.
    versions: list[RunFileVersion]


class AdkRunFiles(BaseModel):
    """The files in a run's session: those it started with and those it saved."""

    files: list[RunSessionFile]
    #: The session they're in.
    session_id: str
    #: Whether runs' files are kept here at all (the admin API's artifacts).
    kept: bool = True


# ---------------------------------------------------------------- reading runs


def _iso(when: datetime | None) -> str | None:
    return when.astimezone(UTC).isoformat() if when is not None else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _pause(run: dict[str, Any]) -> RunPause | None:
    pause = run.get("pause")
    if run["status"] != PAUSED or not isinstance(pause, dict):
        return None
    return _gate(pause)


def _gates(run: dict[str, Any]) -> list[RunPause]:
    return [_gate(gate) for gate in open_gates(run)]


def _gate(pause: dict[str, Any]) -> RunPause:
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


def version_of(stored: Any) -> int | str | None:
    """:return: A run's version as the API answers it: a number, "draft", or null."""
    if stored is None or stored == "draft":
        return stored
    text = str(stored)
    return int(text) if text.isdigit() else None


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
        "version": version_of(run.get("version")),
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
        "gates": _gates(run),
        "error": _failure(run),
        "cancel_requested_at": _iso(run.get("cancel_requested_at")),
        "parent_run_id": run.get("parent_run_id"),
        "next_attempt_at": _iso(run.get("next_attempt_at")),
        "provenance": provenance_of(run),
        "root_run_id": run.get("root_run_id"),
    }


def provenance_of(run: dict[str, Any]) -> RunProvenance | None:
    """:return: How a run (or a refused start) was started, as the store has it."""
    started = Provenance.of(run)
    if started is None or started.trigger_type not in TRIGGER_TYPES:
        return None
    return RunProvenance(
        trigger_type=started.trigger_type,  # type: ignore[arg-type]
        channel=started.channel,
        on_behalf_of=RunPerson(
            id=started.on_behalf_of,
            name=started.on_behalf_of_name,
            verified=started.on_behalf_of_verified,
        )
        if started.on_behalf_of
        else None,
        via=RunVia(agent_id=started.via_agent_id, version=started.via_agent_version)
        if started.via_agent_id
        else None,
        api_key_id=started.api_key_id,
        auth_method=started.auth_method,
    )


def origin_of(run: dict[str, Any]) -> RunOrigin | None:
    """:return: Where the request that started a run came from; None when nobody knows."""
    origin = RunOrigin(
        client_ip=run.get("client_ip"),
        user_agent=run.get("user_agent"),
        request_id=run.get("request_id"),
    )
    return origin if any(origin.model_dump().values()) else None


def as_run(run: dict[str, Any]) -> AdkRun:
    """:return: A run as the store has it (all of it, or a list's summary), as lists show it."""
    return AdkRun.model_validate(_summary(run))


#: Why a failed run an A2A task started isn't retried.
A2A_RETRY = (
    "A run started over A2A can't be retried: its A2A task ended when the run "
    "failed, for good. Resubmit it to run it again as a run of its own, or start "
    "a new task over A2A"
)


def a2a_task(run: dict[str, Any]) -> bool:
    """:return: Whether the run is an A2A task's: started over A2A (the task's
    ID is the run's), not resubmitted from one."""
    payload = run.get("payload")
    trigger = payload.get("trigger") if isinstance(payload, dict) else None
    return (
        isinstance(trigger, dict)
        and trigger.get("type") == "runtime"
        and trigger.get("protocol") == "a2a"
        and not run.get("resubmit_of")
    )


def actions_of(run: dict[str, Any]) -> RunActions:
    """:return: What the run's status allows (and what started it: an A2A
    task's run isn't retried)."""
    current = run["status"]
    gates = _gates(run)
    # Running, unless someone abandoned it already (its worker stops it).
    stopping = run.get("cancel_requested_at") is not None
    return RunActions(
        retry=current == FAILED and not a2a_task(run),
        resubmit=current in FINISHED,
        abandon=current in CANCELABLE and not stopping,
        decide=any(gate.kind not in (HUMAN_INPUT, CHILD_RUN) for gate in gates),
        answer=any(gate.kind == HUMAN_INPUT for gate in gates),
    )


def as_detail(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    children: list[dict[str, Any]] | None = None,
) -> AdkRunDetail:
    """:return: A run as the store has it, with its activity and the runs it
    started, as its page shows it."""
    payload = run.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    document, trigger = payload.get("document"), payload.get("trigger")
    original = payload.get("original_trigger")
    return AdkRunDetail.model_validate(
        {
            **_summary(run),
            "input": payload.get("input"),
            "files": run_files(payload),
            "result": run.get("result"),
            "document": document if isinstance(document, dict) else None,
            "trigger": trigger if isinstance(trigger, dict) else None,
            "original_trigger": original if isinstance(original, dict) else None,
            "origin": origin_of(run),
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
            "activity_incomplete": activity_incomplete(run),
            "children": [as_run(child) for child in children or []],
            "actions": actions_of(run),
        }
    )


def activity_incomplete(run: dict[str, Any]) -> bool:
    """:return: Whether the run's activity misses notes (``runs.MAX_NOTES``,
    ``notes_lost``)."""
    kept = _kept(run)
    lost, noted = kept.get("notes_lost"), kept.get("notes")
    return (isinstance(lost, int) and lost > 0) or (
        isinstance(noted, int) and noted > MAX_NOTES
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


def _waits_for_run(pause: RunPause) -> str:
    """:return: Why nobody decides or answers a ``child_run`` gate."""
    child = str(pause.details.get("child_run_id") or "")[:8]
    return f"The run waits for run {child} to end, not for people: it carries on once that run ends"


def _waits_at(run: dict[str, Any], request_id: str) -> RunPause:
    """
    :return: The gate named, when the run waits at it.
    :raises HTTPException: 409 when it doesn't (decided, or the run moved on).
    """
    pause = next((gate for gate in _gates(run) if gate.id == request_id), None)
    if pause is None:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    return pause


async def _actor(session: AsyncSession, user: str) -> Actor:
    return Actor(user, await name_of(session, user))


#: An ``Idempotency-Key`` header: what makes a start safe to retry.
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_KEY,
        pattern=IDEMPOTENCY_PATTERN,
        description="Makes the start safe to retry: the same key, from the same "
        "caller, in the organization, starts one run; sent again, the answer is that run.",
    ),
]


def _retried(run: dict[str, Any], input: Any) -> AdkRun:
    """
    :return: The run a start's retry (its idempotency key) asked for again.
    :raises HTTPException: 409 when the retry's input isn't the run's: the
        key was used for another start.
    """
    if not same_input(run, input):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That idempotency key started a run with another input: use a new "
            "one for another start.",
        )
    return as_run(run)


# ---------------------------------------------------------------- routes


@router.post(
    "/organizations/{organization_id}/agents/{agent_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra=run_body(
        AdkRunCreate,
        "The input, which version, and the files it starts with: JSON, or a "
        "multipart form with the input as JSON text and each file a files part.",
    ),
)
async def run_adk_workflow(
    organization_id: NodeId,
    agent_id: AgentId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    idempotency_key: IdempotencyKey = None,
) -> AdkRun:
    """
    Run an ADK workflow, at the version named (its draft, by default, when it
    has one; else its latest published), with the saved ADK workflows it
    runs, as the caller: the run, queued for a worker. Files it's sent, when
    its start takes files, are saved as artifacts of its session.
    \f
    :param organization_id: The organization.
    :param agent_id: The ADK workflow.
    :return: The run.
    :raises HTTPException: 403 without agents:run in the organization, or
        when it uses an agent from the Agents page the caller may not use
        (naming the node); 404 when the organization has no such ADK
        workflow; 413 for a file too big; 422 for input or a file that
        doesn't fit its start, or a document that doesn't build (the reason
        naming the node); 503 when runs, files or MongoDB aren't set up or
        answering. A start refused for 403 or 422 is kept for the
        organization's audit (``…/adk-run-refusals``).
    """
    starter = await _actor(session, user)
    # Whose start it is and how it came (the builder, the console, the
    # assistant, a key), for the run's audit, and for a refusal's.
    started = console_start(request, starter)
    refused = partial(
        refuse_start,
        run_store(request),
        organization_id=organization_id,
        agent_id=agent_id,
        requested_by=starter,
        provenance=started,
    )
    try:
        await authorize(session, enforcer, user, RUN, Scope(Level.ORG, organization_id))
    except HTTPException as error:
        if error.status_code == status.HTTP_403_FORBIDDEN:
            await refused(status_code=error.status_code, reason=str(error.detail))
        raise
    body, files = await read_run_body(
        request, AdkRunCreate, Limits.of(request.app.state.settings)
    )
    run_id = (
        idempotent_run_id(organization_id, idempotency_key)
        if idempotency_key is not None
        else None
    )
    if run_id is not None:
        # A retry: the run it made, whatever's changed since.
        with store_errors():
            found = await run_store(request).started_as(
                run_id,
                organization_id=organization_id,
                agent_id=agent_id,
                requested_by=starter,
            )
        if found is not None:
            return _retried(found, body.input)
    artifacts = artifacts_of(request, files)
    store = agent_store(request)
    queue = run_queue(request)
    find = workflow_finder(store, organization_id)
    resolved = None

    try:
        current = await store.get(organization_id, agent_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        # Unnamed, what the builder shows: the draft, else the latest published.
        version = body.version or ("draft" if current.get("has_draft") else None)
        try:
            resolved = await resolve_workflow(
                store,
                f"{agent_id}@{version}" if version else agent_id,
                organization_id=organization_id,
            )
        except WorkflowNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        record = resolved.run_record
        prepared = await prepare_run(
            record,
            body.input,
            find,
            RunResources(
                organization_id,
                chat_agents=getattr(request.app.state, "chat_agents", None),
                ui_components=getattr(request.app.state, "ui_components", None),
                sessions=request.app.state.sessionmaker,
                user=await agent_user(
                    enforcer, organization_id, user, groups_of_caller(user)
                ),
                workflows=store,
            ),
            files,
        )
    except AdkRunError as error:
        code = (
            status.HTTP_403_FORBIDDEN
            if isinstance(error, AdkRunRefused)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        await refused(
            status_code=code,
            reason=str(error),
            version=resolved.version if resolved is not None else body.version,
        )
        raise HTTPException(code, str(error)) from None
    except PyMongoError as error:
        logger.warning("The agents' MongoDB failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    starting = {
        "prepared": prepared,
        "input": body.input,
        "run_as": user,
        "run_as_name": starter.name,
        "trigger": {"type": "manual", "by": user},
        "version": resolved.version,
        "artifacts": artifacts,
        "provenance": started,
    }
    with store_errors(), storage_errors():
        if run_id is None:
            run = await start_adk_run(run_store(request), queue, record, **starting)
        else:
            run, created = await start_adk_run_once(
                run_store(request), queue, record, run_id=run_id, **starting
            )
            if not created:
                return _retried(run, body.input)
    logger.info(
        "%s ran ADK workflow %s (version %s) in organization %s with %d files: run %s",
        user,
        agent_id,
        resolved.version,
        organization_id,
        len(files),
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
    trigger_type: Annotated[list[TriggerType] | None, Query()] = None,
    on_behalf_of: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    via_agent_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    api_key_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    root_run_id: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> AdkRunPage:
    """
    List the organization's ADK workflow runs, newest first: all of them, or
    one ADK workflow's, in any status or some, started however or some way,
    by or for anyone or someone, then.
    \f
    :param agent_id: Only this ADK workflow's runs.
    :param status_filter: Only runs in these statuses (``?status=`` repeated).
    :param trigger_type: Only runs started so (repeated).
    :param on_behalf_of: Only runs for this person (or API key, ``apikey:<id>``).
    :param via_agent_id: Only runs started through this chat agent (or by
        this workflow's runs).
    :param api_key_id: Only runs started with this API key.
    :param root_run_id: Only this run and the runs under it.
    :param created_from: Only runs created then or later (ISO 8601; UTC
        without an offset).
    :param created_to: Only runs created before then.
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
            trigger_types=trigger_type,
            on_behalf_of=on_behalf_of,
            via_agent_id=via_agent_id,
            api_key_id=api_key_id,
            root_run_id=root_run_id,
            created_from=created_from,
            created_to=created_to,
        )
    return AdkRunPage(items=[as_run(run) for run in items], total=total)


@router.get("/organizations/{organization_id}/adk-run-refusals")
async def list_adk_run_refusals(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    agent_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
    trigger_type: Annotated[list[TriggerType] | None, Query()] = None,
    on_behalf_of: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    via_agent_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    api_key_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdkRunRefusalPage:
    """
    List the starts of the organization's ADK workflows it refused, newest
    first: the caller may not (403: without agents:run, or an agent from
    the Agents page they may not use), or what it'd run doesn't fit or build
    (422). Filtered as runs are.
    \f
    :return: ``{"items": [...], "total"}``: the page, and how many match.
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when the run store isn't answering.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    with store_errors():
        items, total = await run_store(request).refused(
            organization_id,
            agent_id=agent_id,
            trigger_types=trigger_type,
            on_behalf_of=on_behalf_of,
            via_agent_id=via_agent_id,
            api_key_id=api_key_id,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    return AdkRunRefusalPage(
        items=[
            AdkRunRefusal(
                id=item["id"],
                organization_id=item["organization_id"],
                agent_id=item["agent_id"],
                version=item["version"],
                status_code=item["status_code"],
                reason=item["reason"],
                requested_by=RunActor(
                    id=item["requested_by"], name=item["requested_by_name"] or ""
                )
                if item["requested_by"] or item["requested_by_name"]
                else None,
                provenance=provenance_of(item),
                origin=origin_of(item),
                at=_iso(item["at"]) or "",
            )
            for item in items
        ],
        total=total,
    )


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
    what it waits for, its result or failure, its activity, the runs it
    started, and the actions its status allows.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        404 for a run that isn't the organization's; 503 when the run store
        isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    with store_errors():
        events = await run_store(request).events(run_id)
        children = await run_store(request).children(run_id)
    return as_detail(run, events, children)


@router.get("/organizations/{organization_id}/adk-runs/{run_id}/steps")
async def get_adk_run_steps(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    earlier: Annotated[
        str | None, Query(alias="session_id", min_length=1, max_length=128)
    ] = None,
) -> AdkRunSteps:
    """
    Read a run's steps from its ADK session: each step of the ADK workflow it
    runs, in order, with its status, what it handed on or why it failed, and
    when it started and finished. They're the run's own (a session another
    run shares holds that run's too), from the session it runs in now; a run
    that started over lists the sessions it ran in before, and
    ``?session_id=`` reads one of them.
    \f
    :param earlier: One of the sessions the run ran in before (``earlier_sessions``).
    :return: ``{"steps": [...], "session_id", "earlier_sessions"}``.
    :raises HTTPException: 403 without organizations:read in the organization;
        404 for a run that isn't the organization's, or a session it never ran
        in; 503 when the run store or the sessions' database isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    found, document, session_id, before = await _run_session(request, run, earlier)
    # A run that hasn't started has no invocation yet, and so no events of its own.
    invocation = str(_kept(run).get("invocation_id") or "")
    progress = run_progress(found, document, invocation_id=invocation)
    steps = [AdkRunStep.model_validate(step) for step in progress["steps"]]
    return AdkRunSteps(
        steps=steps,
        session_id=session_id,
        earlier_sessions=before,
        taken_edges=progress["taken_edges"],
    )


@router.get("/organizations/{organization_id}/adk-runs/{run_id}/trace")
async def get_adk_run_trace(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    earlier: Annotated[
        str | None, Query(alias="session_id", min_length=1, max_length=128)
    ] = None,
) -> AdkRunTrace:
    """
    Read everything a run did in its ADK session, for the organization's
    admins: each of its events (who said what, the tools called and what they
    answered, what each step handed on, every change to the session's state
    and artifacts, the way taken, errors, token usage), the session's state
    before and after the run, and what the worker keeps of it. A run that
    started over reads one of its earlier sessions with ``?session_id=``.
    \f
    :param earlier: One of the sessions the run ran in before (``earlier_sessions``).
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's, or a session it never ran
        in; 503 when the run store or the sessions' database isn't answering.
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    found, document, session_id, before = await _run_session(request, run, earlier)
    kept = _kept(run)
    invocation = str(kept.get("invocation_id") or "")
    traced = run_trace(found, document, invocation_id=invocation)
    return AdkRunTrace(
        **traced,
        session_id=session_id,
        earlier_sessions=before,
        worker=kept,
    )


async def _run_session(
    request: Request, run: dict[str, Any], earlier: str | None
) -> tuple[AdkSession | None, dict[str, Any], str, list[str]]:
    """
    :param earlier: One of the sessions the run ran in before, to read
        instead of the one it runs in.
    :return: The run's ADK session (None before it has one), the document it
        runs, the session's ID, and the sessions it ran in before.
    :raises HTTPException: 404 for a run without a document or a session it
        never ran in; 503 when the sessions' database isn't answering.
    """
    payload = run.get("payload")
    run_as = payload.get("run_as") if isinstance(payload, dict) else None
    document = payload.get("document") if isinstance(payload, dict) else None
    if not (isinstance(run_as, str) and isinstance(document, dict)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_SUCH_RUN)
    kept = _kept(run)
    current = str(kept.get("session_id") or run["session_id"])
    before = [str(each) for each in kept.get("earlier_sessions") or []]
    if earlier is not None and earlier not in (current, *before):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The run never ran in that session"
        )
    session_id = earlier or current
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
    return found, document, session_id, before


@router.get("/organizations/{organization_id}/adk-runs/{run_id}/session-files")
async def list_adk_run_files(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRunFiles:
    """
    List the files in a run's session: those it started with, and those its
    agents saved (``save_file``), each version with the step that saved it.
    Each downloads from ``…/files/{name}?version=``.
    \f
    :raises HTTPException: 403 without organizations:read; 404 for a run that
        isn't the organization's; 503 when the files' storage or the
        sessions' database isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    payload = run.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    kept = _kept(run)
    session_id = str(kept.get("session_id") or run["session_id"])
    artifacts = getattr(request.app.state, "workflow_artifacts", None)
    if artifacts is None:
        return AdkRunFiles(files=[], session_id=session_id, kept=False)
    found, document, _, _ = await _run_session(request, run, None)
    saved = saved_files(
        found, document, invocation_id=str(kept.get("invocation_id") or "")
    )
    inputs = {file.name: file for file in run_files(payload)}
    user_id = str(payload.get("run_as") or "")
    listed: list[RunSessionFile] = []
    with storage_errors():
        names = await artifacts.list_artifact_keys(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        for name in sorted(names):
            versions = await artifacts.list_artifact_versions(
                app_name=APP_NAME, user_id=user_id, session_id=session_id, filename=name
            )
            shown = []
            for each in sorted(versions, key=lambda version: version.version):
                meta = each.custom_metadata or {}
                by = saved.get((name, each.version), {})
                size = meta.get("size_bytes")
                sent = inputs.get(name)
                if size is None and sent is not None and sent.version == each.version:
                    size = sent.size_bytes
                shown.append(
                    RunFileVersion(
                        version=each.version,
                        saved_at=_iso(datetime.fromtimestamp(each.create_time, UTC))
                        if each.create_time
                        else by.get("at"),
                        media_type=each.mime_type,
                        size_bytes=size if isinstance(size, int) else None,
                        step=by.get("step"),
                        saved_by=by.get("author") or meta.get("saved_by"),
                    )
                )
            listed.append(
                RunSessionFile(
                    name=name,
                    origin="input" if name in inputs else "run",
                    media_type=shown[-1].media_type if shown else None,
                    versions=shown,
                )
            )
    return AdkRunFiles(files=listed, session_id=session_id)


def _kept(run: dict[str, Any]) -> dict[str, Any]:
    """:return: What the worker keeps of a run's ADK workflow (``runs.STATE_KEY``):
    the session it runs in, its invocation, the sessions it ran in before."""
    state = run.get("state")
    kept = state.get(STATE_KEY) if isinstance(state, dict) else None
    return kept if isinstance(kept, dict) else {}


@router.get(
    "/organizations/{organization_id}/adk-runs/{run_id}/files/{name}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def get_adk_run_file(
    organization_id: NodeId,
    run_id: RunId,
    name: Annotated[str, Path(min_length=1, max_length=255)],
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    version: Annotated[int | None, Query(ge=0)] = None,
) -> Response:
    """
    Download a file of the run: one it started with, as it was sent (the
    artifact saved in its session, or a resubmitted run's original's), or one
    its agents saved in its session (its latest version, or ``?version=``).
    \f
    :raises HTTPException: 403 without organizations:read; 404 for a run or
        file that isn't there; 503 when files aren't set up or their storage
        isn't answering.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    payload = run.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    artifacts = getattr(request.app.state, "workflow_artifacts", None)
    if artifacts is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    file = next((f for f in run_files(payload) if f.name == name), None)
    if file is not None and version is None:
        # One it started with, where it was sent.
        session_id = str(
            payload.get("files_session_id")
            or payload.get("session_id")
            or run["session_id"]
        )
        version = file.version
    else:
        session_id = str(_kept(run).get("session_id") or run["session_id"])
    with storage_errors():
        part = await artifacts.load_artifact(
            app_name=APP_NAME,
            user_id=str(payload.get("run_as") or ""),
            session_id=session_id,
            filename=name,
            version=version,
        )
    blob = part.inline_data if part is not None else None
    if blob is None or blob.data is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "The run's file isn't kept any more" if file is not None else NO_SUCH_FILE,
        )
    return Response(
        content=blob.data,
        media_type=file.media_type
        if file is not None and version == file.version
        else blob.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": attachment(name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


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
    Approve or reject an approval an ADK workflow run waits at (one of its
    gates; the others stay open): the run carries on down the step's
    approved or rejected way, with who decided and why.
    \f
    :return: The run: queued, unless it's carrying on already (its next
        attempt reads the decision).
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
    if pause.kind == CHILD_RUN:
        raise HTTPException(status.HTTP_409_CONFLICT, _waits_for_run(pause))
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
    Answer a question (human input) an ADK workflow run waits at: the run
    carries on with the answer.
    \f
    :return: The run: queued, unless it's carrying on already.
    :raises HTTPException: 403 without agents:run in the organization; 404 for
        a run that isn't the organization's; 409 when the question isn't open,
        or it's an approval (decided at ``.../decisions``); 422 for an answer
        that doesn't fit what it asks.
    """
    run = await _read(request, session, enforcer, user, organization_id, run_id)
    pause = _waits_at(run, body.request_id)
    if pause.kind == CHILD_RUN:
        raise HTTPException(status.HTTP_409_CONFLICT, _waits_for_run(pause))
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
    where it was, on a worker. A run an A2A task started isn't retried (its
    task ended for good): it's resubmitted instead.
    \f
    :return: The run, queued.
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 unless it failed, or
        when an A2A task started it.
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        store = run_store(request)
        found = await store.get(run_id, organization_id=organization_id)
        if found is not None and found["status"] == FAILED and a2a_task(found):
            raise NotAllowed(A2A_RETRY)
        retried = await store.retry(
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
    idempotency_key: IdempotencyKey = None,
) -> AdkRun:
    """
    Run a finished ADK workflow run's ADK workflow again, as it ran (the same
    documents, input and files), as a new run in an ADK session of its own:
    the caller's resubmission (its trigger, and whom it's for), the run's own
    trigger kept as its ``original_trigger``. Its agents' rules are checked
    against what the caller holds now, not what the run was started with.
    \f
    :return: The new run, queued; with an ``Idempotency-Key`` sent again,
        the resubmission it made.
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 unless it's
        finished, or for a key used for another start.
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    queue = run_queue(request)
    actor = await _actor(session, user)
    with store_errors():
        store = run_store(request)
        resubmitted = await store.get(run_id, organization_id=organization_id)
        if resubmitted is None:
            raise NoSuchRun(run_id)
        # The resubmission is the caller's (its provenance says it's for
        # them): their grants as they are now, which the run's own person's
        # sign-in (its company groups) isn't here to tell for them.
        access = await access_now(
            resubmitted.get("payload") or {},
            RunResources(
                organization_id,
                chat_agents=None,
                sessions=request.app.state.sessionmaker,
                user=await agent_user(
                    enforcer, organization_id, user, groups_of_caller(user)
                ),
                workflows=None,
            ),
        )
        again, created = await store.resubmit_once(
            run_id,
            again_id=idempotent_run_id(organization_id, idempotency_key)
            if idempotency_key is not None
            else None,
            actor=actor,
            organization_id=organization_id,
            provenance=console_start(request, actor),
            agent_access=access,
        )
    if not created:
        # A retry: the resubmission it made.
        return as_run(again)
    await queue_run(queue, again["id"])
    logger.info("%s resubmitted ADK workflow run %s as %s", user, run_id, again["id"])
    return as_run(again)


@router.post(
    "/organizations/{organization_id}/adk-runs/{run_id}/abandon",
    responses={
        202: {
            "model": AdkRun,
            "description": "It's running: its worker stops it, then abandons it",
        }
    },
)
async def abandon_adk_run(
    organization_id: NodeId,
    run_id: RunId,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> AdkRun:
    """
    Give an ADK workflow run up: queued, paused, waiting or failed, it never
    carries on. Running, its worker stops it (at its next step, or within a
    minute) and abandons it: 202, with ``cancel_requested_at`` set. Asking
    again changes nothing. The runs it started that are still going, and
    theirs, are abandoned with it. A run another waits for (a workflow its
    LLM agent called) is that run's answer, abandoned: that run is queued to
    carry on.
    \f
    :return: The run, abandoned; or still running, being stopped (202).
    :raises HTTPException: 403 without agents:manage_runs in the organization;
        404 for a run that isn't the organization's; 409 once it's finished
        (but failed).
    """
    await authorize(
        session, enforcer, user, MANAGE_RUNS, Scope(Level.ORG, organization_id)
    )
    actor = await _actor(session, user)
    with store_errors():
        abandoned = await run_store(request).abandon(
            run_id, actor=actor, organization_id=organization_id
        )
    if abandoned["status"] == RUNNING:
        response.status_code = status.HTTP_202_ACCEPTED
        logger.info("%s asked ADK workflow run %s to stop", user, run_id)
    else:
        logger.info("%s abandoned ADK workflow run %s", user, run_id)
    queue: Embedding | None = request.app.state.embedding
    if abandoned["status"] == ABANDONED and abandoned.get("parent_run_id") and queue:
        # The run store queued it with this run's end, should it wait for this
        # run; else its job finds nothing to run.
        await queue_run(queue, abandoned["parent_run_id"])
    return as_run(abandoned)
