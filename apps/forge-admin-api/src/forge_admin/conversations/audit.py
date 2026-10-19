"""
Conversations, for their audit: the ADK sessions of the organizations' chat
agents and of the assistant (all in this API's database: ADK's ``sessions``
and ``events``), each with how its turns came in, who held them and what
they used (``usage_invocations`` and ``usage_calls``, by session).

An organization's are its chat agents' conversations (whatever the channel:
the builder's chat, the run API, A2A) and the assistant's turns asked from
inside it; the site's are every assistant conversation. An organization reads
only the assistant turns asked from inside it: their events, and the state
they made.

A turn's channel (``console``, ``api``, ``a2a``, ``assistant``, ``page``) is
kept from migration 0024 on; a conversation with none is ``unknown``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from forge_common.adk.turns import TURN_KEY
from forge_task_adk_workflows.trace import MAX_EVENTS, session_trace
from forge_task_adk_workflows.usage_store import calls, invocations
from google.adk.events import Event
from google.adk.sessions import BaseSessionService
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge_admin.auth.api_keys import SUBJECT_PREFIX, key_id_of
from forge_admin.models import ApiKey, User

#: The assistant's ADK app (``forge_admin.assistant.forge.APP_NAME``).
ASSISTANT_APP = "forge"
#: How turns come in.
CHANNELS = ("console", "api", "a2a", "assistant", "page")
#: A conversation none of whose turns says how it came in.
UNKNOWN = "unknown"
#: The most conversations a page lists.
MAX_PAGE = 100

# ADK's own tables (google.adk.sessions.schemas), read only.
adk_sessions = sa.table(
    "sessions",
    sa.column("app_name", sa.String),
    sa.column("user_id", sa.String),
    sa.column("id", sa.String),
    sa.column("create_time", sa.DateTime),
    sa.column("update_time", sa.DateTime),
)
adk_events = sa.table(
    "events",
    sa.column("id", sa.String),
    sa.column("app_name", sa.String),
    sa.column("user_id", sa.String),
    sa.column("session_id", sa.String),
)


@dataclass(frozen=True)
class Where:
    """
    Whose conversations: an organization's (its chat agents', and the
    assistant's asked from inside it), or the site's (every assistant
    conversation).

    :ivar organization_id: The organization; None for the site.
    :ivar agents: The organization's chat agents' IDs (their ADK apps).
    """

    organization_id: str | None
    agents: tuple[str, ...] = ()

    def sessions(self) -> sa.ColumnElement[bool]:
        """:return: Which ADK sessions are its conversations."""
        assistant = adk_sessions.c.app_name == ASSISTANT_APP
        if self.organization_id is None:
            return assistant
        asked_here = sa.select(invocations.c.session_id).where(
            invocations.c.kind == "assistant",
            invocations.c.organization_id == self.organization_id,
        )
        mine = sa.and_(assistant, adk_sessions.c.id.in_(asked_here))
        if not self.agents:
            return mine
        return sa.or_(adk_sessions.c.app_name.in_(self.agents), mine)

    def usage(self, table: sa.TableClause | sa.Table) -> sa.ColumnElement[bool]:
        """:return: Which usage rows it reads: its organization's; every one for the site."""
        if self.organization_id is None:
            return sa.true()
        return table.c.organization_id == self.organization_id


@dataclass(frozen=True)
class Filters:
    """
    :ivar agent: One chat agent's (its ID), or ``assistant``'s.
    :ivar channel: One of :data:`CHANNELS`, or :data:`UNKNOWN`.
    :ivar subjects: Users and API keys (``apikey:<id>``) the search found by
        name: their conversations, or the ones they called into.
    :ivar text: What was searched: a conversation user's or a caller's ID,
        or a session's, holding it.
    :ivar since: Active since.
    """

    agent: str | None = None
    channel: str | None = None
    subjects: tuple[str, ...] = ()
    text: str | None = None
    since: datetime | None = None


