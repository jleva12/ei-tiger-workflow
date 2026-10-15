"""
Conversations, for their audit (``forge_admin.conversations.audit``): every
conversation of an organization's chat agents (from the builder's chat, the
run API or A2A) and of the assistant asked from inside it, found by who held
or called it, by agent and by channel; and each one whole: its messages,
tool calls and answers, state, turns, and model and tool usage.

- An organization's need ``agents:audit`` there (its admins).
- The site's, every assistant conversation (asked from inside an
  organization or not), need ``agents:audit`` on the site (site admins).

Times are ISO 8601 in UTC, with their offset.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from pymongo.errors import PyMongoError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from forge_admin.api.routes.chat_agents import chat_agent_store
from forge_admin.api.routes.common import NodeId, Session
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.conversations.audit import (
    ASSISTANT_APP,
    MAX_PAGE,
    UNKNOWN,
    Filters,
    Where,
    conversation,
    list_conversations,
    readable,
    subjects_named,
    subjects_of,
    who,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Conversation audit"])

AUDIT = "agents:audit"
NOT_FOUND = "There's no such conversation"
UNAVAILABLE = "The conversations' database isn't answering; try again shortly"

Channel = Literal["console", "api", "a2a", "assistant", "page", "unknown"]
Since = Literal["1d", "7d", "30d", "90d", "all"]
DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


class Person(BaseModel):
    """Who a conversation's user or caller is, when it's someone Forge knows."""

    kind: Literal["person", "api_key"]
    name: str
    email: str | None = None


class ConversationSummary(BaseModel):
    """A conversation, as the audit lists it."""

    #: Its ADK app: a chat agent's ID (``ca_…``), or ``forge`` for the assistant.
    app_name: str
    #: The agent's name; "Assistant" for the assistant.
    agent_name: str
    #: The conversation's user, as its session is kept: a person, an API key,
    #: or whatever ID an outside app or A2A caller gave.
    user_id: str
    session_id: str
    created_at: str | None
    updated_at: str | None
    events: int
    turns: int
    #: How its turns came in, in the order they first did; ``unknown`` for
    #: turns from before channels were kept.
    channels: list[str]
    #: Who called its turns (a person, ``apikey:<id>``, or the caller's ID).
    callers: list[str]
    last_status: str | None
    failed_turns: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    #: USD; null when no model it used has a known price.
    cost: float | None


class ConversationPage(BaseModel):
    """A page of conversations, the latest active first."""

    items: list[ConversationSummary]
    total: int
    #: The people and API keys the page names, by ID.
    people: dict[str, Person]
    #: The agents it may filter by: each chat agent's ID and name.
    agents: dict[str, str]


class ConversationDetail(BaseModel):
    """
    One conversation, whole: each event (``forge_task_adk_workflows.trace``:
    who said what, the tools called and what they answered, every state
    change), its state, its turns and its model and tool calls.
    """

    app_name: str
    agent_name: str
    user_id: str
    session_id: str
    created_at: str | None
    updated_at: str | None
    events: list[dict[str, Any]]
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    state: dict[str, Any]
    later_changes: bool
    omitted: int
    #: Only the turns asked from inside the organization are shown (an
    #: assistant conversation, read by an organization).
    partial: bool
    #: Its events as ADK's session API serves them (camelCase JSON, the
    #: latest 2,000), which the chat window replays as the person saw them.
    adk_events: list[dict[str, Any]]
    #: Each turn: ``{"id", "channel", "caller", "version", "status", "error",
    #: "started_at", "finished_at", "duration_ms", "organization_id"}``.
    turns: list[dict[str, Any]]
    #: Each model and tool call: ``{"id", "invocation_id", "author", "call",
    #: "name", "at", "input_tokens", "cached_tokens", "output_tokens",
    #: "thinking_tokens", "cost", "failed"}``.
    calls: list[dict[str, Any]]
    people: dict[str, Person]


@contextmanager
def _database() -> Iterator[None]:
    try:
        yield
    except (SQLAlchemyError, OSError) as error:
        logger.warning("The conversations' database failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def _agents(request: Request, organization_id: str) -> dict[str, str]:
    """:return: The organization's chat agents' names, by ID; none without MongoDB."""
    if getattr(request.app.state, "chat_agents", None) is None:
        return {}
    try:
        records = await chat_agent_store(request).list(organization_id)
    except PyMongoError as error:
        logger.warning("The chat agents' MongoDB failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    found: dict[str, str] = {}
    for record in records:
        document = record.get("document") or {}
        found[str(record["_id"])] = str(document.get("name") or record["_id"])
    return found


def _filters(
    agent: str | None,
    channel: str | None,
    subjects: tuple[str, ...],
    q: str | None,
    since: Since,
) -> Filters:
    days = DAYS.get(since)
    return Filters(
        agent=agent or None,
        channel=channel or None,
        subjects=subjects,
        text=q,
        since=datetime.now(UTC) - timedelta(days=days) if days else None,
    )


async def _page(
    request: Request,
    session: Session,
    where: Where,
    names: dict[str, str],
    *,
    q: str | None,
    agent: str | None,
    channel: str | None,
    since: Since,
    limit: int,
    offset: int,
) -> ConversationPage:
    with _database():
        subjects = await subjects_named(
            session, q or "", organization_id=where.organization_id
        )
        found, total = await list_conversations(
            _engine(request),
            where,
            _filters(agent, channel, subjects, q, since),
            limit=limit,
            offset=offset,
        )
        people = await who(session, subjects_of(found))
    return ConversationPage(
        items=[
            ConversationSummary(
                **{
                    **summary.__dict__,
                    "agent_name": "Assistant"
                    if summary.app_name == ASSISTANT_APP
                    else names.get(summary.app_name, summary.app_name),
                    "channels": summary.channels or [UNKNOWN],
                }
            )
            for summary in found
        ],
        total=total,
        people={key: Person(**value) for key, value in people.items()},
        agents=names,
    )


async def _detail(
    request: Request,
    session: Session,
    where: Where,
    names: dict[str, str],
    *,
    session_id: str,
    app: str,
    user_id: str,
) -> ConversationDetail:
    with _database():
        if not await readable(_engine(request), where, app, session_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        found = await conversation(
            _engine(request),
            request.app.state.adk_run_sessions,
            where,
            app_name=app,
            user_id=user_id,
            session_id=session_id,
        )
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        subjects = {
            user_id,
            *(turn["caller"] for turn in found["turns"] if turn["caller"]),
        }
        people = await who(session, subjects)
    return ConversationDetail(
        **found,
        agent_name="Assistant" if app == ASSISTANT_APP else names.get(app, app),
        people={key: Person(**value) for key, value in people.items()},
    )


Q = Annotated[
    str | None,
    Query(
        max_length=200, description="A name, email, API key name, user or session ID"
    ),
]
AgentFilter = Annotated[
    str | None, Query(max_length=128, description="A chat agent's ID, or assistant")
]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0)]
App = Annotated[str, Query(min_length=1, max_length=128)]
UserId = Annotated[str, Query(alias="user", min_length=1, max_length=128)]


@router.get("/organizations/{organization_id}/conversations")
async def list_organization_conversations(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    q: Q = None,
    agent: AgentFilter = None,
    channel: Channel | None = None,
    since: Since = "all",
    limit: Limit = 50,
    offset: Offset = 0,
) -> ConversationPage:
    """
    The organization's conversations, the latest active first: its chat
    agents' (every channel) and the assistant's asked from inside it, each
    with its user, callers, channels, turns and usage. ``q`` finds people by
    name or email, API keys by name, and user, caller and session IDs.
    \f
    :raises HTTPException: 403 without agents:audit in the organization; 503
        when the database isn't answering.
    """
    await authorize(session, enforcer, user, AUDIT, Scope(Level.ORG, organization_id))
    names = await _agents(request, organization_id)
    where = Where(organization_id, tuple(names))
    return await _page(
        request,
        session,
        where,
        names,
        q=q,
        agent=agent,
        channel=channel,
        since=since,
        limit=limit,
        offset=offset,
    )


@router.get("/organizations/{organization_id}/conversations/{session_id}")
async def get_organization_conversation(
    organization_id: NodeId,
    session_id: str,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    app: App,
    user_id: UserId,
) -> ConversationDetail:
    """
    One of the organization's conversations, whole. An assistant
    conversation shows only the turns asked from inside the organization.
    \f
    :raises HTTPException: 403 without agents:audit in the organization; 404
        for a conversation that isn't the organization's; 503 when the
        database isn't answering.
    """
    await authorize(session, enforcer, user, AUDIT, Scope(Level.ORG, organization_id))
    names = await _agents(request, organization_id)
    return await _detail(
        request,
        session,
        Where(organization_id, tuple(names)),
        names,
        session_id=session_id,
        app=app,
        user_id=user_id,
    )


@router.get("/conversations")
async def list_site_conversations(
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    q: Q = None,
    channel: Channel | None = None,
    since: Since = "all",
    limit: Limit = 50,
    offset: Offset = 0,
) -> ConversationPage:
    """
    Every assistant conversation, asked from inside an organization or not,
    the latest active first.
    \f
    :raises HTTPException: 403 without agents:audit on the site; 503 when the
        database isn't answering.
    """
    await authorize(session, enforcer, user, AUDIT, SITE)
    return await _page(
        request,
        session,
        Where(None),
        {},
        q=q,
        agent=None,
        channel=channel,
        since=since,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{session_id}")
async def get_site_conversation(
    session_id: str,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    app: App,
    user_id: UserId,
) -> ConversationDetail:
    """
    Any conversation, whole: the assistant's, or an organization's chat agent's.
    \f
    :raises HTTPException: 403 without agents:audit on the site; 404 when
        there's no such conversation; 503 when the database isn't answering.
    """
    await authorize(session, enforcer, user, AUDIT, SITE)
    return await _detail(
        request,
        session,
        Where(None),
        {},
        session_id=session_id,
        app=app,
        user_id=user_id,
    )
