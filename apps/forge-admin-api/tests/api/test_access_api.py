"""Request handling that needs no database: sign-in, validation, the model file."""

import pytest
from fastapi.testclient import TestClient

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import MODEL_PATH
from forge_admin.config import Settings

ORG = "org:00000000-0000-4000-8000-00000000000c"
USER = "d2a66448-9446-46be-81ca-a8c73197df05"


def client(settings: Settings, user: str | None = None) -> TestClient:
    configured = settings.model_copy(update={"local_user_id": user})
    return TestClient(ApiServer(configured, routers=ROUTERS).create_app())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/organizations"),
        ("POST", "/api/v1/organizations"),
        ("GET", "/api/v1/roles"),
        ("GET", "/api/v1/me/access"),
        ("GET", f"/api/v1/scopes/{ORG}/members"),
    ],
)
def test_endpoints_need_a_signed_in_user(
    settings: Settings, method: str, path: str
) -> None:
    with client(settings) as test_client:
        response = test_client.request(method, path, json={"name": "x"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/me/access?scope=org:1", None),
        ("GET", f"/api/v1/me/access?scope=team:{USER}", None),
        ("GET", "/api/v1/scopes/group:x/members", None),
        ("PUT", f"/api/v1/scopes/{ORG}/members/has space/roles/org:member", None),
        ("PUT", f"/api/v1/scopes/{ORG}/members/{USER}/roles/member", None),
        ("PUT", f"/api/v1/scopes/{ORG}/members/{USER}/roles/team:lead", None),
        ("GET", "/api/v1/organizations/not-a-uuid", None),
        ("GET", f"/api/v1/scopes/team:{USER}/members", None),
        ("GET", f"/api/v1/scopes/app:{USER}/members", None),
        ("GET", f"/api/v1/scopes/tenant:{USER}/members", None),
        ("POST", "/api/v1/roles", {"key": "tenant:admin", "name": "No tenants"}),
        ("POST", "/api/v1/organizations", {"name": ""}),
        ("POST", "/api/v1/roles", {"key": "admin", "name": "No level"}),
        ("POST", "/api/v1/roles", {"key": "group:admin", "name": "Unknown level"}),
        ("POST", "/api/v1/roles", {"key": "team:lead", "name": "No teams"}),
        ("POST", "/api/v1/permissions", {"key": "organizations"}),
        ("POST", "/api/v1/permissions", {"key": "Organizations:read"}),
    ],
)
def test_malformed_requests_are_rejected(
    settings: Settings, method: str, path: str, body: dict[str, str] | None
) -> None:
    with client(settings, USER) as test_client:
        response = test_client.request(method, path, json=body)
    assert response.status_code == 422


def test_the_shared_model_is_served(settings: Settings) -> None:
    with client(settings) as test_client:
        response = test_client.get("/api/v1/authz/model")
    assert response.status_code == 200
    assert response.text == MODEL_PATH.read_text()
    assert "g(r.sub, p.sub, r.dom)" in response.text