@dataclass
class Summary:
    """A conversation as the list shows it."""

    app_name: str
    user_id: str
    session_id: str
    created_at: str | None
    updated_at: str | None
    events: int = 0
    turns: int = 0
    channels: list[str] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    last_status: str | None = None
    failed_turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(when: Any) -> str | None:
    if not isinstance(when, datetime):
        return None
    aware = when.replace(tzinfo=UTC) if when.tzinfo is None else when
    return aware.astimezone(UTC).isoformat()


def _conditions(where: Where, filters: Filters) -> list[sa.ColumnElement[bool]]:
    found: list[sa.ColumnElement[bool]] = [where.sessions()]
    if filters.agent == "assistant":
        found.append(adk_sessions.c.app_name == ASSISTANT_APP)
    elif filters.agent:
        found.append(adk_sessions.c.app_name == filters.agent)
    if filters.channel == UNKNOWN:
        said = sa.select(invocations.c.session_id).where(
            invocations.c.channel.is_not(None), where.usage(invocations)
        )
        found.append(adk_sessions.c.id.not_in(said))
    elif filters.channel:
        came = sa.select(invocations.c.session_id).where(
            invocations.c.channel == filters.channel, where.usage(invocations)
        )
        found.append(adk_sessions.c.id.in_(came))
    text = (filters.text or "").strip()
    if text or filters.subjects:
        callers: list[sa.ColumnElement[bool]] = []
        held: list[sa.ColumnElement[bool]] = []
        if filters.subjects:
            held.append(adk_sessions.c.user_id.in_(filters.subjects))
            callers.append(invocations.c.user_id.in_(filters.subjects))
        if text:
            held.append(adk_sessions.c.user_id.like(_like(text), escape="\\"))
            held.append(adk_sessions.c.id.like(_like(text), escape="\\"))
            callers.append(invocations.c.user_id.like(_like(text), escape="\\"))
        called = sa.select(invocations.c.session_id).where(
            sa.or_(*callers), where.usage(invocations)
        )
        found.append(sa.or_(*held, adk_sessions.c.id.in_(called)))
    if filters.since is not None:
        found.append(adk_sessions.c.update_time >= filters.since.replace(tzinfo=None))
    return found


async def list_conversations(
    engine: AsyncEngine,
    where: Where,
    filters: Filters,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Summary], int]:
    """
    :return: A page of the conversations, the latest active first, each with
        its turns, channels, callers and usage; and how many there are.
    """
    conditions = _conditions(where, filters)
    page = (
        sa.select(
            adk_sessions.c.app_name,
            adk_sessions.c.user_id,
            adk_sessions.c.id,
            adk_sessions.c.create_time,
            adk_sessions.c.update_time,
        )
        .where(*conditions)
        .order_by(adk_sessions.c.update_time.desc(), adk_sessions.c.id)
        .limit(min(max(limit, 1), MAX_PAGE))
        .offset(max(offset, 0))
    )
    total = sa.select(sa.func.count()).select_from(adk_sessions).where(*conditions)
    async with engine.connect() as conn:
        rows = (await conn.execute(page)).all()
        count = int((await conn.execute(total)).scalar_one())
        found = [
            Summary(
                app_name=row.app_name,
                user_id=row.user_id,
                session_id=row.id,
                created_at=_iso(row.create_time),
                updated_at=_iso(row.update_time),
            )
            for row in rows
        ]
        await _summarize(conn, where, found)
    return found, count


