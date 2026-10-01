"""Bearer tokens: minting, verifying, and how the API identifies the user."""

from datetime import timedelta

import jwt
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from forge_admin.api.server import ApiServer
from forge_admin.auth.tokens import TokenError, mint_token, verify_token
from forge_admin.cli import token as token_cli
from forge_admin.config import Settings
from forge_admin.db.audit import current_actor, utc_now
from forge_admin.models import User

SECRET = "test-secret-that-is-at-least-32-chars"
USER = User(
    id="d2a66448-9446-46be-81ca-a8c73197df05",
    first_name="Ada",
    last_name="Lovelace",
    email="ada@example.com",
    msid="alovelace",
)
DAY = timedelta(days=1)

router = APIRouter()


@router.get("/whoami")
async def whoami(request: Request) -> dict[str, str | None]:
    return {"user": request.state.user_id, "actor": current_actor()}


@pytest.fixture
def signed(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "jwt_secret": SecretStr(SECRET),
            "jwt_issuer": "forge-local",
            "jwt_audience": "forge-admin",
        }
    )


def whoami_with(settings: Settings, token: str) -> tuple[int, dict[str, str]]:
    with TestClient(ApiServer(settings, routers=[router]).create_app()) as client:
        response = client.get(
            "/api/v1/whoami", headers={"Authorization": f"Bearer {token}"}
        )
    return response.status_code, response.json()


def test_a_minted_token_names_the_user(signed: Settings) -> None:
    token = mint_token(signed, USER, lifetime=DAY)
    assert verify_token(signed, token) == USER.id
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["msid"] == "alovelace"
    assert (claims["name"], claims["email"]) == ("Ada Lovelace", "ada@example.com")
    assert (claims["iss"], claims["aud"]) == ("forge-local", "forge-admin")
    assert claims["exp"] - claims["iat"] == DAY.total_seconds()


def test_the_token_is_the_user_and_the_audit_actor(signed: Settings) -> None:
    # A token outranks the local development user.
    local = signed.model_copy(update={"local_user_id": "someone-else"})
    status, body = whoami_with(local, mint_token(signed, USER, lifetime=DAY))
    assert (status, body) == (200, {"user": USER.id, "actor": USER.id})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"jwt_secret": SecretStr("another-secret-of-at-least-32-chars")}, "Invalid"),
        ({"jwt_audience": "someone-else"}, "Invalid"),
        ({"jwt_issuer": "someone-else"}, "Invalid"),
    ],
)
def test_tokens_for_other_keys_or_services_are_refused(
    signed: Settings, change: dict[str, object], reason: str
) -> None:
    token = mint_token(signed.model_copy(update=change), USER, lifetime=DAY)
    status, body = whoami_with(signed, token)
    assert status == 401
    assert body["detail"].startswith(reason)


def test_expired_and_malformed_tokens_are_refused(signed: Settings) -> None:
    expired = mint_token(signed, USER, lifetime=DAY, now=utc_now() - 2 * DAY)
    assert whoami_with(signed, expired) == (
        401,
        {"detail": "The bearer token has expired"},
    )
    assert whoami_with(signed, "not-a-jwt")[0] == 401
    role = User(**{**USER_FIELDS, "id": "site:admin"})
    assert whoami_with(signed, mint_token(signed, role, lifetime=DAY)) == (
        401,
        {"detail": "Invalid bearer token subject"},
    )


def test_without_a_secret_tokens_are_refused(
    settings: Settings, signed: Settings
) -> None:
    with pytest.raises(TokenError):
        mint_token(settings, USER, lifetime=DAY)
    token = mint_token(signed, USER, lifetime=DAY)
    assert whoami_with(settings, token) == (
        401,
        {"detail": "This API does not accept bearer tokens"},
    )


def test_the_secret_must_be_long() -> None:
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(_env_file=None, mysql_password="x", jwt_secret="short")


def test_minting_needs_a_secret_and_a_user(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(token_cli, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="FORGE_ADMIN_JWT_SECRET"):
        token_cli.main([])
    keyed = settings.model_copy(update={"jwt_secret": SecretStr(SECRET)})
    monkeypatch.setattr(token_cli, "get_settings", lambda: keyed)
    with pytest.raises(SystemExit, match="--msid"):
        token_cli.main([])


USER_FIELDS = {
    "id": USER.id,
    "first_name": USER.first_name,
    "last_name": USER.last_name,
    "email": USER.email,
    "msid": USER.msid,
}
