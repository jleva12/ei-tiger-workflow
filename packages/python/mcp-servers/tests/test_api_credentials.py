"""API credentials: where each may be sent, what each method sends (a key in a
header or the query, a bearer token, basic auth, OAuth client credentials and
sign-in, against a fake token endpoint), and credentials kept in the admin's
table, read without its models."""

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from forge_mcp_servers import (
    CREDENTIAL_METHODS,
    ApiCredentials,
    CredentialUnavailable,
    SecretBox,
    SqlCredentialStore,
    StoredCredential,
    request_credentials,
    sends_to,
)
from forge_mcp_servers.api_credentials import CREDENTIALS
from forge_mcp_servers.auth import NotConnected

KEY = "k" * 40
API = "https://api.example.com/v1"
TOKEN_URL = "https://auth.example.com/oauth/token"
AUTHORIZE_URL = "https://auth.example.com/authorize"
REDIRECT = "http://localhost:5190/oauth/credentials/callback"


class TokenEndpoint:
    """An authorization server's token endpoint: what each request asked
    for, and a new access token for each."""

    def __init__(self, *, lifetime: int = 3600, refused: bool = False) -> None:
        self.lifetime = lifetime
        self.refused = refused
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def form(self, index: int = -1) -> dict[str, str]:
        body = self.requests[index].content.decode()
        return {k: v[0] for k, v in parse_qs(body).items()}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) != TOKEN_URL:
            return httpx.Response(404)
        if self.refused:
            return httpx.Response(400, json={"error": "invalid_grant"})
        issued = len(self.requests)
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{issued}",
                "refresh_token": f"refresh-{issued}",
                "expires_in": self.lifetime,
                "token_type": "Bearer",
            },
        )


def row(kind: str, settings: dict[str, Any], secrets: dict[str, Any], **rest: Any):
    box = SecretBox(KEY)
    return StoredCredential(
        id="cred_1",
        organization_id="org_1",
        name="Orders API",
        urls=rest.pop("urls", [API]),
        auth_kind=kind,
        auth_settings=CREDENTIAL_METHODS.get(kind).settings(settings).model_dump(),
        auth_secrets=box.seal(secrets),
        **rest,
    )


# ------------------------------------------------------------------ where


@pytest.mark.parametrize(
    ("url", "sent"),
    [
        ("https://api.example.com/v1", True),
        ("https://api.example.com/v1/orders?page=2", True),
        ("https://API.example.com:443/v1/orders", True),
        ("https://api.example.com/v10", False),
        ("https://api.example.com/v2/orders", False),
        ("http://api.example.com/v1/orders", False),
        ("https://api.example.com:8443/v1", False),
        ("https://evil.example.com/v1", False),
        ("https://api.example.com.evil.io/v1", False),
        ("not a url", False),
        # Where the request lands, its dot segments resolved.
        ("https://api.example.com/v1/./orders", True),
        ("https://api.example.com/v1/orders/../charges", True),
        ("https://api.example.com/v1/../admin", False),
        ("https://api.example.com/v1/%2e%2e/admin", False),
        ("https://api.example.com/v1/.%2E/admin", False),
        ("https://api.example.com/v1/..", False),
    ],
)
def test_a_credential_is_sent_only_under_its_urls(url: str, sent: bool) -> None:
    assert sends_to(url, [API]) is sent


def test_an_origin_takes_every_path_on_it() -> None:
    assert sends_to(
        "https://api.example.com/anything/at/all", ["https://api.example.com"]
    )
    assert sends_to("https://api.example.com", ["https://api.example.com/"])
    assert not sends_to("https://api.example.com/x", [])


# --------------------------------------------------------------- methods


