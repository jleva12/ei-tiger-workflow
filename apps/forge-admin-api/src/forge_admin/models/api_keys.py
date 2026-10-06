"""Organizations' API keys: how outside apps call the organization's API."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, ascii_string


def new_id() -> str:
    """:return: A random UUID, the ID of a new API key."""
    return str(uuid4())


class ApiKey(AuditBase):
    """
    An organization's API key: a secret an outside app sends instead of a
    person's sign-in. Only the secret's SHA-256 is kept, and its last four
    characters to tell keys apart (forge_admin.auth.api_keys).

    What a key may do is the role it holds in its organization, a Casbin
    ``g`` line for the subject ``apikey:<id>`` like a member's, so it's
    authorized as members are. Deleting the key deletes that line too.
    """

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    #: The secret's SHA-256, hex: what a request's key is looked up by.
    secret_sha256: Mapped[str] = mapped_column(ascii_string(64), unique=True)
    #: The secret's last four characters.
    hint: Mapped[str] = mapped_column(ascii_string(8))
    #: When it stops working; None for never.
    expires_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
    #: When it was last used, to within a few minutes.
    last_used_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
