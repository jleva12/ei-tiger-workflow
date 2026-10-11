"""
An organization's API credentials: how its workflows' HTTP steps and its
agents' HTTP and OpenAPI tools sign in to its HTTP APIs, each sent only to
the URLs it names.

Reading needs ``organizations:read`` in the organization; adding, changing,
signing in with and deleting need ``credentials:manage`` (its admins and
members have it by default). Auth methods are pluggable
(``forge_mcp_servers.api_credentials.CREDENTIAL_METHODS``);
``GET /credential-auth-methods`` lists them with their forms' fields. A
method's secrets are write-only: a credential answers which are set, never
what they are.

An OAuth sign-in starts at ``POST …/oauth/start``, which answers where to send
the person; the authorization server sends them back to the web console's
``/oauth/credentials/callback``, which hands what came back to
``POST /credential-oauth/callback`` as the same person.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from forge_mcp_servers import ApiCredentials
from forge_mcp_servers.auth import AuthError, AuthMethodInfo, NotConnected
from forge_mcp_servers.secrets import SecretsError
from forge_mcp_servers.service import epoch
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
from forge_admin.api.routes.mcp_servers import (
    FLOW_LIFETIME,
    SIGN_IN_GONE,
    AuthInput,
    AuthRead,
    OAuthCallback,
    OAuthStart,
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
from forge_admin.models import ApiCredential, ApiCredentialOAuthFlow

router = APIRouter(tags=["organization API credentials"])

MANAGE = "credentials:manage"
READ = "organizations:read"
NOT_FOUND = "The organization has no such credential"
NAME_TAKEN = "The organization already has a credential by that name"

CredentialId = Annotated[str, Path(pattern=rf"^{UUID_PATTERN}$")]
#: Where a credential may be sent: an http(s) origin, and optionally a path;
#: no query or fragment.
CredentialUrl = Annotated[
    str, Field(pattern=r"^https?://[^\s/?#]+(/[^\s?#]*)?$", max_length=2048)
]


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    description: str = Field(default="", max_length=2000)
    #: Where it may be sent, e.g. https://api.github.com: requests elsewhere go
    #: without it.
    urls: list[CredentialUrl] = Field(min_length=1, max_length=20)
    auth: AuthInput


class CredentialUpdate(BaseModel):
    """What changes; what's left out stays."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: str | None = Field(default=None, max_length=2000)
    urls: list[CredentialUrl] | None = Field(default=None, min_length=1, max_length=20)
    auth: AuthInput | None = None


class CredentialRead(BaseModel):
    id: str
    organization_id: str
    name: str
    description: str
    urls: list[str]
    auth: AuthRead
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str


def service(request: Request) -> ApiCredentials:
    """
    :raises HTTPException: 503 when credentials aren't set up.
    """
    found: ApiCredentials | None = getattr(request.app.state, "api_credentials", None)
    if found is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Credentials aren't set up: set FORGE_ADMIN_SECRETS_KEY",
        )
    return found


async def _read(
    session: AsyncSession, credentials: ApiCredentials, credential: ApiCredential
) -> CredentialRead:
    try:
        interactive = credentials.method(credential).interactive
    except ValueError:
        interactive = False
    try:
        saved = sorted(credentials.secrets(credential))
    except SecretsError:
        saved = []
    auth = AuthRead(
        kind=credential.auth_kind,
        settings=credential.auth_settings,
        secrets_set=saved,
        interactive=interactive,
    )
    connection = credentials.connection(credential)
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
    return CredentialRead.model_validate(
        {
            **{
                name: getattr(credential, name)
                for name in CredentialRead.model_fields
                if name != "auth"
            },
            "auth": auth,
        }
    )


