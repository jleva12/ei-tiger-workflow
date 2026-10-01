"""Audit columns on every table, and the actor each request writes as."""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr

import forge_admin.models  # noqa: F401  registers every table
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings
from forge_admin.db.audit import current_actor
from forge_admin.db.base import Base

AUDIT_COLUMNS = {"created_at", "created_by", "updated_at", "updated_by"}

router = APIRouter()


@router.get("/whoami")
async def whoami() -> dict[str, str]:
    return {"actor": current_actor()}


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_every_table_has_audit_columns(table: str) -> None:
    columns = Base.metadata.tables[table].columns
    assert set(columns.keys()) >= AUDIT_COLUMNS
    for name in AUDIT_COLUMNS:
        assert not columns[name].nullable
        assert columns[name].server_default is not None
    # The audit columns come after the table's own columns.
    assert set(list(columns.keys())[-4:]) == AUDIT_COLUMNS


def test_changes_outside_a_request_are_by_system() -> None:
    assert current_actor() == "system"


def test_requests_without_an_api_key_are_anonymous(settings: Settings) -> None:
    with TestClient(ApiServer(settings, routers=[router]).create_app()) as client:
        assert client.get("/api/v1/whoami").json() == {"actor": "anonymous"}


def test_requests_with_the_api_key_are_by_api_key(settings: Settings) -> None:
    keyed = settings.model_copy(update={"api_key": SecretStr("secret-key")})
    with TestClient(ApiServer(keyed, routers=[router]).create_app()) as client:
        response = client.get("/api/v1/whoami", headers={"X-API-Key": "secret-key"})
    assert response.json() == {"actor": "api-key"}
