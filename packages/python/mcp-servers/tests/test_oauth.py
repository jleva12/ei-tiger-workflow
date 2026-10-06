"""OAuth for MCP servers, against a fake server and authorization server:
discovery, registering Forge, signing in with PKCE, refreshing, and client
credentials."""

import asyncio
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest

from forge_mcp_servers.auth import AuthContext, AuthError, NotConnected
from forge_mcp_servers.oauth import discover
from forge_mcp_servers.registry import AUTH_METHODS
from forge_mcp_servers.testing import ISSUER, MCP, FakeOAuth

REDIRECT = "http://localhost:5190/oauth/mcp/callback"


def context(
    fake: FakeOAuth,
    http: httpx.AsyncClient,
    kind: str = "oauth",
    settings: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    grant: dict[str, Any] | None = None,
) -> AuthContext[Any, Any]:
    method = AUTH_METHODS.get(kind)
    return AuthContext(
        server_url=MCP,
        settings=method.settings(settings or {}),
        secrets=method.secrets(secrets or {}),
        grant=grant or {},
        http=http,
    )


def run(fake: FakeOAuth, work: Any) -> Any:
    async def main() -> Any:
        async with httpx.AsyncClient(transport=fake.transport) as http:
            return await work(http)

    return asyncio.run(main())


def sign_in(fake: FakeOAuth, **settings: Any) -> dict[str, Any]:
    """Begin, sign in, and complete: the grant."""
    method = AUTH_METHODS.get("oauth")

    async def work(http: httpx.AsyncClient) -> dict[str, Any]:
        ctx = context(fake, http, settings=settings)
        begun = await method.begin(ctx, redirect_uri=REDIRECT, state="st-1")
        code = fake.authorize(begun.url)
        return await method.complete(
            ctx, pending=begun.pending, code=code, redirect_uri=REDIRECT
        )

    return run(fake, work)


def test_discovery_follows_the_servers_challenge_to_its_authorization_server() -> None:
    fake = FakeOAuth()
    found = run(fake, lambda http: discover(http, MCP))
    assert found.resource == MCP
    assert found.issuer == ISSUER
    assert found.authorization_endpoint == f"{ISSUER}/authorize"
    assert found.token_endpoint == f"{ISSUER}/token"
    assert found.registration_endpoint == f"{ISSUER}/register"
    # The scope the challenge asks for, over every scope the server has.
    assert found.scope == "tools:read"


def test_a_server_from_before_resource_metadata_is_its_own_authorization_server() -> (
    None
):
    fake = FakeOAuth(resource_metadata=False)
    found = run(fake, lambda http: discover(http, MCP))
    assert found.issuer == "https://mcp.example.com"
    assert found.token_endpoint == "https://mcp.example.com/token"
    assert found.registration_endpoint == "https://mcp.example.com/register"


def test_signing_in_registers_forge_and_asks_with_pkce_for_the_server() -> None:
    fake = FakeOAuth()
    method = AUTH_METHODS.get("oauth")

    async def work(http: httpx.AsyncClient) -> Any:
        return await method.begin(
            context(fake, http), redirect_uri=REDIRECT, state="st-1"
        )

    begun = run(fake, work)
    url = urlsplit(begun.url)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    assert query == {
        "response_type": "code",
        "client_id": "client-0",
        "redirect_uri": REDIRECT,
        "state": "st-1",
        "code_challenge": query["code_challenge"],
        "code_challenge_method": "S256",
        "resource": MCP,
        "scope": "tools:read",
    }
    assert fake.clients["client-0"]["redirect_uris"] == [REDIRECT]
    assert fake.clients["client-0"]["token_endpoint_auth_method"] == "none"
    # The verifier stays with Forge.
    assert begun.pending["verifier"] not in begun.url
    assert begun.pending["client"]["registered"] is True


def test_a_client_id_set_up_is_used_rather_than_registering() -> None:
    fake = FakeOAuth()
    grant = sign_in(fake, client_id="client-mine", scopes="tools:write")
    assert fake.clients == {}
    assert grant["client"]["client_id"] == "client-mine"
    assert grant["tokens"]["scope"] == "tools:read"


def test_without_registration_a_client_id_is_needed() -> None:
    fake = FakeOAuth(registration=False)
    with pytest.raises(AuthError, match="enter a client ID"):
        sign_in(fake)


