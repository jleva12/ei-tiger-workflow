"""The auth methods' protocol and the plain methods: what each sends, what
its form has, and how secrets are kept."""

import asyncio
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel, Field

from forge_admin.mcp_servers.auth import (
    AuthContext,
    AuthMethod,
    AuthMethods,
    Credentials,
    NoFields,
)
from forge_admin.mcp_servers.registry import AUTH_METHODS
from forge_admin.mcp_servers.secrets import SecretBox, SecretsError

KEY = "k" * 40


def credentials(
    kind: str, settings: dict[str, Any], secrets: dict[str, Any]
) -> Credentials:
    method = AUTH_METHODS.get(kind)

    async def main() -> Credentials:
        async with httpx.AsyncClient() as http:
            return await method.credentials(
                AuthContext(
                    server_url="https://mcp.example.com/mcp",
                    settings=method.settings(settings),
                    secrets=method.secrets(secrets),
                    grant={},
                    http=http,
                )
            )

    return asyncio.run(main())


def test_the_registry_has_every_method_once() -> None:
    assert [method.kind for method in AUTH_METHODS] == [
        "none",
        "api_key",
        "bearer",
        "oauth",
        "oauth_client_credentials",
    ]
    with pytest.raises(ValueError, match="Two auth methods"):
        AUTH_METHODS.register(AUTH_METHODS.get("none"))
    with pytest.raises(ValueError, match="no auth method 'saml'"):
        AUTH_METHODS.get("saml")


def test_none_sends_nothing() -> None:
    assert credentials("none", {}, {}).headers == {}


def test_an_api_key_goes_in_its_header_after_its_scheme() -> None:
    assert credentials("api_key", {}, {"key": "abc"}).headers == {"X-API-Key": "abc"}
    sent = credentials(
        "api_key", {"header": "Authorization", "scheme": "Token"}, {"key": "abc"}
    )
    assert sent.headers == {"Authorization": "Token abc"}


def test_a_bearer_token_is_sent_as_authorization() -> None:
    assert credentials("bearer", {}, {"token": "t0"}).headers == {
        "Authorization": "Bearer t0"
    }


def test_settings_and_secrets_are_checked() -> None:
    api_key = AUTH_METHODS.get("api_key")
    with pytest.raises(ValueError, match="header"):
        api_key.settings({"header": "Not a header"})
    with pytest.raises(ValueError, match="key"):
        api_key.secrets({})
    with pytest.raises(ValueError, match="Extra inputs"):
        api_key.settings({"colour": "blue"})
    oauth = AUTH_METHODS.get("oauth")
    # Everything is optional: Forge registers itself and finds the rest.
    assert oauth.settings({}).model_dump() == {
        "client_id": "",
        "scopes": "",
        "authorization_server": "",
    }


def test_a_method_describes_its_form() -> None:
    info = AUTH_METHODS.get("api_key").describe()
    assert info.interactive is False
    assert [(f.name, f.secret, f.required, f.default) for f in info.fields] == [
        ("header", False, False, "X-API-Key"),
        ("scheme", False, False, ""),
        ("key", True, True, ""),
    ]
    assert info.fields[2].label == "API key"
    assert AUTH_METHODS.get("oauth").describe().interactive is True


def test_a_new_method_is_one_class_registered() -> None:
    class Signed(BaseModel):
        secret: str = Field(min_length=1, title="Signing secret")

    class HmacAuth(AuthMethod[NoFields, Signed]):
        kind = "hmac"
        label = "HMAC"
        description = "Signs each connection."
        secrets_model = Signed

        async def credentials(
            self, context: AuthContext[NoFields, Signed]
        ) -> Credentials:
            return Credentials({"X-Signature": context.secrets.secret[::-1]})

    methods = AuthMethods([HmacAuth()])
    assert methods.get("hmac").describe().fields[0].label == "Signing secret"


def test_secrets_are_sealed_and_only_opened_with_their_key() -> None:
    box = SecretBox(KEY)
    sealed = box.seal({"key": "abc"})
    assert sealed and "abc" not in sealed
    assert box.open(sealed) == {"key": "abc"}
    assert box.seal({}) is None and box.open(None) == {}
    with pytest.raises(SecretsError, match="FORGE_ADMIN_SECRETS_KEY changed"):
        SecretBox("x" * 40).open(sealed)
    with pytest.raises(ValueError, match="32"):
        SecretBox("short")
