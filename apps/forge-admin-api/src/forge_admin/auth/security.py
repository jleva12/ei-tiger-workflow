"""Who is calling: an organization's API key, the deployment's API key, the
user's bearer token, and the audit actor."""

import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from forge_admin.auth.access import set_caller
from forge_admin.auth.api_keys import (
    ApiKeyError,
    KeyIdentity,
    looks_like_key,
    verify_key,
)
from forge_admin.auth.tokens import TokenError, groups_of, verify_identity
from forge_admin.db.audit import set_actor

# Declaring the headers through FastAPI's security classes also documents
# them in OpenAPI, so /docs shows an Authorize button for each.
api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="An organization's API key (fk_…), or the deployment's API key",
)
bearer = HTTPBearer(
    auto_error=False,
    description="A JWT whose sub is the user's ID, or an organization's API key (fk_…)",
)

# Audit actors for requests without a user.
API_KEY_ACTOR = "api-key"
ANONYMOUS_ACTOR = "anonymous"

ONE_CREDENTIAL = "Send one credential: an API key or a sign-in token"


def _refused(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _identify(
    request: Request,
    api_key: str | None,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    gate: bool,
    local: bool,
) -> None:
    """
    Identify the caller, and record them for the rest of the request.

    :param gate: Require the deployment's API key, when one is configured
        (an organization's key stands in for it).
    :param local: Fall back to ``FORGE_ADMIN_LOCAL_USER_ID`` without credentials.
    :raises HTTPException: 401 for a key or token that isn't valid, mixed
        credentials, or a missing deployment key.
    """
    settings = request.app.state.settings
    token = credentials.credentials if credentials is not None else None
    sent = [v for v in (token, api_key) if v is not None and looks_like_key(v)]
    key: KeyIdentity | None = None
    if sent:
        # A key with a sign-in token, or two different keys: whose call is it?
        if len(set(sent)) > 1 or (token is not None and not looks_like_key(token)):
            raise _refused(ONE_CREDENTIAL)
        try:
            key = await verify_key(request.app.state.sessionmaker, sent[0])
        except ApiKeyError as error:
            raise _refused(str(error)) from None
    expected = settings.api_key
    if (
        key is None
        and gate
        and expected is not None
        and (
            api_key is None
            or not hmac.compare_digest(
                api_key.encode(), expected.get_secret_value().encode()
            )
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    user_id: str | None = None
    groups: tuple[str, ...] = ()
    if key is not None:
        user_id = key.subject
    elif token is not None:
        try:
            identity = verify_identity(settings, token)
        except TokenError as error:
            raise _refused(str(error)) from None
        user_id, groups = identity.subject, identity.groups
    elif local and settings.local_user_id:
        user_id = settings.local_user_id
        groups = groups_of(settings.local_user_groups)
    request.state.user_id = user_id
    request.state.groups = groups
    request.state.api_key = key
    set_caller(user_id, groups)
    fallback = ANONYMOUS_ACTOR if expected is None or not gate else API_KEY_ACTOR
    set_actor(user_id or fallback)


async def authenticate(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    """
    Check the deployment's API key, if one is configured, and identify the
    caller.

    The caller is an organization's API key (``fk_…``, as a bearer token or
    ``X-API-Key``; it also passes the deployment's key check), else the user
    of an ``Authorization: Bearer <JWT>`` header (see
    ``tokens.verify_identity``), else, in local development and tests,
    ``FORGE_ADMIN_LOCAL_USER_ID``, else no one. ``request.state.user_id``
    holds the caller (``apikey:<id>`` for a key), ``request.state.groups``
    the company groups their token names (whose linked permissions
    authorization counts), ``request.state.api_key`` the key's identity, and
    changes are audited as the caller, or as ``api-key`` when only the
    deployment's key was verified, or ``anonymous``. This dependency is async
    so the actor stays set for the route that follows.

    :param request: The current request, used to read the settings.
    :param api_key: The ``X-API-Key`` header value, if sent.
    :param credentials: The bearer token, if sent.
    :raises HTTPException: 401 when the deployment's key is missing or
        wrong, or a key or token isn't valid.
    """
    await _identify(request, api_key, credentials, gate=True, local=True)


async def identify(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    """
    Identify the caller from the credentials they sent, asking for none: for
    the runtime when it's public. Without credentials the caller is
    anonymous (not the local user: a public runtime's anonymous callers are
    anonymous everywhere); credentials that are sent must be valid.

    :raises HTTPException: 401 for a key or token that isn't valid.
    """
    await _identify(request, api_key, credentials, gate=False, local=False)
