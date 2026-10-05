"""Organizations' MCP servers, and the OAuth sign-ins in progress for them."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, JSONText, ascii_string


def new_id() -> str:
    """:return: A random UUID, the ID of a new MCP server."""
    return str(uuid4())


class McpServer(AuditBase):
    """
    A remote MCP server an organization's agents may use as a toolset: where
    it is, how Forge authenticates to it, and what it had when last checked.

    The auth method's settings are kept as they are; its secrets and its
    grant (the tokens it keeps) are Fernet ciphertext
    (:mod:`forge_admin.mcp_servers.secrets`), which the API never answers.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    #: The MCP endpoint.
    url: Mapped[str] = mapped_column(Text)
    #: Only streamable_http for now.
    transport: Mapped[str] = mapped_column(ascii_string(32), default="streamable_http")
    #: Headers sent as they are: ``[{"name": …, "value": …}]``. Not secret.
    headers: Mapped[list[dict[str, str]]] = mapped_column(JSONText, default=list)
    #: Seconds to wait for it to answer.
    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0)
    #: The auth method (forge_admin.mcp_servers.registry).
    auth_kind: Mapped[str] = mapped_column(ascii_string(64), default="none")
    auth_settings: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
    #: Ciphertext, or None for none.
    auth_secrets: Mapped[str | None] = mapped_column(Text, default=None)
    #: Ciphertext, or None for none.
    auth_grant: Mapped[str | None] = mapped_column(Text, default=None)
    #: unchecked, ok, error or needs_auth: what the last check found.
    status: Mapped[str] = mapped_column(ascii_string(16), default="unchecked")
    #: The tools it listed when last checked: ``[{"name", "title", "description"}]``.
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONText, default=list)
    #: What it said it is when last checked: ``{"name", "version"}``.
    server_info: Mapped[dict[str, Any] | None] = mapped_column(JSONText, default=None)
    checked_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class McpOAuthFlow(AuditBase):
    """
    An OAuth sign-in someone began for an MCP server and hasn't finished: its
    ``state`` comes back with them, and only they may finish it, before it
    expires.
    """

    __tablename__ = "mcp_oauth_flows"

    state: Mapped[str] = mapped_column(ascii_string(64), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(255))
    redirect_uri: Mapped[str] = mapped_column(Text)
    #: Ciphertext: the PKCE verifier, the client, the token endpoint.
    pending: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(AUDIT_TIMESTAMP)