def test_a_signed_in_server_gets_its_access_token_and_refreshes_it() -> None:
    fake = FakeOAuth()
    grant = sign_in(fake)
    assert grant["tokens"]["access_token"] == "access-1"
    method = AUTH_METHODS.get("oauth")
    assert method.connection(grant).connected is True

    async def credentials(http: httpx.AsyncClient, grant: dict[str, Any]) -> Any:
        return await method.credentials(context(fake, http, grant=grant))

    sent = run(fake, lambda http: credentials(http, grant))
    assert sent.headers == {"Authorization": "Bearer access-1"}
    assert sent.grant is None

    grant["tokens"]["expires_at"] = time.time() + 30
    sent = run(fake, lambda http: credentials(http, grant))
    assert sent.headers == {"Authorization": "Bearer access-2"}
    assert sent.grant["tokens"]["refresh_token"] == "refresh-2"
    assert sent.grant["client"] == grant["client"]

    # The old refresh token was rotated away: someone must sign in again.
    with pytest.raises(NotConnected, match="connect the server again"):
        run(fake, lambda http: credentials(http, grant))


def test_a_refresh_that_doesnt_rotate_keeps_the_refresh_token() -> None:
    fake = FakeOAuth(rotate=False)
    grant = sign_in(fake)
    grant["tokens"]["expires_at"] = 0
    method = AUTH_METHODS.get("oauth")

    async def work(http: httpx.AsyncClient) -> Any:
        return await method.credentials(context(fake, http, grant=grant))

    sent = run(fake, work)
    assert sent.grant["tokens"]["refresh_token"] == "refresh-1"


def test_a_server_nobody_signed_in_to_isnt_connected() -> None:
    fake = FakeOAuth()
    method = AUTH_METHODS.get("oauth")

    async def work(http: httpx.AsyncClient) -> Any:
        return await method.credentials(context(fake, http))

    with pytest.raises(NotConnected, match="connect it"):
        run(fake, work)
    assert method.connection({}).connected is False


def test_a_code_exchanged_with_another_verifier_is_refused() -> None:
    fake = FakeOAuth()
    method = AUTH_METHODS.get("oauth")

    async def work(http: httpx.AsyncClient) -> Any:
        ctx = context(fake, http)
        begun = await method.begin(ctx, redirect_uri=REDIRECT, state="st-1")
        code = fake.authorize(begun.url)
        pending = begun.pending | {"verifier": "x" * 64}
        return await method.complete(
            ctx, pending=pending, code=code, redirect_uri=REDIRECT
        )

    with pytest.raises(NotConnected, match="invalid_grant"):
        run(fake, work)


def test_client_credentials_find_the_token_endpoint_and_reuse_the_token() -> None:
    fake = FakeOAuth()
    method = AUTH_METHODS.get("oauth_client_credentials")
    settings = {"client_id": "svc"}

    async def work(http: httpx.AsyncClient, grant: dict[str, Any]) -> Any:
        return await method.credentials(
            context(
                fake,
                http,
                "oauth_client_credentials",
                settings,
                {"client_secret": "s3cret"},
                grant,
            )
        )

    sent = run(fake, lambda http: work(http, {}))
    assert sent.headers == {"Authorization": "Bearer access-1"}
    token_request = next(r for r in fake.requests if r.url.path == "/token")
    form = parse_qs(token_request.content.decode())
    assert form["scope"] == ["tools:read"] and form["resource"] == [MCP]

    again = run(fake, lambda http: work(http, sent.grant))
    assert again.headers == {"Authorization": "Bearer access-1"} and again.grant is None


def test_client_credentials_that_are_refused_say_why_without_the_response() -> None:
    fake = FakeOAuth()
    method = AUTH_METHODS.get("oauth_client_credentials")

    async def work(http: httpx.AsyncClient) -> Any:
        return await method.credentials(
            context(
                fake,
                http,
                "oauth_client_credentials",
                {"client_id": "svc", "token_url": f"{ISSUER}/token"},
                {"client_secret": "wrong"},
            )
        )

    with pytest.raises(AuthError, match=r"answered 401 \(invalid_client\)"):
        run(fake, work)