async def _credential(
    session: AsyncSession, organization_id: str, credential_id: str
) -> ApiCredential:
    credential = await session.get(ApiCredential, credential_id)
    if credential is None or credential.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return credential


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _apply_auth(
    credentials: ApiCredentials,
    credential: ApiCredential,
    auth: AuthInput,
    *,
    new: bool,
) -> bool:
    """
    Set the credential's auth method, settings and secrets.

    :return: Whether it signs in differently now: its grant no longer applies.
    :raises HTTPException: 422 for a method there isn't, or settings or
        secrets it doesn't take.
    """
    try:
        method = credentials.methods.get(auth.kind)
        settings = method.settings(auth.settings).model_dump()
    except ValueError as error:
        raise _unprocessable(str(error)) from None
    same = not new and credential.auth_kind == auth.kind
    try:
        saved = credentials.secrets(credential) if same else {}
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
    changed = not same or settings != credential.auth_settings or merged != saved
    credential.auth_kind, credential.auth_settings = auth.kind, settings
    credential.auth_secrets = credentials.box.seal(merged)
    return changed


@router.get("/credential-auth-methods")
async def list_credential_auth_methods(request: Request) -> list[AuthMethodInfo]:
    """
    The ways a credential can sign in to an API, each with its form's fields.
    \f
    :param request: The request.
    :return: The auth methods.
    """
    return [method.describe() for method in service(request).methods]


@router.get("/organizations/{organization_id}/credentials")
async def list_credentials(
    organization_id: NodeId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[CredentialRead]:
    """
    List an organization's API credentials, by name.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        503 when credentials aren't set up.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    credentials = service(request)
    found = await session.scalars(
        select(ApiCredential)
        .where(ApiCredential.organization_id == organization_id)
        .order_by(ApiCredential.name)
    )
    return [await _read(session, credentials, credential) for credential in found]


@router.post(
    "/organizations/{organization_id}/credentials", status_code=status.HTTP_201_CREATED
)
async def create_credential(
    organization_id: NodeId,
    body: CredentialCreate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CredentialRead:
    """
    Add an API credential to the organization. One that needs signing in
    (OAuth sign-in) is unconnected until someone does.
    \f
    :raises HTTPException: 403 without credentials:manage in the organization;
        409 when the name is taken; 422 for an auth method there isn't, or
        settings or secrets it doesn't take; 503 when credentials aren't set up.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    credentials = service(request)
    credential = ApiCredential(
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        urls=list(dict.fromkeys(body.urls)),
        auth_kind=body.auth.kind,
        auth_settings={},
    )
    _apply_auth(credentials, credential, body.auth, new=True)
    session.add(credential)
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, credentials, credential)


@router.get("/organizations/{organization_id}/credentials/{credential_id}")
async def get_credential(
    organization_id: NodeId,
    credential_id: CredentialId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CredentialRead:
    """
    One of the organization's API credentials.
    \f
    :raises HTTPException: 403 without organizations:read in the organization;
        404 when it has no such credential.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    credentials = service(request)
    return await _read(
        session, credentials, await _credential(session, organization_id, credential_id)
    )


@router.patch("/organizations/{organization_id}/credentials/{credential_id}")
async def update_credential(
    organization_id: NodeId,
    credential_id: CredentialId,
    body: CredentialUpdate,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CredentialRead:
    """
    Change an API credential. Signing in differently (another method, its
    settings or secrets changed) drops its sign-in: its tokens were for how
    it signed in before.
    \f
    :raises HTTPException: 403 without credentials:manage; 404 when the
        organization has no such credential; 409 when the name is taken; 422
        for an auth method there isn't, or settings or secrets it doesn't take.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    credentials = service(request)
    credential = await _credential(session, organization_id, credential_id)
    if body.name is not None:
        credential.name = body.name
    if body.description is not None:
        credential.description = body.description
    if body.urls is not None:
        credential.urls = list(dict.fromkeys(body.urls))
    if body.auth is not None and _apply_auth(
        credentials, credential, body.auth, new=False
    ):
        credentials.set_grant(credential, None)
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, credentials, credential)


