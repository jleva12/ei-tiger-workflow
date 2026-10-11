"""Organizations' API credentials, and the OAuth sign-ins in progress for them."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, JSONText, ascii_string


def new_id() -> str:
    """:return: A random UUID, the ID of a new credential."""
    return str(uuid4())


class ApiCredential(AuditBase):
    """
    How an organization's workflows and agents sign in to one of its HTTP
    APIs: an auth method (an API key, a token, basic auth, OAuth), and the
    URLs it may be sent to. Its HTTP steps and HTTP and OpenAPI tools name it
    by ID; a request anywhere else goes without it.

    The auth method's settings are kept as they are; its secrets and its
    grant (the tokens it keeps) are Fernet ciphertext
    (``forge_mcp_servers.secrets``), which the API never answers.
    """

    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    #: Where it may be sent: URLs, each its origin and optionally a path
    #: (``forge_mcp_servers.api_credentials.sends_to``).
    urls: Mapped[list[str]] = mapped_column(JSONText, default=list)
    #: The auth method (forge_mcp_servers.api_credentials.CREDENTIAL_METHODS).
    auth_kind: Mapped[str] = mapped_column(ascii_string(64))
    auth_settings: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
    #: Ciphertext, or None for none.
    auth_secrets: Mapped[str | None] = mapped_column(Text, default=None)
    #: Ciphertext, or None for none.
    auth_grant: Mapped[str | None] = mapped_column(Text, default=None)


class ApiCredentialOAuthFlow(AuditBase):
    """
    An OAuth sign-in someone began with a credential and hasn't finished: its
    ``state`` comes back with them, and only they may finish it, before it
    expires.
    """

    __tablename__ = "api_credential_oauth_flows"

    state: Mapped[str] = mapped_column(ascii_string(64), primary_key=True)
    credential_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("api_credentials.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(255))
    redirect_uri: Mapped[str] = mapped_column(Text)
    #: Ciphertext: the PKCE verifier and what else the method keeps meanwhile.
    pending: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(AUDIT_TIMESTAMP)
