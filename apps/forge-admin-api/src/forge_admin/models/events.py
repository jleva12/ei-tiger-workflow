"""Events other systems send an organization: its inbound endpoint, the event
types it accepts with the JSON Schema each must match, and every event
received."""

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, JSONText, ascii_string
from forge_admin.models.hierarchy import new_id


class EventEndpoint(AuditBase):
    """
    An organization's inbound endpoint: where other systems send it events,
    at ``/hooks/events/<id>/<event key>``, with its token. An organization
    has at most one; turning it off refuses every event without forgetting
    the token.

    :ivar organization_id: The organization.
    :ivar enabled: Whether it accepts events.
    :ivar token_sha256: The SHA-256 of its token, hex; the token itself is
        shown once, when it's made.
    :ivar token_hint: The token's last characters, to tell tokens apart.
    """

    __tablename__ = "organization_event_endpoints"

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    token_sha256: Mapped[str] = mapped_column(ascii_string(64))
    token_hint: Mapped[str] = mapped_column(ascii_string(8))


class EventType(AuditBase):
    """
    A kind of event an organization accepts, such as ``invoice.received``,
    and the JSON Schema its payload must match. It starts as a draft, which
    the endpoint refuses, and accepts events once the organization turns it
    on after reviewing it. Changing the schema bumps ``schema_version``; each event
    records the version it was checked against.

    :ivar organization_id: The organization.
    :ivar key: What senders name it by in the URL, unique in the organization.
    :ivar name: What people call it.
    :ivar description: What it means, optionally.
    :ivar status: ``draft`` until it's first turned on, then ``active``
        (accepted) or ``paused``; only ``active`` ones are accepted.
    :ivar payload_schema: The JSON Schema (draft 2020-12) of its payload.
    :ivar schema_version: 1, then one more each time the schema changes.
    """

    __tablename__ = "organization_event_types"
    # Also the index the organization's foreign key needs.
    __table_args__ = (UniqueConstraint("organization_id", "key"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(ascii_string(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(ascii_string(16), default="draft")
    payload_schema: Mapped[dict[str, Any]] = mapped_column(JSONText)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class Event(AuditBase):
    """
    One event received at an organization's endpoint, valid or not: its
    payload as sent, and how it measured up to its type's schema. Only valid
    events will set off what the organization builds on them. ``created_at`` is when it
    arrived, and ``created_by`` the endpoint (``endpoint:<id>``).

    :ivar organization_id: The organization.
    :ivar event_type_id: Its type; deleting the type deletes its events.
    :ivar schema_version: The version of the type's schema it was checked
        against.
    :ivar status: ``valid`` or ``invalid``.
    :ivar errors: Where and why it didn't match the schema, as
        ``{"path", "message", "keyword"}``; empty when valid.
    :ivar payload: The JSON body, parsed, with its keys in the order sent.
    :ivar idempotency_key: The sender's ID for it (``Idempotency-Key``), if
        it sent one; a second event with the same key and type is the same
        event.
    :ivar size_bytes: The body's size.
    :ivar content_type: Its ``Content-Type``.
    :ivar user_agent: The sender's ``User-Agent``.
    :ivar source_ip: The address it came from.
    """

    __tablename__ = "organization_events"
    __table_args__ = (
        UniqueConstraint("event_type_id", "idempotency_key"),
        # Lists are the latest first, of the organization or of one type.
        Index(
            "ix_organization_events_organization_id_created_at",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_organization_events_event_type_id_created_at",
            "event_type_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    event_type_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organization_event_types.id", ondelete="CASCADE")
    )
    schema_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(ascii_string(16))
    errors: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    payload: Mapped[Any] = mapped_column(JSONText)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(255), default="")
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    source_ip: Mapped[str] = mapped_column(ascii_string(64), default="")
