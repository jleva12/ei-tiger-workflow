"""The requests chat agents file to their intakes, for people."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, JSONText, ascii_string


class IntakeRequest(AuditBase):
    """
    One request a chat agent filed to one of its intakes (an intake agent on
    its Tools way) for a person: ``forge_agent_runtime.requests.IntakeRequest``,
    kept so it outlives the conversation it was started in.

    ``record`` is the intake's record whole (``forge_agent_runtime.intake.IntakeRecord``).
    """

    __tablename__ = "intake_requests"
    __table_args__ = (
        Index(
            "ix_intake_requests_owner",
            "organization_id",
            "agent_id",
            "user_id",
            "status",
            "updated_at",
        ),
    )

    #: Given by the runtime when it starts the request.
    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    #: The chat agent (its ID, whichever version filed it).
    agent_id: Mapped[str] = mapped_column(ascii_string(64))
    #: Whose request it is: the person the conversation is for.
    user_id: Mapped[str] = mapped_column(String(255))
    #: The conversation it was last worked on in.
    session_id: Mapped[str] = mapped_column(String(128))
    #: The intake's node's ID on the agent's canvas.
    intake: Mapped[str] = mapped_column(ascii_string(64))
    #: The intake's node's name, when it was last saved.
    title: Mapped[str] = mapped_column(String(255), default="")
    #: The requests started together share it.
    basket: Mapped[str] = mapped_column(ascii_string(36))
    #: ``open``, ``submitting`` (its workflows starting), ``submitted`` or ``cancelled``.
    status: Mapped[str] = mapped_column(ascii_string(16), default="open")
    #: Counts its changes: each is made only to the version it was read at
    #: (compare-and-set), so two changes made at once can't undo each other.
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    record: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
