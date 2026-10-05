"""
An organization's overview: what its workflows, agents and assistant did
over the last 7, 30 or 90 days, what they used (model calls, their tokens and
cost, tool calls) and what waits on people now, for the people who run it
(``forge_admin.overview``).

Reading it needs ``organizations:read`` in the organization (its members).
Days are the caller's (``time_zone``, an IANA name; UTC when unknown). Times
are ISO 8601 in UTC, with their offset (``+00:00``).
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from pymongo.errors import PyMongoError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.adk_workflow_runs import run_store
from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.overview.report import Sources, build_overview, window, zone_of

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Organization overview"])

READ = "organizations:read"
UNAVAILABLE = "The organization's overview isn't answering; try again shortly"

Kind = Literal["workflow", "agent", "assistant"]


class Bucket(BaseModel):
    start: str
    end: str


class Subject(BaseModel):
    """A workflow, an agent, or the assistant."""

    #: ``<kind>:<id>``: what the facts name it by.
    key: str
    kind: Kind
    id: str
    name: str
    #: It's still there; a deleted one keeps its history.
    current: bool
    #: Its latest run, invocation or model call, whenever it was.
    last_at: str | None


class Member(BaseModel):
    id: str
    name: str


class UsageFact(BaseModel):
    """Model calls in a bucket (-1: the period before), of one subject, to one model."""

    bucket: int
    subject: str
    model: str
    calls: int
    failed: int
    #: Prompt tokens, cached ones included.
    input: int
    cached: int
    #: Reply tokens, thinking ones included.
    output: int
    thinking: int
    #: USD, of the priced calls.
    cost: float
    #: Calls whose model has no known price.
    unpriced: int


class ActivityFact(BaseModel):
    """Workflow runs, or agent and assistant invocations, that started in a bucket."""

    bucket: int
    subject: str
    started: int
    succeeded: int
    failed: int
    abandoned: int
    open: int
    retried: int
    duration_ms: int
    timed: int


class WeekFact(BaseModel):
    subject: str
    #: 0 is 11 weeks ago; 11 this week so far.
    week: int
    tokens: int
    runs: int


class StepFact(BaseModel):
    subject: str
    #: The ADK agent that called: a workflow's step, an agent or a sub-agent.
    author: str
    calls: int
    tokens: int
    cost: float


class ToolFact(BaseModel):
    subject: str
    tool: str
    calls: int
    failed: int


class PersonFact(BaseModel):
    subject: str
    user: str
    runs: int
    calls: int
    tokens: int
    cost: float


class WaitingRun(BaseModel):
    run_id: str
    subject: str
    kind: Literal["approval", "human_input"]
    reason: str
    since: str | None


class FailureFact(BaseModel):
    subject: str
    step: str | None
    message: str
    count: int
    last_at: str | None


class OpenRuns(BaseModel):
    queued: int
    running: int
    paused: int
    waiting: int


class OverviewOut(BaseModel):
    """The organization's overview of a period."""

    period: Literal["7d", "30d", "90d"]
    unit: Literal["day", "week"]
    time_zone: str
    as_of: str
    buckets: list[Bucket]
    previous: Bucket
    weeks: list[Bucket]
    subjects: list[Subject]
    members: list[Member]
    usage: list[UsageFact]
    activity: list[ActivityFact]
    weekly: list[WeekFact]
    steps: list[StepFact]
    tools: list[ToolFact]
    people: list[PersonFact]
    waiting: list[WaitingRun]
    failures: list[FailureFact]
    open: OpenRuns
    #: The first model call recorded; null before usage was.
    recording_since: str | None
    #: More workflow runs than it reads: the oldest are left out.
    truncated: bool


async def _current(store: Any, organization_id: str) -> list[tuple[str, str]]:
    """
    :return: The organization's workflows or agents now, ``(id, name)``;
        none when their store isn't set up or answering.
    """
    if store is None:
        return []
    try:
        records = await store.list(organization_id)
    except PyMongoError as error:
        logger.warning(
            "The overview goes without the organization's documents: %s", error
        )
        return []
    found = []
    for record in records:
        document = (
            record.get("document") if isinstance(record.get("document"), dict) else {}
        )
        found.append((str(record["_id"]), str(document.get("name") or record["_id"])))
    return found


def _names(
    session: AsyncSession,
) -> Callable[[Sequence[str]], Awaitable[dict[str, str]]]:
    """Users' names by ID: their name, or else their email, or else the ID."""

    async def names(ids: Sequence[str]) -> dict[str, str]:
        return {user: await name_of(session, user) for user in ids}

    return names


@router.get("/organizations/{organization_id}/overview")
async def organization_overview(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    period: Annotated[Literal["7d", "30d", "90d"], Query()] = "7d",
    time_zone: Annotated[str | None, Query(max_length=64)] = None,
) -> OverviewOut:
    """
    The organization's overview of the last 7, 30 or 90 days: its workflow
    runs and its agents' and assistant's invocations, by how they ended; its
    model calls, their tokens (input, cached, output, thinking) and cost, by
    workflow or agent and model, bucketed by the caller's day (by week for
    90 days), with the period before for comparison; its steps, tools and
    people; and the runs waiting on people now.
    \f
    :param period: ``7d``, ``30d`` or ``90d``.
    :param time_zone: The caller's IANA time zone, whose days the buckets are.
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when its runs or usage aren't answering.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    runs = run_store(request)
    usage = request.app.state.usage
    sources = Sources(
        runs=runs,
        usage=usage,
        workflows=await _current(
            request.app.state.organization_agents, organization_id
        ),
        agents=await _current(
            getattr(request.app.state, "chat_agents", None), organization_id
        ),
    )
    # Now, as the runs' times are stamped (a test's clock, in tests).
    win = window(period, zone_of(time_zone), runs.clock())
    try:
        overview = await build_overview(organization_id, sources, win, _names(session))
    except (SQLAlchemyError, OSError) as error:
        logger.warning("The overview of %s failed: %s", organization_id, error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    return OverviewOut.model_validate(overview)
