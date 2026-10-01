"""Where other systems send an organization events: ``POST
/hooks/events/<endpoint>/<event type key>``, outside the API prefix.

Senders aren't Forge users: they authenticate with the endpoint's token,
as ``Authorization: Bearer <token>`` or ``X-Forge-Token: <token>``, and
never with the API key, so ``/hooks`` can be exposed on its own. Every
event of a known, accepted type is kept, valid or not, with how it measured
up to the type's schema. An ``Idempotency-Key`` header makes retries safe:
the same key and type answer with the event already kept.
"""

import asyncio
import hmac
import json
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import Session
from forge_admin.auth.access import UUID_PATTERN
from forge_admin.db.audit import UtcDateTime, set_actor
from forge_admin.events import EVENT_KEY_PATTERN, token_digest, validate_payload
from forge_admin.models import Event, EventEndpoint, EventType

router = APIRouter(prefix="/hooks", tags=["hooks"])

EndpointId = Annotated[str, Path(max_length=36)]
EventKey = Annotated[str, Path(max_length=100)]
BAD_CREDENTIALS = "Invalid endpoint or token"
# Compared against when the endpoint doesn't exist, so a wrong ID takes as
# long to refuse as a wrong token.
_NO_DIGEST = token_digest("")


class IssueRead(BaseModel):
    path: str
    message: str
    keyword: str


class Receipt(BaseModel):
    """What the sender gets back: the event kept, and whether it's valid."""

    id: str
    event_type: str
    status: Literal["valid", "invalid"]
    schema_version: int
    received_at: UtcDateTime
    errors: list[IssueRead] = []
    # The event was already received with this Idempotency-Key.
    duplicate: bool = False


def _refuse(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _token_of(authorization: str | None, forge_token: str | None) -> str:
    """:raises HTTPException: 401 without a token."""
    if forge_token:
        return forge_token.strip()
    scheme, _, credentials = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    raise _refuse("Send the endpoint's token as Authorization: Bearer <token>")


async def _endpoint(
    session: AsyncSession, endpoint_id: str, token: str
) -> EventEndpoint:
    """:raises HTTPException: 401 for an unknown endpoint or a wrong token."""
    found = None
    if re.fullmatch(UUID_PATTERN, endpoint_id):
        found = await session.get(EventEndpoint, endpoint_id)
    expected = found.token_sha256 if found is not None else _NO_DIGEST
    matches = hmac.compare_digest(token_digest(token), expected)
    if found is None or not matches:
        raise _refuse(BAD_CREDENTIALS)
    return found


async def _body(request: Request, limit: int) -> bytes:
    """:raises HTTPException: 413 over the limit."""
    too_large = HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, f"Events are at most {limit} bytes"
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large
    return bytes(body)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} isn't JSON")


def _parse(body: bytes) -> Any:
    """:raises HTTPException: 400 when the body isn't JSON."""
    if not body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is empty")
    try:
        # NaN and Infinity aren't JSON, and MySQL wouldn't store them.
        return json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"The body isn't JSON: {error}"
        ) from None


def _receipt(event: Event, key: str, *, duplicate: bool = False) -> Receipt:
    return Receipt(
        id=event.id,
        event_type=key,
        status="valid" if event.status == "valid" else "invalid",
        schema_version=event.schema_version,
        received_at=event.created_at,
        errors=[IssueRead(**issue) for issue in event.errors],
        duplicate=duplicate,
    )


async def _kept(
    session: AsyncSession, event_type_id: str, idempotency_key: str
) -> Event | None:
    return await session.scalar(
        select(Event).where(
            Event.event_type_id == event_type_id,
            Event.idempotency_key == idempotency_key,
        )
    )


@router.post(
    "/events/{endpoint_id}/{event_key}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"description": "Already received with this Idempotency-Key"},
        400: {"description": "The body isn't JSON"},
        401: {"description": "Unknown endpoint, or a missing or wrong token"},
        403: {"description": "The endpoint is turned off"},
        404: {"description": "The organization has no event type with this key"},
        409: {"description": "The event type is a draft or paused"},
        413: {"description": "The body is too large"},
        422: {"description": "Kept, but it doesn't match the event type's schema"},
    },
)
async def receive_event(
    endpoint_id: EndpointId,
    event_key: EventKey,
    request: Request,
    response: Response,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
    x_forge_token: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> Receipt:
    """
    Receive an event for an organization: its JSON body is kept and checked
    against the event type's schema. 202 when it matches; 422, with where it
    doesn't, when it doesn't (it's kept either way, marked invalid).
    \f
    :param endpoint_id: The organization's endpoint.
    :param event_key: The event type's key, e.g. ``incident.opened``.
    :param request: The request, for its body and sender.
    :param response: Its status, 200 for a repeat.
    :param session: The request's database session.
    :param authorization: ``Bearer <token>``.
    :param x_forge_token: The token, for senders that can't set
        Authorization.
    :param idempotency_key: The sender's ID for the event, to make retries
        safe.
    :return: The event kept.
    """
    token = _token_of(authorization, x_forge_token)
    endpoint = await _endpoint(session, endpoint_id, token)
    set_actor(f"endpoint:{endpoint.id}")
    if not endpoint.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The endpoint is turned off")
    body = await _body(request, request.app.state.settings.events_max_bytes)

    event_type = await session.scalar(
        select(EventType).where(
            EventType.organization_id == endpoint.organization_id,
            EventType.key == event_key,
        )
    )
    if event_type is None:
        known = re.fullmatch(EVENT_KEY_PATTERN, event_key) is not None
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"The organization has no event type {event_key}"
            if known
            else "Event type keys are lowercase letters, digits and . _ -",
        )
    if event_type.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Event type {event_key} is a draft; the organization turns it on once "
            "it's reviewed"
            if event_type.status == "draft"
            else f"Event type {event_key} is paused",
        )
    payload = _parse(body)

    key = idempotency_key.strip() if idempotency_key else None
    if key:
        kept = await _kept(session, event_type.id, key)
        if kept is not None:
            response.status_code = status.HTTP_200_OK
            return _receipt(kept, event_key, duplicate=True)

    issues = await asyncio.to_thread(
        validate_payload, event_type.payload_schema, payload
    )
    event = Event(
        organization_id=endpoint.organization_id,
        event_type_id=event_type.id,
        schema_version=event_type.schema_version,
        status="invalid" if issues else "valid",
        errors=[issue.as_dict() for issue in issues],
        payload=payload,
        idempotency_key=key or None,
        size_bytes=len(body),
        content_type=request.headers.get("content-type", "")[:255],
        user_agent=request.headers.get("user-agent", "")[:500],
        source_ip=(request.client.host if request.client else "")[:64],
    )
    session.add(event)
    try:
        await session.commit()
    except IntegrityError:
        # The same key, sent twice at once: the other request kept it.
        await session.rollback()
        kept = await _kept(session, event_type.id, key) if key else None
        if kept is None:
            raise
        response.status_code = status.HTTP_200_OK
        return _receipt(kept, event_key, duplicate=True)
    if issues:
        response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _receipt(event, event_key)
