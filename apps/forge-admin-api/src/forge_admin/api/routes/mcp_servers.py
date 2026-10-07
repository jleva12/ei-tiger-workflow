"""
An organization's MCP servers: the remote (streamable HTTP) MCP servers its
agents use as toolsets, each with how Forge authenticates to it.

Reading needs ``organizations:read`` in the organization; adding, changing,
checking, connecting and deleting need ``mcp_servers:manage`` (its admins and
members have it by default). Auth methods are pluggable
(:mod:`forge_admin.mcp_servers.auth`); ``GET /mcp-auth-methods`` lists them
with their forms' fields. A method's secrets are write-only: a server answers
which are set, never what they are.

``GET /mcp-server-defaults`` lists the servers the application offers
ready-made (``forge_admin.mcp_servers.defaults``): the web console fills in
the form with one, and the person adds what's theirs.

An OAuth sign-in starts at ``POST …/oauth/start``, which answers where to
send the person; the authorization server sends them back to the web
console's ``/oauth/mcp/callback``, which hands what came back to
``POST /mcp-oauth/callback`` as the same person.
"""

import logging
import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from forge_mcp_servers.auth import (
    HEADER_NAME_PATTERN,
    AuthError,
    AuthMethodInfo,
    NotConnected,
)
from forge_mcp_servers.secrets import SecretsError
from forge_mcp_servers.service import UNCHECKED, McpServers, epoch
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Name,
    NodeId,
    Session,
    commit_or_conflict,
    name_of,
)
from forge_admin.auth.access import (
    UUID_PATTERN,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.db.audit import UtcDateTime, utc_now
from forge_admin.mcp_servers.defaults import DefaultServer
from forge_admin.models import McpOAuthFlow, McpServer

logger = logging.getLogger(__name__)

router = APIRouter(tags=["organization MCP servers"])

MANAGE = "mcp_servers:manage"
NOT_FOUND = "The organization has no such MCP server"
NAME_TAKEN = "The organization already has an MCP server by that name"
SIGN_IN_GONE = "This sign-in expired or was already used; connect the server again"
# How long a person has to sign in.
FLOW_LIFETIME = timedelta(minutes=10)

ServerId = Annotated[str, Path(pattern=rf"^{UUID_PATTERN}$")]


class HeaderRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=HEADER_NAME_PATTERN)
    value: str = Field(max_length=8192)


class AuthInput(BaseModel):
    """How Forge authenticates to the server."""

    model_config = ConfigDict(extra="forbid")

    #: An auth method's kind (``GET /mcp-auth-methods``).
    kind: str = Field(default="none", max_length=64)
    #: The method's settings; the defaults for those left out.
    settings: dict[str, Any] = {}
    #: Secrets to change: a value replaces the saved one, null or "" clears
    #: it. One left out keeps what's saved, unless the kind changes.
    secrets: dict[str, str | None] = {}


Url = Annotated[str, Field(pattern=r"^https?://[^\s/?#]+[^\s]*$", max_length=2048)]
Timeout = Annotated[float, Field(gt=0, le=300)]


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    description: str = Field(default="", max_length=2000)
    #: Its MCP endpoint, e.g. https://mcp.example.com/mcp.
    url: Url
    transport: Literal["streamable_http"] = "streamable_http"
    #: Sent as they are with every request: not secret.
    headers: list[HeaderRow] = Field(default=[], max_length=50)
    #: Seconds it has to answer.
    timeout_seconds: Timeout = 30.0
    auth: AuthInput = AuthInput()