@router.delete(
    "/organizations/{organization_id}/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_credential(
    organization_id: NodeId,
    credential_id: CredentialId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Delete an API credential, with its secrets and sign-in. Steps and tools
    that name it fail until they're given another.
    \f
    :raises HTTPException: 403 without credentials:manage; 404 when the
        organization has no such credential.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    credential = await _credential(session, organization_id, credential_id)
    await session.execute(
        delete(ApiCredentialOAuthFlow).where(
            ApiCredentialOAuthFlow.credential_id == credential.id
        )
    )
    await session.delete(credential)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/organizations/{organization_id}/credentials/{credential_id}/oauth/start")
async def start_credential_oauth(
    organization_id: NodeId,
    credential_id: CredentialId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> OAuthStart:
    """
    Begin signing in with the credential: answers where to send the person,
    who comes back to the web console within ten minutes.
    \f
    :raises HTTPException: 403 without credentials:manage; 404 when the
        organization has no such credential; 422 when its auth method isn't
        one a person signs in with; 502 when its settings can't be used.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    credentials = service(request)
    credential = await _credential(session, organization_id, credential_id)
    settings = request.app.state.settings
    redirect_uri = f"{settings.web_url.rstrip('/')}/oauth/credentials/callback"
    state = secrets.token_urlsafe(32)
    try:
        authorization = await credentials.begin(
            credential, redirect_uri=redirect_uri, state=state
        )
    except ValueError as error:
        raise _unprocessable(str(error)) from None
    except (AuthError, SecretsError) as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from None
    now = utc_now()
    # Sign-ins nobody finished.
    await session.execute(
        delete(ApiCredentialOAuthFlow).where(ApiCredentialOAuthFlow.expires_at < now)
    )
    session.add(
        ApiCredentialOAuthFlow(
            state=state,
            credential_id=credential.id,
            user_id=user,
            redirect_uri=redirect_uri,
            pending=credentials.box.seal(authorization.pending) or "",
            expires_at=now + FLOW_LIFETIME,
        )
    )
    await session.commit()
    return OAuthStart(authorization_url=authorization.url)


@router.delete("/organizations/{organization_id}/credentials/{credential_id}/oauth")
async def disconnect_credential_oauth(
    organization_id: NodeId,
    credential_id: CredentialId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CredentialRead:
    """
    Forget the credential's sign-in: its tokens are dropped, and steps and
    tools using it fail until someone signs in again.
    \f
    :raises HTTPException: 403 without credentials:manage; 404 when the
        organization has no such credential.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    credentials = service(request)
    credential = await _credential(session, organization_id, credential_id)
    credentials.set_grant(credential, None)
    await session.commit()
    return await _read(session, credentials, credential)


@router.post("/credential-oauth/callback")
async def finish_credential_oauth(
    body: OAuthCallback,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> CredentialRead:
    """
    Finish a sign-in with what the person came back with: only they may,
    once, within ten minutes of starting it.
    \f
    :raises HTTPException: 400 when the sign-in is unknown, someone else's,
        expired, or the authorization server refused it; 403 without
        credentials:manage in the credential's organization; 502 when the
        code wasn't exchanged.
    """
    credentials = service(request)
    flow = await session.get(ApiCredentialOAuthFlow, body.state)
    if flow is None or flow.user_id != user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    await session.delete(flow)
    await session.commit()
    if flow.expires_at < utc_now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    credential = await session.get(ApiCredential, flow.credential_id)
    if credential is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, SIGN_IN_GONE)
    await authorize(
        session, enforcer, user, MANAGE, Scope(Level.ORG, credential.organization_id)
    )
    if body.error or not body.code:
        reason = body.error_description or body.error or "no code came back"
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"The sign-in didn't finish: {reason}"
        )
    try:
        pending = credentials.box.open(flow.pending)
        await credentials.complete(
            credential,
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
    return await _read(session, credentials, credential)