async def _summarize(conn: Any, where: Where, found: list[Summary]) -> None:
    # Each conversation's events, turns and usage, for the page's only.
    if not found:
        return
    ids = [each.session_id for each in found]
    by_id = {each.session_id: each for each in found}
    events = await conn.execute(
        sa.select(adk_events.c.session_id, sa.func.count())
        .where(adk_events.c.session_id.in_(ids))
        .group_by(adk_events.c.session_id)
    )
    for session_id, count in events.all():
        if session_id in by_id:
            by_id[session_id].events = int(count)
    turns = await conn.execute(
        sa.select(
            invocations.c.session_id,
            invocations.c.channel,
            invocations.c.user_id,
            invocations.c.status,
            invocations.c.started_at,
        )
        .where(invocations.c.session_id.in_(ids), where.usage(invocations))
        .order_by(invocations.c.started_at)
    )
    for row in turns.all():
        each = by_id.get(row.session_id)
        if each is None:
            continue
        each.turns += 1
        each.last_status = row.status
        each.failed_turns += row.status == "failed"
        if row.channel and row.channel not in each.channels:
            each.channels.append(row.channel)
        if row.user_id and row.user_id not in each.callers:
            each.callers.append(row.user_id)
    used = await conn.execute(
        sa.select(
            calls.c.session_id,
            calls.c.call,
            sa.func.count(),
            sa.func.sum(calls.c.input_tokens),
            sa.func.sum(calls.c.output_tokens),
            sa.func.sum(calls.c.cost),
        )
        .where(calls.c.session_id.in_(ids), where.usage(calls))
        .group_by(calls.c.session_id, calls.c.call)
    )
    for session_id, call, count, tokens_in, tokens_out, cost in used.all():
        each = by_id.get(session_id)
        if each is None:
            continue
        if call == "model":
            each.model_calls += int(count)
            each.input_tokens += int(tokens_in or 0)
            each.output_tokens += int(tokens_out or 0)
            if cost is not None:
                each.cost = (each.cost or 0.0) + float(cost)
        else:
            each.tool_calls += int(count)


async def readable(
    engine: AsyncEngine, where: Where, app_name: str, session_id: str
) -> bool:
    """:return: Whether the conversation is one of ``where``'s."""
    if where.organization_id is None:
        return True
    if app_name in where.agents:
        return True
    if app_name != ASSISTANT_APP:
        return False
    async with engine.connect() as conn:
        asked = await conn.execute(
            sa.select(sa.func.count())
            .select_from(invocations)
            .where(
                invocations.c.session_id == session_id,
                invocations.c.kind == "assistant",
                invocations.c.organization_id == where.organization_id,
            )
        )
        return int(asked.scalar_one()) > 0