async def test_static_methods_send_their_secrets_where_they_go() -> None:
    service = ApiCredentials(SecretBox(KEY))
    header = await service.credentials(
        row("api_key", {"header": "X-Api-Key"}, {"key": "k1"})
    )
    assert header.headers == {"X-Api-Key": "k1"} and header.params == {}
    query = await service.credentials(
        row("api_key_query", {"parameter": "token"}, {"key": "k2"})
    )
    assert query.headers == {} and query.params == {"token": "k2"}
    bearer = await service.credentials(row("bearer", {}, {"token": "t3"}))
    assert bearer.headers == {"Authorization": "Bearer t3"}
    basic = await service.credentials(
        row("basic", {"username": "sk_live"}, {"password": ""})
    )
    assert basic.headers == {
        "Authorization": f"Basic {base64.b64encode(b'sk_live:').decode()}"
    }


async def test_client_credentials_fetch_a_token_and_reuse_it_until_it_runs_out() -> (
    None
):
    endpoint = TokenEndpoint()
    service = ApiCredentials(SecretBox(KEY), transport=endpoint.transport)
    credential = row(
        "oauth_client_credentials",
        {
            "client_id": "forge",
            "token_url": TOKEN_URL,
            "scopes": "orders:read",
            "audience": "https://api.example.com",
            "client_authentication": "body",
        },
        {"client_secret": "s3cret"},
    )
    first = await service.credentials(credential)
    assert first.headers == {"Authorization": "Bearer access-1"}
    assert endpoint.form() == {
        "grant_type": "client_credentials",
        "scope": "orders:read",
        "audience": "https://api.example.com",
        "client_id": "forge",
        "client_secret": "s3cret",
    }
    # Kept encrypted, and used while it lasts.
    assert credential.auth_grant and "access-1" not in credential.auth_grant
    again = await service.credentials(credential)
    assert again.headers == first.headers and len(endpoint.requests) == 1


async def test_client_credentials_sign_in_with_basic_auth_by_default() -> None:
    endpoint = TokenEndpoint(lifetime=1)  # Runs out at once: asked again.
    service = ApiCredentials(SecretBox(KEY), transport=endpoint.transport)
    credential = row(
        "oauth_client_credentials",
        {"client_id": "forge", "token_url": TOKEN_URL},
        {"client_secret": "s3cret"},
    )
    await service.credentials(credential)
    second = await service.credentials(credential)
    assert second.headers == {"Authorization": "Bearer access-2"}
    sent = endpoint.requests[0].headers["authorization"]
    assert sent == f"Basic {base64.b64encode(b'forge:s3cret').decode()}"
    assert "client_secret" not in endpoint.form()


async def test_signing_in_exchanges_the_code_with_pkce_and_refreshes() -> None:
    endpoint = TokenEndpoint(lifetime=1)
    service = ApiCredentials(SecretBox(KEY), transport=endpoint.transport)
    credential = row(
        "oauth",
        {
            "authorization_url": AUTHORIZE_URL,
            "token_url": TOKEN_URL,
            "client_id": "forge",
            "scopes": "orders",
            "authorization_params": "access_type=offline&prompt=consent",
        },
        {"client_secret": "s3cret"},
    )
    with pytest.raises(NotConnected):
        await service.credentials(credential)
    begun = await service.begin(credential, redirect_uri=REDIRECT, state="st-1")
    assert begun.url.startswith(f"{AUTHORIZE_URL}?")
    query = {k: v[0] for k, v in parse_qs(urlsplit(begun.url).query).items()}
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256" and query["state"] == "st-1"
    assert query["scope"] == "orders" and query["redirect_uri"] == REDIRECT

    await service.complete(
        credential, pending=begun.pending, code="c-1", redirect_uri=REDIRECT, user="u-1"
    )
    exchanged = endpoint.form(0)
    assert exchanged["grant_type"] == "authorization_code"
    assert exchanged["code_verifier"] == begun.pending["verifier"]
    connection = service.connection(credential)
    assert connection is not None and connection.connected
    assert connection.connected_by == "u-1"

    # Its token runs out within the margin (it lasts a second): refreshed
    # with the refresh token.
    refreshed = await service.credentials(credential)
    assert refreshed.headers == {"Authorization": "Bearer access-2"}
    assert endpoint.form(1) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
    }