class McpServerUpdate(BaseModel):
    """What changes; what's left out stays."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: str | None = Field(default=None, max_length=2000)
    url: Url | None = None
    headers: list[HeaderRow] | None = Field(default=None, max_length=50)
    timeout_seconds: Timeout | None = None
    auth: AuthInput | None = None


class AuthRead(BaseModel):
    kind: str
    #: The method's settings, as saved.
    settings: dict[str, Any]
    #: Which of its secrets are saved (never what they are).
    secrets_set: list[str]
    #: A person connects it (signs in).
    interactive: bool
    #: For a method a person connects: whether someone has.
    connected: bool | None = None
    connected_by: str | None = None
    #: Who connected it, by name.
    connected_by_name: str | None = None
    connected_at: UtcDateTime | None = None
    #: When the connection runs out, when it can't renew itself.
    expires_at: UtcDateTime | None = None
    scope: str | None = None


class ToolRead(BaseModel):
    name: str
    title: str | None = None
    description: str = ""


class McpServerRead(BaseModel):
    id: str
    organization_id: str
    name: str
    description: str
    url: str
    transport: str
    headers: list[HeaderRow]
    timeout_seconds: float
    auth: AuthRead
    #: What the last check found: unchecked, ok, error or needs_auth.
    status: Literal["unchecked", "ok", "error", "needs_auth"]
    #: Its tools, when last checked.
    tools: list[ToolRead]
    server_info: dict[str, Any] | None
    checked_at: UtcDateTime | None
    last_error: str | None
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str


class OAuthStart(BaseModel):
    #: Where to send the person to sign in.
    authorization_url: str


class OAuthCallback(BaseModel):
    """What the authorization server sent the person back with."""

    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=4096)
    error: str | None = Field(default=None, max_length=256)
    error_description: str | None = Field(default=None, max_length=2048)


def service(request: Request) -> McpServers:
    """
    :raises HTTPException: 503 when MCP servers aren't set up.
    """
    servers: McpServers | None = request.app.state.mcp_servers
    if servers is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "MCP servers aren't set up: set FORGE_ADMIN_SECRETS_KEY",
        )
    return servers


async def _read(
    session: AsyncSession, servers: McpServers, server: McpServer
) -> McpServerRead:
    method = servers.method(server)
    try:
        saved = sorted(servers.secrets(server))
    except SecretsError:
        saved = []
    connection = servers.connection(server)
    auth = AuthRead(
        kind=server.auth_kind,
        settings=server.auth_settings,
        secrets_set=saved,
        interactive=method.interactive,
    )
    if connection is not None:
        auth.connected = connection.connected
        auth.connected_by = connection.connected_by
        auth.connected_by_name = (
            await name_of(session, connection.connected_by)
            if connection.connected_by
            else None
        )
        auth.connected_at = epoch(connection.connected_at)
        auth.expires_at = epoch(connection.expires_at)
        auth.scope = connection.scope
    return McpServerRead.model_validate(
        {
            **{
                name: getattr(server, name)
                for name in McpServerRead.model_fields
                if name != "auth"
            },
            "auth": auth,
        }
    )


async def _server(
    session: AsyncSession, organization_id: str, server_id: str
) -> McpServer:
    server = await session.get(McpServer, server_id)
    if server is None or server.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return server


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _apply_auth(
    servers: McpServers, server: McpServer, auth: AuthInput, *, new: bool
) -> bool:
    """
    Set the server's auth method, settings and secrets.

    :return: Whether it authenticates differently now: its grant no longer applies.
    :raises HTTPException: 422 for a method there isn't, or settings or
        secrets it doesn't take.
    """
    try:
        method = servers.methods.get(auth.kind)
        settings = method.settings(auth.settings).model_dump()
    except ValueError as error:
        raise _unprocessable(str(error)) from None
    same = not new and server.auth_kind == auth.kind
    try:
        saved = servers.secrets(server) if same else {}
    except SecretsError:
        saved = {}
    names = method.secret_names()
    unknown = sorted(set(auth.secrets) - set(names))
    if unknown:
        raise _unprocessable(f"{method.label} has no secret {', '.join(unknown)}")
    merged = {name: value for name, value in saved.items() if name in names}
    for name, value in auth.secrets.items():
        if value:
            merged[name] = value
        else:
            merged.pop(name, None)
    try:
        method.secrets(merged)
    except ValueError as error:
        raise _unprocessable(str(error)) from None
    changed = not same or settings != server.auth_settings or merged != saved
    server.auth_kind, server.auth_settings = auth.kind, settings
    server.auth_secrets = servers.box.seal(merged)
    return changed


def _forget_connection(servers: McpServers, server: McpServer) -> None:
    """What was connected or checked no longer applies."""
    servers.set_grant(server, None)
    server.status, server.last_error, server.checked_at = UNCHECKED, None, None


@router.get("/mcp-auth-methods")
async def list_auth_methods(request: Request) -> list[AuthMethodInfo]:
    """
    The ways Forge can authenticate to an MCP server, each with its form's
    fields.
    \f
    :param request: The request.
    :return: The auth methods.
    """
    return [method.describe() for method in service(request).methods]


@router.get("/mcp-server-defaults")
async def list_mcp_server_defaults(
    request: Request, user: CurrentUser
) -> list[DefaultServer]:
    """
    The MCP servers the application offers ready-made: what connecting to
    each takes (its URL, timeout, auth method with its settings and help for
    its fields, and the headers it needs), to fill in a new server's form.
    For signed-in people: the URLs can be addresses inside the deployment.
    \f
    :param request: The request.
    :param user: Who's asking.
    :return: The default servers, as the file lists them.
    """
    return list(request.app.state.mcp_server_defaults)


@router.get("/organizations/{organization_id}/mcp-servers")
async def list_mcp_servers(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[McpServerRead]:
    """
    List an organization's MCP servers, by name.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when MCP servers aren't set up.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    servers = service(request)
    found = await session.scalars(
        select(McpServer)
        .where(McpServer.organization_id == organization_id)
        .order_by(McpServer.name)
    )
    return [await _read(session, servers, server) for server in found]


