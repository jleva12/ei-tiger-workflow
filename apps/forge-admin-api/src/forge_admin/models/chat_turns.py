"""What makes chat agents' turns and the workflows they start happen once:
each conversation's turn lease, and the workflow starts waiting to be made."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, JSONText, ascii_string


class ChatTurnLease(AuditBase):
    """
    A conversation's turn lease (``forge_agent_runtime.leases``): the turn
    running in it, held by ``owner`` until ``expires_at`` (renewed while it
    runs). Every process of the hosted runtime shares them, so a
    conversation's turns are one at a time across them all.
    """

    __tablename__ = "chat_turn_leases"

    #: The conversation's key: SHA-256 of its agent, person and session.
    id: Mapped[str] = mapped_column(ascii_string(64), primary_key=True)
    #: The chat agent (its ID, whichever version runs).
    app_name: Mapped[str] = mapped_column(ascii_string(64))
    user_id: Mapped[str] = mapped_column(String(255))
    session_id: Mapped[str] = mapped_column(String(128))
    #: The turn holding it.
    owner: Mapped[str] = mapped_column(ascii_string(32))
    expires_at: Mapped[datetime] = mapped_column(AUDIT_TIMESTAMP)


class WorkflowStartIntent(AuditBase):
    """
    A workflow run a chat agent's intake starts when it completes, written
    before the change that completes it (an intake's record in the
    conversation's state, or a request's ``open`` → ``submitting``) is kept,
    and started once that change is: the start's outbox. Its ID is the
    run's, made from the record or request and the workflow it starts, so
    starting it again finds the run rather than making another.

    ``status``: ``intended`` until the run is made; ``dispatched`` once it
    is; ``dropped`` when the change it waited for was never kept.
    """

    __tablename__ = "workflow_start_intents"
    __table_args__ = (
        # What the upkeep looks for: intents still waiting, oldest first.
        Index("ix_workflow_start_intents_status", "status", "created_at"),
        Index("ix_workflow_start_intents_source", "source", "source_id"),
    )

    #: The run it makes (``forge_admin.chat_agents.source.chat_run_id``).
    id: Mapped[str] = mapped_column(ascii_string(32), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    #: The chat agent, the person and the conversation it's for.
    agent_id: Mapped[str] = mapped_column(ascii_string(64))
    user_id: Mapped[str] = mapped_column(String(255))
    session_id: Mapped[str] = mapped_column(String(128))
    #: What a chat follows the run by, in place of a model's call ID.
    call_id: Mapped[str] = mapped_column(String(128))
    #: The workflow at the version it runs (``ag_x``, ``ag_x@3``, ``ag_x@draft``), and its name.
    workflow: Mapped[str] = mapped_column(ascii_string(80))
    workflow_name: Mapped[str] = mapped_column(String(255), default="")
    input: Mapped[Any] = mapped_column(JSONText)
    #: Who and what started it, as they were when it was written: what the
    #: run's start needs of the turn (its caller, channel, agent version).
    origin: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
    #: What completing waits to be kept: an intake's record (``record``, in
    #: the conversation's state under ``state_key``) or a request (``request``).
    source: Mapped[str] = mapped_column(ascii_string(16))
    source_id: Mapped[str] = mapped_column(ascii_string(64))
    state_key: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(ascii_string(16), default="intended")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: A process starting it has it until then.
    claimed_until: Mapped[datetime | None] = mapped_column(
        AUDIT_TIMESTAMP, default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    dispatched_at: Mapped[datetime | None] = mapped_column(
        AUDIT_TIMESTAMP, default=None
    )
