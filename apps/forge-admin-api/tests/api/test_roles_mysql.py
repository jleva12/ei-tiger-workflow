"""Roles and permissions as data: editable through the API, audited, against MySQL."""

from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.models import CasbinRule

pytestmark = pytest.mark.mysql

API = "/api/v1"
InDb = Callable[[Callable[[AsyncSession], Awaitable[object]]], object]


@pytest.fixture
def admin(
    site_admin: str, client_as: Callable[[str], TestClient]
) -> Iterator[TestClient]:
    client = client_as(site_admin)
    yield client


@pytest.fixture
def prefix(admin: TestClient) -> Iterator[str]:
    prefix = f"t{uuid4().hex[:8]}"
    yield prefix
    for role in admin.get(f"{API}/roles").json():
        if prefix in role["key"]:
            admin.delete(f"{API}/roles/{role['key']}")
    for permission in admin.get(f"{API}/permissions").json():
        if prefix in permission["resource"]:
            admin.delete(f"{API}/permissions/{permission['id']}")


def grant_rows(in_db: InDb, role: str) -> list[CasbinRule]:
    async def load(session: AsyncSession) -> list[CasbinRule]:
        result = await session.scalars(
            select(CasbinRule)
            .where(CasbinRule.ptype == "p", CasbinRule.v0 == role)
            .order_by(CasbinRule.v2)
        )
        return list(result)

    rows = in_db(load)
    assert isinstance(rows, list)
    return rows


def test_default_roles_exist(admin: TestClient) -> None:
    roles = {role["key"]: role for role in admin.get(f"{API}/roles").json()}
    expected = {"site:admin", "org:admin", "org:member", "org:viewer"}
    assert set(roles) >= expected
    assert not [key for key in roles if not key.startswith(("site:", "org:"))]
    assert all(roles[key]["level"] == key.split(":")[0] for key in expected)
    assert "*:*" in [p["key"] for p in roles["site:admin"]["permissions"]]
    # The fixture's site administrator holds site:admin.
    assert roles["site:admin"]["member_count"] >= 1


def test_role_and_permission_lifecycle(admin: TestClient, prefix: str) -> None:
    resource = f"{prefix}_projects"
    read = admin.post(f"{API}/permissions", json={"key": f"{resource}:read"})
    write = admin.post(f"{API}/permissions", json={"key": f"{resource}:write"})
    assert (read.status_code, write.status_code) == (201, 201)
    read_id, write_id = read.json()["id"], write.json()["id"]
    assert (
        admin.post(f"{API}/permissions", json={"key": f"{resource}:read"}).status_code
        == 409
    )

    key = f"org:{prefix}_editor"
    created = admin.post(
        f"{API}/roles", json={"key": key, "name": "Editor", "permission_ids": [read_id]}
    )
    assert created.status_code == 201
    assert (created.json()["level"], created.json()["member_count"]) == ("org", 0)
    assert (
        admin.post(f"{API}/roles", json={"key": key, "name": "Again"}).status_code
        == 409
    )
    unknown = admin.post(
        f"{API}/roles",
        json={"key": f"org:{prefix}_x", "name": "X", "permission_ids": [0]},
    )
    assert unknown.status_code == 422

    edited = admin.patch(
        f"{API}/roles/{key}",
        json={"name": "Project editor", "permission_ids": [read_id, write_id]},
    ).json()
    assert edited["name"] == "Project editor"
    assert sorted(p["key"] for p in edited["permissions"]) == [
        f"{resource}:read",
        f"{resource}:write",
    ]

    # Changing a permission's key moves every grant with it.
    moved = admin.patch(
        f"{API}/permissions/{write_id}", json={"key": f"{prefix}_tasks:write"}
    )
    assert moved.json()["key"] == f"{prefix}_tasks:write"
    keys = [p["key"] for p in admin.get(f"{API}/roles/{key}").json()["permissions"]]
    assert sorted(keys) == [f"{resource}:read", f"{prefix}_tasks:write"]

    assert admin.delete(f"{API}/permissions/{read_id}").status_code == 204
    keys = [p["key"] for p in admin.get(f"{API}/roles/{key}").json()["permissions"]]
    assert keys == [f"{prefix}_tasks:write"]

    assert admin.delete(f"{API}/roles/{key}").status_code == 204
    assert admin.get(f"{API}/roles/{key}").status_code == 404


def test_changing_roles_needs_site_permission(
    client_as: Callable[[str], TestClient],
) -> None:
    nobody = client_as(f"user-{uuid4().hex[:8]}")
    assert nobody.get(f"{API}/roles").status_code == 200
    assert (
        nobody.post(f"{API}/roles", json={"key": "org:x", "name": "X"}).status_code
        == 403
    )
    assert nobody.post(f"{API}/permissions", json={"key": "x:read"}).status_code == 403


def test_writes_are_audited(
    admin: TestClient, prefix: str, site_admin: str, in_db: InDb
) -> None:
    read = admin.post(
        f"{API}/permissions", json={"key": f"{prefix}_projects:read"}
    ).json()
    assert read["created_by"] == read["updated_by"] == site_admin
    assert read["created_at"] == read["updated_at"]

    key = f"org:{prefix}_editor"
    role = admin.post(
        f"{API}/roles",
        json={"key": key, "name": "Editor", "permission_ids": [read["id"]]},
    ).json()
    edited = admin.patch(f"{API}/roles/{key}", json={"name": "Renamed"}).json()
    assert edited["created_at"] == role["created_at"]
    assert datetime.fromisoformat(edited["updated_at"]) > datetime.fromisoformat(
        role["updated_at"]
    )

    # A grant the role keeps is not rewritten when its permissions change.
    [grant] = grant_rows(in_db, key)
    assert grant.created_by == site_admin
    write = admin.post(
        f"{API}/permissions", json={"key": f"{prefix}_projects:write"}
    ).json()
    admin.patch(
        f"{API}/roles/{key}", json={"permission_ids": [read["id"], write["id"]]}
    )
    kept, added = grant_rows(in_db, key)
    assert (kept.id, kept.created_at) == (grant.id, grant.created_at)
    # Changing what a role grants counts as updating the role.
    regranted = admin.get(f"{API}/roles/{key}").json()
    assert regranted["updated_at"] > edited["updated_at"]

    # Moving a permission is a bulk UPDATE, and still stamps updated_at.
    admin.patch(
        f"{API}/permissions/{write['id']}", json={"key": f"{prefix}_tasks:write"}
    )
    _, moved = grant_rows(in_db, key)
    assert moved.updated_at > added.updated_at
    assert moved.created_at == added.created_at
