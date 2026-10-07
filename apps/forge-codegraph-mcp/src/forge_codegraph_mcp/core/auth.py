"""Authentication for the MCP endpoint.

Auth is injected through ``ServerBuilder.with_auth``, which takes any FastMCP
``AuthProvider``. This server's is ``access.ForgeAccessVerifier``: Forge
credentials, which the admin API checks, each reading one organization's
repositories. Several providers can be combined (:func:`combine_auth`).

The same providers protect API route classes that set ``requires_auth = True`` (see
:func:`http_bearer_auth`).
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.server.auth import AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.auth import AccessToken


def combine_auth(
    providers: Sequence[AuthProvider], required_scopes: list[str] | None = None
) -> AuthProvider | None:
    """
    Combines providers so a request is accepted if any of them accepts its token.

    At most one provider may own OAuth routes and metadata (an OAuth proxy or a
    ``RemoteAuthProvider``); every other provider must be a plain ``TokenVerifier``
    (``JWTVerifier``, ``access.ForgeAccessVerifier``, ...). The OAuth provider is tried first.

    By default every token must carry the OAuth provider's required scopes (e.g. GitHub's
    ``user``), which API keys and JWTs from another issuer usually don't. Set
    ``required_scopes`` to change that, e.g. ``[]``; ``JWTVerifier`` still enforces its own.

    :param providers: Providers to accept tokens from.
    :type providers: Sequence[AuthProvider]
    :param required_scopes: Scopes every token must carry, overriding the OAuth provider's.
    :type required_scopes: list[str] | None
    :return: ``None`` for no providers, the provider itself for one, else a ``MultiAuth``.
    :rtype: AuthProvider | None
    :raises ValueError: If more than one provider owns OAuth routes.
    """
    if len(providers) <= 1:
        return providers[0] if providers else None
    servers = [p for p in providers if not isinstance(p, TokenVerifier)]
    verifiers = [p for p in providers if isinstance(p, TokenVerifier)]
    if len(servers) > 1:
        names = ", ".join(type(p).__name__ for p in servers)
        raise ValueError(f"only one auth provider may own OAuth routes; got {names}")
    return MultiAuth(
        server=servers[0] if servers else None, verifiers=verifiers, required_scopes=required_scopes
    )


def http_bearer_auth(provider: AuthProvider) -> Callable[..., Awaitable[AccessToken]]:
    """
    FastAPI dependency that authenticates API routes with the MCP endpoint's auth provider.

    The same tokens (API keys, JWTs, OAuth-issued tokens) work for both. ``ServerBuilder``
    applies it to route classes with ``requires_auth = True``.

    :param provider: The combined auth provider from ``ServerBuilder.with_auth``.
    :type provider: AuthProvider
    :return: A dependency returning the caller's verified access token.
    :rtype: Callable[..., Awaitable[AccessToken]]
    """
    scheme = HTTPBearer(auto_error=False, description="Same bearer token as the MCP endpoint")

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(scheme)],
    ) -> AccessToken:
        challenge = {"WWW-Authenticate": "Bearer"}
        if credentials is None:
            raise HTTPException(status_code=401, detail="Missing bearer token", headers=challenge)
        token = await provider.verify_token(credentials.credentials)
        if token is None:
            raise HTTPException(
                status_code=401, detail="Invalid or expired token", headers=challenge
            )
        if missing := set(provider.required_scopes or []) - set(token.scopes):
            raise HTTPException(
                status_code=403,
                detail=f"Missing required scopes: {', '.join(sorted(missing))}",
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
            )
        return token

    return authenticate