@router.post(
    "/organizations/{organization_id}/mcp-servers", status_code=status.HTTP_201_CREATED
)
async def create_mcp_server(
    organization_id: NodeId,
    body: McpServerCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    Add an MCP server to the organization. It's unchecked until checked.
    \f
    :raises HTTPException: 403 without mcp_servers:manage in the organization;
        409 when the name is taken; 422 for an auth method there isn't, or
        settings or secrets it doesn't take; 503 when MCP servers aren't set up.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    servers = service(request)
    server = McpServer(
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        url=body.url,
        transport=body.transport,
        headers=[row.model_dump() for row in body.headers],
        timeout_seconds=body.timeout_seconds,
        auth_settings={},
        tools=[],
        status=UNCHECKED,
    )
    _apply_auth(servers, server, body.auth, new=True)
    session.add(server)
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, servers, server)


@router.get("/organizations/{organization_id}/mcp-servers/{server_id}")
async def get_mcp_server(
    organization_id: NodeId,
    server_id: ServerId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    One of the organization's MCP servers.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        404 when it has no such server.
    """
    await authorize(
        session, enforcer, user, "organizations:read", Scope(Level.ORG, organization_id)
    )
    servers = service(request)
    return await _read(
        session, servers, await _server(session, organization_id, server_id)
    )


@router.patch("/organizations/{organization_id}/mcp-servers/{server_id}")
async def update_mcp_server(
    organization_id: NodeId,
    server_id: ServerId,
    body: McpServerUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    Change an MCP server. A new URL, or a different way of authenticating,
    disconnects it (its tokens were for what it was) and leaves it unchecked.
    \f
    :raises HTTPException: 403 without mcp_servers:manage; 404 when the
        organization has no such server; 409 when the name is taken; 422 for
        an auth method there isn't, or settings or secrets it doesn't take.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    servers = service(request)
    server = await _server(session, organization_id, server_id)
    reconnect = False
    if body.name is not None:
        server.name = body.name
    if body.description is not None:
        server.description = body.description
    if body.url is not None and body.url != server.url:
        server.url, reconnect = body.url, True
    if body.headers is not None:
        server.headers = [row.model_dump() for row in body.headers]
    if body.timeout_seconds is not None:
        server.timeout_seconds = body.timeout_seconds
    if body.auth is not None:
        reconnect = _apply_auth(servers, server, body.auth, new=False) or reconnect
    if reconnect:
        _forget_connection(servers, server)
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, servers, server)


@router.delete(
    "/organizations/{organization_id}/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_mcp_server(
    organization_id: NodeId,
    server_id: ServerId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Delete an MCP server, with its credentials. Agents that use it lose its tools.
    \f
    :raises HTTPException: 403 without mcp_servers:manage; 404 when the
        organization has no such server.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    server = await _server(session, organization_id, server_id)
    await session.execute(
        delete(McpOAuthFlow).where(McpOAuthFlow.server_id == server.id)
    )
    await session.delete(server)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/organizations/{organization_id}/mcp-servers/{server_id}/check")
async def check_mcp_server(
    organization_id: NodeId,
    server_id: ServerId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    Connect to the server as its agents would, and list its tools. What it
    found (``ok`` with its tools, ``needs_auth``, or ``error`` and why) is
    kept and answered; a server that can't be reached is still a 200.
    \f
    :raises HTTPException: 403 without mcp_servers:manage; 404 when the
        organization has no such server.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    servers = service(request)
    server = await _server(session, organization_id, server_id)
    await servers.check(server)
    await session.commit()
    return await _read(session, servers, server)


@router.post("/organizations/{organization_id}/mcp-servers/{server_id}/oauth/start")
async def start_mcp_oauth(
    organization_id: NodeId,
    server_id: ServerId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> OAuthStart:
    """
    Begin signing in to the server's authorization server: answers where to
    send the person, who comes back to the web console within ten minutes.
    \f
    :raises HTTPException: 403 without mcp_servers:manage; 404 when the
        organization has no such server; 422 when its auth method isn't one
        a person signs in with; 502 when its authorization server can't be
        found or used.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    servers = service(request)
    server = await _server(session, organization_id, server_id)
    settings = request.app.state.settings
    redirect_uri = f"{settings.web_url.rstrip('/')}/oauth/mcp/callback"
    state = secrets.token_urlsafe(32)
    try:
        authorization = await servers.begin(
            server, redirect_uri=redirect_uri, state=state
        )
    except ValueError as error:
        raise _unprocessable(str(error)) from None
    except (AuthError, SecretsError) as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from None
    now = utc_now()
    # Sign-ins nobody finished.
    await session.execute(delete(McpOAuthFlow).where(McpOAuthFlow.expires_at < now))
    session.add(
        McpOAuthFlow(
            state=state,
            server_id=server.id,
            user_id=user,
            redirect_uri=redirect_uri,
            pending=servers.box.seal(authorization.pending) or "",
            expires_at=now + FLOW_LIFETIME,
        )
    )
    if authorization.pending.get("client", {}).get("registered"):
        # The client it registered, kept for the next sign-in.
        grant = servers.grant(server)
        servers.set_grant(server, grant | {"client": authorization.pending["client"]})
    await session.commit()
    return OAuthStart(authorization_url=authorization.url)


@router.delete(
    "/organizations/{organization_id}/mcp-servers/{server_id}/oauth",
)
async def disconnect_mcp_oauth(
    organization_id: NodeId,
    server_id: ServerId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    Forget the server's sign-in: its tokens are dropped, and it's unchecked
    until someone connects it again.
    \f
    :raises HTTPException: 403 without mcp_servers:manage; 404 when the
        organization has no such server.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    servers = service(request)
    server = await _server(session, organization_id, server_id)
    _forget_connection(servers, server)
    await session.commit()
    return await _read(session, servers, server)


@router.post("/mcp-oauth/callback")
async def finish_mcp_oauth(
    body: OAuthCallback,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> McpServerRead:
    """
    Finish a sign-in with what the person came back with: only they may,
    once, within ten minutes of starting it. The server is checked
    straight after, and answered.
    \f
    :raises HTTPException: 400 when the sign-in is unknown, someone else's,
        expired, or the authorization server refused it; 403 without
        mcp_servers:manage in the server's organization; 502 when the code
        wasn't exchanged.
    """
    servers = service(request)
    flow = await session.get(McpOAuthFlow, body.state)
    if flow is None or flow.user_id != user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    await session.delete(flow)
    await session.commit()
    if flow.expires_at < utc_now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    server = await session.get(McpServer, flow.server_id)
    if server is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    await authorize(
        session, enforcer, user, MANAGE, Scope(Level.ORG, server.organization_id)
    )
    if body.error or not body.code:
        reason = body.error_description or body.error or "no code came back"
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"The sign-in didn't finish: {reason}"
        )
    try:
        pending = servers.box.open(flow.pending)
        await servers.complete(
            server,
            pending=pending,
            code=body.code,
            redirect_uri=flow.redirect_uri,
            user=user,
        )
    except (AuthError, SecretsError) as error:
        status_code = (
            status.HTTP_400_BAD_REQUEST
            if isinstance(error, NotConnected)
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code, str(error)) from None
    await session.commit()
    await servers.check(server)
    await session.commit()
    return await _read(session, servers, server)