async def conversation(
    engine: AsyncEngine,
    sessions: BaseSessionService,
    where: Where,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """
    One conversation, all of it ``where`` may read: its events (each
    message, tool call and answer, state change), its state, its turns (how
    each came in, who held it, how it ended) and its model and tool calls
    with their tokens and cost; and its events as ADK's session API serves
    them (``adk_events``: camelCase JSON), for the chat window to replay.

    :return: None when there's no such conversation.
    """
    session = await sessions.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        return None
    partial = where.organization_id is not None and app_name == ASSISTANT_APP
    async with engine.connect() as conn:
        turn_rows = (
            await conn.execute(
                sa.select(invocations)
                .where(invocations.c.session_id == session_id, where.usage(invocations))
                .order_by(invocations.c.started_at)
            )
        ).all()
        call_rows = (
            await conn.execute(
                sa.select(calls)
                .where(calls.c.session_id == session_id, where.usage(calls))
                .order_by(calls.c.at, calls.c.id)
            )
        ).all()
    # An organization reads the assistant's turns asked from inside it only.
    allowed = (
        {row.invocation_id for row in call_rows if row.invocation_id}
        if partial
        else None
    )
    trace = session_trace(session, invocations=allowed)
    # The same events as ADK's own session API serves them, for the chat
    # window to replay as the person saw it.
    shown = []
    allowed_runs = {row.id for row in turn_rows}
    for event in session.events:
        if allowed is None or event.invocation_id in allowed:
            shown.append(event)
            continue
        # A failure before the first model/tool call has no usage_call linking
        # its ADK invocation to this organization. Its server-owned run ID still
        # links the terminal outcome to an authorized audit row. Project only
        # that outcome; unrelated content, state and metadata stay private.
        terminal = (event.custom_metadata or {}).get(TURN_KEY)
        if (
            isinstance(terminal, dict)
            and terminal.get("version") == 1
            and terminal.get("status") in ("failed", "cancelled")
            and isinstance(terminal.get("runId"), str)
            and terminal["runId"] in allowed_runs
        ):
            shown.append(
                Event(
                    id=event.id,
                    author="assistant",
                    timestamp=event.timestamp,
                    invocation_id=event.invocation_id,
                    custom_metadata={
                        TURN_KEY: {
                            key: value
                            for key, value in terminal.items()
                            if key in ("version", "runId", "status")
                            or (
                                key in ("errorCode", "errorMessage")
                                and isinstance(value, str)
                            )
                        }
                    },
                )
            )
    return {
        **trace,
        "app_name": app_name,
        "user_id": user_id,
        "session_id": session_id,
        "created_at": _iso(datetime.fromtimestamp(session.events[0].timestamp, UTC))
        if session.events
        else None,
        "updated_at": _iso(datetime.fromtimestamp(session.last_update_time, UTC)),
        "partial": partial,
        "adk_events": [
            event.model_dump(mode="json", by_alias=True, exclude_none=True)
            for event in shown[-MAX_EVENTS:]
        ],
        "turns": [
            {
                "id": row.id,
                "channel": row.channel,
                "caller": row.user_id,
                "version": row.version,
                "status": row.status,
                "error": row.error,
                "started_at": _iso(row.started_at),
                "finished_at": _iso(row.finished_at),
                "duration_ms": row.duration_ms,
                "organization_id": row.organization_id or None,
            }
            for row in turn_rows
        ],
        "calls": [
            {
                "id": row.id,
                "invocation_id": row.invocation_id,
                "author": row.author,
                "call": row.call,
                "name": row.name,
                "at": _iso(row.at),
                "input_tokens": row.input_tokens,
                "cached_tokens": row.cached_tokens,
                "output_tokens": row.output_tokens,
                "thinking_tokens": row.thinking_tokens,
                "cost": row.cost,
                "failed": row.failed,
            }
            for row in call_rows
        ],
    }


# ------------------------------------------------------------------- people


async def subjects_named(
    session: AsyncSession, text: str, *, organization_id: str | None
) -> tuple[str, ...]:
    """
    :param text: What was searched.
    :param organization_id: Whose API keys it may find; every one's for None.
    :return: The users whose name or email holds it, and the API keys
        (``apikey:<id>``) whose name does, at most 200.
    """
    text = text.strip()
    if not text:
        return ()
    pattern = _like(text)
    full = User.first_name + " " + User.last_name
    people = await session.execute(
        sa.select(User.id)
        .where(
            sa.or_(
                full.like(pattern, escape="\\"),
                User.email.like(pattern, escape="\\"),
                User.id == text,
            )
        )
        .limit(200)
    )
    keys_query = sa.select(ApiKey.id).where(ApiKey.name.like(pattern, escape="\\"))
    if organization_id is not None:
        keys_query = keys_query.where(ApiKey.organization_id == organization_id)
    keys = await session.execute(keys_query.limit(200))
    return (*people.scalars(), *(f"{SUBJECT_PREFIX}{key}" for key in keys.scalars()))


async def who(
    session: AsyncSession, subjects: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """
    :return: Each subject known here, by its ID: a person (``kind``
        ``person``, ``name``, ``email``) or an API key (``api_key``, ``name``).
        Anyone else (an outside app's own user ID, an A2A caller) isn't listed.
    """
    wanted = {subject for subject in subjects if subject}
    keys = {key_id_of(subject): subject for subject in wanted if key_id_of(subject)}
    people = wanted - set(keys.values())
    found: dict[str, dict[str, Any]] = {}
    if people:
        rows = await session.execute(sa.select(User).where(User.id.in_(people)))
        for user in rows.scalars():
            name = f"{user.first_name} {user.last_name}".strip() or user.email
            found[user.id] = {"kind": "person", "name": name, "email": user.email}
    if keys:
        rows = await session.execute(sa.select(ApiKey).where(ApiKey.id.in_(list(keys))))
        for key in rows.scalars():
            found[keys[key.id]] = {"kind": "api_key", "name": key.name, "email": None}
    return found


def subjects_of(summaries: Collection[Summary]) -> set[str]:
    """:return: Every conversation user and caller the page names."""
    return {s.user_id for s in summaries} | {
        caller for s in summaries for caller in s.callers
    }
