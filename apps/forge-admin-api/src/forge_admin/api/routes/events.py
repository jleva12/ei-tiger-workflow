"""Events other systems send an organization: its inbound endpoint, the event
types it accepts with the JSON Schema of each, and the events received.

An organization turns its endpoint on and gets a token, shown once; senders
post each event to ``/hooks/events/<endpoint>/<event type key>`` with it (see
``event_hooks.py``). Reading any of it takes ``organizations:read`` in the
organization; turning the endpoint on or off, rotating its token and defining
event types take ``events:manage``, the organization admins'.
"""

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
)
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.config import Settings
from forge_admin.db.audit import UtcDateTime
from forge_admin.events import (
    EVENT_KEY_PATTERN,
    check_schema,
    new_token,
    validate_payload,
)
from forge_admin.models import Event, EventEndpoint, EventType

router = APIRouter(tags=["events"])

EventTypeId = Annotated[str, Path(max_length=36)]
EventId = Annotated[str, Path(max_length=36)]
EventKey = Annotated[str, StringConstraints(pattern=EVENT_KEY_PATTERN)]
EventStatus = Literal["valid", "invalid"]
# A type starts as a draft; turning it on makes it active, and it can be
# paused and turned on again, never made a draft again.
TypeStatus = Literal["draft", "active", "paused"]
MANAGE = "events:manage"
KEY_TAKEN = "The organization has an event type with this key"


class EndpointRead(Audited):
    id: str
    organization_id: str
    enabled: bool
    # Its token's last characters.
    token_hint: str
    # Where senders post, followed by /<event type key>.
    url: str
    # The token itself, only in the answer that made it.
    token: str | None = None


class EndpointUpdate(BaseModel):
    enabled: bool


class EventTypeCreate(BaseModel):
    key: EventKey
    name: Name
    description: str = ""
    payload_schema: dict[str, Any]


class EventTypeUpdate(BaseModel):
    key: EventKey | None = None
    name: Name | None = None
    description: str | None = None
    # Turn it on, or pause it.
    status: Literal["active", "paused"] | None = None
    payload_schema: dict[str, Any] | None = None


class EventTypeRead(Audited):
    id: str
    organization_id: str
    key: str
    name: str
    description: str
    status: TypeStatus
    payload_schema: dict[str, Any]
    schema_version: int
    # What it has received.
    event_count: int = 0
    invalid_count: int = 0
    last_received_at: UtcDateTime | None = None


class IssueRead(BaseModel):
    path: str
    message: str
    keyword: str


class EventSummary(Audited):
    id: str
    organization_id: str
    event_type_id: str
    event_key: str
    schema_version: int
    status: EventStatus
    errors: list[IssueRead]
    idempotency_key: str | None
    size_bytes: int
    content_type: str
    user_agent: str
    source_ip: str


class EventRead(EventSummary):
    payload: Any


class SchemaTrial(BaseModel):
    payload_schema: dict[str, Any]
    payload: Any = None


class SchemaTrialResult(BaseModel):
    valid: bool
    errors: list[IssueRead]


def endpoint_url(settings: Settings, request: Request, endpoint_id: str) -> str:
    """
    :return: Where senders post to an endpoint, before the event type's key.
    """
    base = settings.public_url or str(request.base_url)
    return f"{base.rstrip('/')}/hooks/events/{endpoint_id}"


def _endpoint_read(
    request: Request, endpoint: EventEndpoint, token: str | None = None
) -> EndpointRead:
    return as_read(
        EndpointRead,
        endpoint,
        url=endpoint_url(request.app.state.settings, request, endpoint.id),
        token=token,
    )


async def _endpoint_of(
    session: AsyncSession, organization_id: str
) -> EventEndpoint | None:
    return await session.scalar(
        select(EventEndpoint).where(EventEndpoint.organization_id == organization_id)
    )


def _checked(schema: dict[str, Any]) -> dict[str, Any]:
    """:raises HTTPException: 422 with why the schema can't be used."""
    try:
        return check_schema(schema)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


async def _stats(
    session: AsyncSession, organization_id: str, event_type_id: str | None = None
) -> dict[str, tuple[int, int, datetime | None]]:
    """:return: Each event type's events, invalid ones and latest, by its ID."""
    query = (
        select(
            Event.event_type_id,
            func.count(Event.id),
            func.coalesce(func.sum(case((Event.status == "invalid", 1), else_=0)), 0),
            func.max(Event.created_at),
        )
        .where(Event.organization_id == organization_id)
        .group_by(Event.event_type_id)
    )
    if event_type_id is not None:
        query = query.where(Event.event_type_id == event_type_id)
    return {
        str(key): (int(count), int(invalid), latest)
        for key, count, invalid, latest in await session.execute(query)
    }


