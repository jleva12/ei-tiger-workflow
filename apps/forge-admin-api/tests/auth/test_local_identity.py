"""The local development identity, /me/access without one, and the seed's guard."""

import re

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forge_admin.api.routes import me
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import ROLE_KEY_PATTERN
from forge_admin.cli import seed
from forge_admin.config import Settings
from forge_admin.db.audit import current_actor

ADMIN_ID = "d2a66448-9446-46be-81ca-a8c73197df05"

router = APIRouter()


@router.get("/whoami")
async def whoami(request: Request) -> dict[str, str | None]:
    return {"user": request.state.user_id, "actor": current_actor()}


def test_local_user_is_the_user_and_the_audit_actor(settings: Settings) -> None:
    local = settings.model_copy(update={"local_user_id": ADMIN_ID})
    with TestClient(ApiServer(local, routers=[router]).create_app()) as client:
        assert client.get("/api/v1/whoami").json() == {
            "user": ADMIN_ID,
            "actor": ADMIN_ID,
        }


def test_without_a_local_user_there_is_no_me(settings: Settings) -> None:
    server = ApiServer(settings, routers=[router, me.router])
    with TestClient(server.create_app()) as client:
        assert client.get("/api/v1/whoami").json() == {
            "user": None,
            "actor": "anonymous",
        }
        response = client.get("/api/v1/me/access")
    assert response.status_code == 401


def test_local_user_setting_validation() -> None:
    blank = Settings(_env_file=None, mysql_password="x", local_user_id="")
    assert blank.local_user_id is None
    with pytest.raises(ValidationError, match="local_user_id"):
        Settings(_env_file=None, mysql_password="x", local_user_id="has space")


def test_seed_needs_the_site_admin(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="FORGE_ADMIN_SITE_ADMIN_MSID"):
        seed.main()
    partial = settings.model_copy(
        update={
            "site_admin_id": ADMIN_ID,
            "site_admin_first_name": "Ada",
            "site_admin_last_name": "Lovelace",
            "site_admin_email": "not-an-email",
            "site_admin_msid": "alovelace",
        }
    )
    monkeypatch.setattr(seed, "get_settings", lambda: partial)
    with pytest.raises(SystemExit, match="email"):
        seed.main()


def test_seeded_role_key_is_a_site_role() -> None:
    assert re.fullmatch(ROLE_KEY_PATTERN, seed.SITE_ADMIN_ROLE)
    assert seed.SITE_ADMIN_ROLE.startswith("site:")
