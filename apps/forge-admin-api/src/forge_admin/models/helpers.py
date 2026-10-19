"""Background helper agents' jobs (``forge_admin.helpers``)."""

from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, JSONText, ascii_string


def new_id() -> str:
    """:return: A random UUID, a new job's ID."""
    return str(uuid4())


class HelperJob(AuditBase):
    """
    What a background helper (``run: background`` in ``helpers.yaml``) was
    asked to do for an organization, and once it's done, what it made (or
    why it couldn't). Its ``created_by`` is who asked.
    """

    __tablename__ = "helper_jobs"

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    #: The helper's name, as ``helpers.yaml`` has it.
    helper: Mapped[str] = mapped_column(ascii_string(64))
    #: What it was asked: its kit's input.
    input: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
    #: ``running``, ``done`` or ``failed``.
    status: Mapped[str] = mapped_column(ascii_string(16), default="running")
    #: What it made, once done (its kit's result).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONText, default=None)
    #: Notes for the person: what to check, what it assumed.
    notes: Mapped[list[str]] = mapped_column(JSONText, default=list)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    #: The model it ran on (``provider/model``).
    model: Mapped[str | None] = mapped_column(String(200), default=None)