def _type_read(
    event_type: EventType, stats: dict[str, tuple[int, int, datetime | None]]
) -> EventTypeRead:
    count, invalid, latest = stats.get(event_type.id, (0, 0, None))
    return as_read(
        EventTypeRead,
        event_type,
        event_count=count,
        invalid_count=invalid,
        last_received_at=latest,
    )


async def _type_of(
    session: AsyncSession, organization_id: str, event_type_id: str
) -> EventType:
    """:raises HTTPException: 404 for another organization's event type, or none."""
    found = await session.get(EventType, event_type_id)
    if found is None or found.organization_id != organization_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The organization has no such event type"
        )
    return found


@router.get("/organizations/{organization_id}/event-endpoint")
async def get_endpoint(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EndpointRead | None:
    """
    An organization's inbound events endpoint, or null before it's turned on the
    first time.
    \f
    :param organization_id: The organization.
    :param request: The request, for the endpoint's address.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The endpoint, without its token.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    endpoint = await _endpoint_of(session, organization_id)
    return None if endpoint is None else _endpoint_read(request, endpoint)


@router.put("/organizations/{organization_id}/event-endpoint")
async def set_endpoint(
    organization_id: NodeId,
    body: EndpointUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EndpointRead:
    """
    Turn an organization's inbound events endpoint on or off. Turning it on the first
    time makes it, with a token that this answer alone carries.
    \f
    :param organization_id: The organization.
    :param body: Whether it accepts events.
    :param request: The request, for the endpoint's address.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The endpoint, with its token when it was just made.
    :raises HTTPException: 403 without ``events:manage`` in the organization.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    endpoint = await _endpoint_of(session, organization_id)
    token = None
    if endpoint is None:
        made = new_token()
        token = made.value
        endpoint = EventEndpoint(
            organization_id=organization_id,
            enabled=body.enabled,
            token_sha256=made.sha256,
            token_hint=made.hint,
        )
        session.add(endpoint)
    else:
        endpoint.enabled = body.enabled
    # Two admins turning it on at once: the second finds the first's.
    await commit_or_conflict(
        session, "The organization's endpoint was just made; reload it"
    )
    return _endpoint_read(request, endpoint, token)


@router.post("/organizations/{organization_id}/event-endpoint/token")
async def rotate_token(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EndpointRead:
    """
    Replace an organization's endpoint token. The old one stops working at once.
    \f
    :param organization_id: The organization.
    :param request: The request, for the endpoint's address.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The endpoint, with the new token, which only this answer carries.
    :raises HTTPException: 403 without ``events:manage`` in the organization; 404
        before the endpoint is made.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    endpoint = await _endpoint_of(session, organization_id)
    if endpoint is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Turn the organization's endpoint on first"
        )
    made = new_token()
    endpoint.token_sha256 = made.sha256
    endpoint.token_hint = made.hint
    await session.commit()
    return _endpoint_read(request, endpoint, made.value)


@router.get("/organizations/{organization_id}/event-types")
async def list_event_types(
    organization_id: NodeId, user: CurrentUser, session: Session, enforcer: Enforcer
) -> list[EventTypeRead]:
    """
    List an organization's event types by name, each with what it has received.
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The event types.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    found = await session.scalars(
        select(EventType)
        .where(EventType.organization_id == organization_id)
        .order_by(EventType.name, EventType.key)
    )
    stats = await _stats(session, organization_id)
    return [_type_read(event_type, stats) for event_type in found]


@router.post(
    "/organizations/{organization_id}/event-types", status_code=status.HTTP_201_CREATED
)
async def create_event_type(
    organization_id: NodeId,
    body: EventTypeCreate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EventTypeRead:
    """
    Define an event type the organization accepts, with the JSON Schema its payload
    must match: draft 2020-12, an object at the root, references only
    within itself. It's a draft, which the endpoint refuses, until the organization
    turns it on.
    \f
    :param organization_id: The organization.
    :param body: Its key, unique in the organization, name, description and
        schema.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The event type, a draft at schema version 1.
    :raises HTTPException: 403 without ``events:manage`` in the organization; 409
        when the organization has an event type with the key; 422 for a schema it
        can't use.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    event_type = EventType(
        organization_id=organization_id,
        key=body.key,
        name=body.name,
        description=body.description,
        status="draft",
        payload_schema=_checked(body.payload_schema),
        schema_version=1,
    )
    session.add(event_type)
    await commit_or_conflict(session, KEY_TAKEN)
    return _type_read(event_type, {})


@router.get("/organizations/{organization_id}/event-types/{event_type_id}")
async def get_event_type(
    organization_id: NodeId,
    event_type_id: EventTypeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EventTypeRead:
    """
    One of an organization's event types, with what it has received.
    \f
    :param organization_id: The organization.
    :param event_type_id: The event type.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The event type.
    :raises HTTPException: 404 for another organization's event type, or none.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    event_type = await _type_of(session, organization_id, event_type_id)
    return _type_read(event_type, await _stats(session, organization_id, event_type.id))


@router.patch("/organizations/{organization_id}/event-types/{event_type_id}")
async def update_event_type(
    organization_id: NodeId,
    event_type_id: EventTypeId,
    body: EventTypeUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EventTypeRead:
    """
    Change an event type, or turn it on (``active``) or pause it. A new
    schema applies to events received from now on and bumps its version;
    events already received keep the version they were checked against. A
    new key changes the URL senders use.
    \f
    :param organization_id: The organization.
    :param event_type_id: The event type.
    :param body: The fields to change; absent ones are left alone.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The event type.
    :raises HTTPException: 403 without ``events:manage`` in the organization; 404
        for another organization's event type, or none; 409 when another of the
        organization's event types has the new key; 422 for a schema it can't use.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    event_type = await _type_of(session, organization_id, event_type_id)
    if body.key is not None:
        event_type.key = body.key
    if body.name is not None:
        event_type.name = body.name
    if body.description is not None:
        event_type.description = body.description
    if body.status is not None:
        event_type.status = body.status
    if body.payload_schema is not None and json.dumps(
        body.payload_schema
    ) != json.dumps(event_type.payload_schema):
        schema = _checked(body.payload_schema)
        # Reordering properties is saved but checks the same; only a schema
        # that checks differently is a new version.
        if schema != event_type.payload_schema:
            event_type.schema_version += 1
        event_type.payload_schema = schema
    await commit_or_conflict(session, KEY_TAKEN)
    return _type_read(event_type, await _stats(session, organization_id, event_type.id))


@router.delete(
    "/organizations/{organization_id}/event-types/{event_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_event_type(
    organization_id: NodeId,
    event_type_id: EventTypeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete an event type and every event it received. Senders still
    posting it get 404.
    \f
    :param organization_id: The organization.
    :param event_type_id: The event type.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :raises HTTPException: 403 without ``events:manage`` in the organization; 404
        for another organization's event type, or none.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    event_type = await _type_of(session, organization_id, event_type_id)
    await session.delete(event_type)
    await session.commit()


def _summary_fields(event: Event, key: str) -> dict[str, Any]:
    return {"event_key": key, "errors": event.errors}


@router.get("/organizations/{organization_id}/events")
async def list_events(
    organization_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    event_type_id: Annotated[str | None, Query(max_length=36)] = None,
    status_: Annotated[EventStatus | None, Query(alias="status")] = None,
) -> list[EventSummary]:
    """
    List the events an organization received, latest first, without their payloads.
    \f
    :param organization_id: The organization.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param limit: How many to return.
    :param offset: How many to skip, for the next page.
    :param event_type_id: Only this event type's.
    :param status_: Only the ``valid`` or the ``invalid`` ones.
    :return: The events.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    query = (
        select(Event, EventType.key)
        .join(EventType, Event.event_type_id == EventType.id)
        .where(Event.organization_id == organization_id)
        .options(defer(Event.payload))
    )
    if event_type_id:
        query = query.where(Event.event_type_id == event_type_id)
    if status_:
        query = query.where(Event.status == status_)
    rows = await session.execute(
        query.order_by(Event.created_at.desc(), Event.id).offset(offset).limit(limit)
    )
    return [
        as_read(EventSummary, event, **_summary_fields(event, key))
        for event, key in rows
    ]


@router.get("/organizations/{organization_id}/events/{event_id}")
async def get_event(
    organization_id: NodeId,
    event_id: EventId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> EventRead:
    """
    One event an organization received, with its payload as sent.
    \f
    :param organization_id: The organization.
    :param event_id: The event.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The event.
    :raises HTTPException: 404 for another organization's event, or none.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    row = (
        await session.execute(
            select(Event, EventType.key)
            .join(EventType, Event.event_type_id == EventType.id)
            .where(Event.id == event_id, Event.organization_id == organization_id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The organization has no such event"
        )
    event, key = row
    return as_read(
        EventRead, event, payload=event.payload, **_summary_fields(event, key)
    )


@router.post("/organizations/{organization_id}/event-schemas/validate")
async def try_schema(
    organization_id: NodeId,
    body: SchemaTrial,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SchemaTrialResult:
    """
    Check a sample payload against a schema, saved or not, as the endpoint
    would; nothing is stored.
    \f
    :param organization_id: The organization.
    :param body: The schema and the payload.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: Whether it matches, and where it doesn't.
    :raises HTTPException: 422 for a schema an event type couldn't use.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    schema = _checked(body.payload_schema)
    issues = await asyncio.to_thread(validate_payload, schema, body.payload)
    return SchemaTrialResult(
        valid=not issues, errors=[IssueRead(**issue.as_dict()) for issue in issues]
    )