async def test_a_refused_refresh_asks_for_signing_in_again() -> None:
    endpoint = TokenEndpoint(refused=True)
    service = ApiCredentials(SecretBox(KEY), transport=endpoint.transport)
    credential = row(
        "oauth",
        {
            "authorization_url": AUTHORIZE_URL,
            "token_url": TOKEN_URL,
            "client_id": "forge",
        },
        {},
    )
    service.set_grant(
        credential,
        {"tokens": {"access_token": "old", "refresh_token": "r", "expires_at": 0}},
    )
    with pytest.raises(NotConnected, match="sign in again on the Credentials page"):
        await service.credentials(credential)


async def test_a_request_elsewhere_is_refused_and_says_where_it_may_go() -> None:
    service = ApiCredentials(SecretBox(KEY))
    credential = row("bearer", {}, {"token": "t"})
    sent = await service.for_request(credential, f"{API}/orders")
    assert sent.headers == {"Authorization": "Bearer t"}
    with pytest.raises(CredentialUnavailable) as refused:
        await service.for_request(credential, "https://evil.example.com/collect")
    assert str(refused.value) == (
        "The credential Orders API is only sent to https://api.example.com/v1, "
        "not to https://evil.example.com"
    )


async def test_secrets_that_cant_be_read_say_so() -> None:
    credential = row("bearer", {}, {"token": "t"})
    other = ApiCredentials(SecretBox("x" * 40))
    with pytest.raises(CredentialUnavailable, match="FORGE_ADMIN_SECRETS_KEY"):
        await other.for_request(credential, API)


# ----------------------------------------------------------------- store


@pytest.fixture
async def engine() -> Any:
    made = create_async_engine("sqlite+aiosqlite://")
    async with made.begin() as conn:
        await conn.run_sync(CREDENTIALS.metadata.create_all)
    yield made
    await made.dispose()


async def add(engine: AsyncEngine, box: SecretBox, **values: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(CREDENTIALS).values(
                **{
                    "id": "cred_1",
                    "organization_id": "org_1",
                    "name": "Orders API",
                    "urls": json.dumps([API]),
                    "auth_kind": "oauth_client_credentials",
                    "auth_settings": json.dumps(
                        {"client_id": "forge", "token_url": TOKEN_URL}
                    ),
                    "auth_secrets": box.seal({"client_secret": "s3cret"}),
                    "auth_grant": None,
                }
                | values
            )
        )


async def test_a_request_keeps_the_renewed_grant_on_the_row(engine) -> None:
    box = SecretBox(KEY)
    endpoint = TokenEndpoint()
    await add(engine, box)
    service = ApiCredentials(box, transport=endpoint.transport)
    store = SqlCredentialStore(engine)
    found = await request_credentials(
        service, store, credential_id="cred_1", organization_id="org_1", url=f"{API}/x"
    )
    assert found.headers == {"Authorization": "Bearer access-1"}
    async with engine.connect() as conn:
        kept = (await conn.execute(sa.select(CREDENTIALS.c.auth_grant))).scalar_one()
    assert box.open(kept)["tokens"]["access_token"] == "access-1"
    # Read back: no new token asked for.
    await request_credentials(
        service, store, credential_id="cred_1", organization_id="org_1", url=API
    )
    assert len(endpoint.requests) == 1


async def test_another_organizations_or_a_deleted_credential_isnt_found(engine) -> None:
    box = SecretBox(KEY)
    await add(engine, box)
    service, store = ApiCredentials(box), SqlCredentialStore(engine)
    for organization, credential in (("org_2", "cred_1"), ("org_1", "cred_9")):
        with pytest.raises(CredentialUnavailable, match="it was deleted"):
            await request_credentials(
                service,
                store,
                credential_id=credential,
                organization_id=organization,
                url=API,
            )
    with pytest.raises(CredentialUnavailable, match="organization's runs"):
        await request_credentials(
            service, store, credential_id="cred_1", organization_id=None, url=API
        )
