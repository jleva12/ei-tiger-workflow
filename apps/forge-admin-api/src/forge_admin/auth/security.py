"""Who is calling: the optional API key, the user's bearer token, the audit actor."""

import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from forge_admin.auth.access import set_caller
from forge_admin.auth.tokens import TokenError, groups_of, verify_identity
from forge_admin.db.audit import set_actor

# Declaring the headers through FastAPI's security classes also documents
# them in OpenAPI, so /docs shows an Authorize button for each.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer = HTTPBearer(auto_error=False, description="A JWT whose sub is the user's ID")

# Audit actors for requests without a user.
API_KEY_ACTOR = "api-key"
ANONYMOUS_ACTOR = "anonymous"


async def authenticate(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    """
    Check the API key, if one is configured, and identify the user.

    The key is compared in constant time. The user comes from an
    ``Authorization: Bearer <JWT>`` header (see ``tokens.verify_identity``);
    without one, from ``FORGE_ADMIN_LOCAL_USER_ID`` (local development and
    tests only), or there is none. ``request.state.user_id`` holds the user,
    ``request.state.groups`` the company groups their token names (whose
    linked permissions authorization counts),
    and changes are audited as them, or as ``api-key`` when a key was verified,
    or ``anonymous``. This dependency is async so the actor stays set for the
    route that follows.

    :param request: The current request, used to read the settings.
    :param api_key: The ``X-API-Key`` header value, if sent.
    :param credentials: The bearer token, if sent.
    :raises HTTPException: 401 when the key is missing or wrong, or the token
        isn't valid.
    """
    settings = request.app.state.settings
    expected = settings.api_key
    if expected is not None and (
        api_key is None
        or not hmac.compare_digest(
            api_key.encode(), expected.get_secret_value().encode()
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    user_id = settings.local_user_id
    groups = groups_of(settings.local_user_groups) if user_id else ()
    if credentials is not None:
        try:
            identity = verify_identity(settings, credentials.credentials)
            user_id, groups = identity.subject, identity.groups
        except TokenError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
    request.state.user_id = user_id
    request.state.groups = groups
    set_caller(user_id, groups)
    fallback = ANONYMOUS_ACTOR if expected is None else API_KEY_ACTOR
    set_actor(user_id or fallback)
