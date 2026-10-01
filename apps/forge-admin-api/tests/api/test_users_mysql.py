"""Users, managed by the site administrator, against MySQL."""

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.models import User

pytestmark = pytest.mark.mysql

API = "/api/v1"


@dataclass
class Directory:
    admin: TestClient
    admin_id: str
    tag: str
    org: str

    def person(self, name: str, **fields: str) -> dict[str, str]:
        """A new user's fields: unique to this test, overridable."""
        return {
            "first_name": name.title(),
            "last_name": "Tester",
            "email": f"{name}-{self.tag}@example.com",
            "msid": f"{name}{self.tag}",
            **fields,
        }

    def add(self, name: str) -> str:
        """Add a user named ``name``; return their ID."""
        response = self.admin.post(f"{API}/users", json=self.person(name))
        assert response.status_code == 201
        return response.json()["id"]


@pytest.fixture
def directory(
    site_admin: str,
    client_as: Callable[[str], TestClient],
    in_db: Callable[[Callable[[AsyncSession], Awaitable[object]]], object],
) -> Iterator[Directory]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    org = admin.post(f"{API}/organizations", json={"name": f"Org {tag}"}).json()["id"]
    yield Directory(admin, site_admin, tag, org)

    admin.delete(f"{API}/organizations/{org}")

    # In the database: the API won't let the admin remove their own user.
    async def remove_users(session: AsyncSession) -> None:
        await session.execute(
            delete(User).where(User.email.like(f"%-{tag}@example.com"))
        )
        await session.commit()

    in_db(remove_users)


def test_site_admin_manages_users(
    directory: Directory, client_as: Callable[[str], TestClient]
) -> None:
    admin, tag, person = directory.admin, directory.tag, directory.person
    ada = directory.add("ada")
    user = admin.get(f"{API}/users/{ada}").json()
    assert (user["first_name"], user["last_name"]) == ("Ada", "Tester")
    assert (user["email"], user["msid"]) == (f"ada-{tag}@example.com", f"ada{tag}")
    assert user["created_by"] == directory.admin_id

    # Emails and MS IDs are stored in lowercase, so each is unique ignoring
    # case, and a clash says which.
    same_email = person("ada2", email=f"ADA-{tag}@Example.com")
    response = admin.post(f"{API}/users", json=same_email)
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "A user with this email exists",
    )
    same_msid = person("ada3", msid=f"ADA{tag}")
    response = admin.post(f"{API}/users", json=same_msid)
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "A user with this MS ID exists",
    )
    for bad in (
        person("x", email="not-an-email"),
        person("x", msid="has space"),
        person("x", first_name=" "),
    ):
        assert admin.post(f"{API}/users", json=bad).status_code == 422

    # An ID can be given, to match an existing subject; not a role-like one.
    own_id = f"sso:{tag}"
    given = person("bo", id=own_id)
    assert admin.post(f"{API}/users", json=given).json()["id"] == own_id
    assert admin.post(f"{API}/users", json=given).status_code == 409
    role_like = person("x", id="org:admin")
    assert admin.post(f"{API}/users", json=role_like).status_code == 422

    renamed = admin.patch(
        f"{API}/users/{ada}", json={"last_name": "Lovelace", "msid": f"alove{tag}"}
    )
    assert (renamed.json()["last_name"], renamed.json()["msid"]) == (
        "Lovelace",
        f"alove{tag}",
    )
    # Its own values are no clash; another user's are.
    assert admin.patch(
        f"{API}/users/{ada}", json={"email": f"ada-{tag}@example.com"}
    ).is_success
    taken = admin.patch(f"{API}/users/{ada}", json={"msid": f"bo{tag}"})
    assert taken.status_code == 409

    # Anyone signed in can list users; only the site's users:* can change them.
    as_ada = client_as(ada)
    listed = [u["id"] for u in as_ada.get(f"{API}/users").json()]
    assert {ada, own_id} <= set(listed)
    denied = as_ada.patch(f"{API}/users/{ada}", json={"first_name": "x"})
    assert denied.status_code == 403
    assert as_ada.delete(f"{API}/users/{own_id}").status_code == 403

    # Removing a user revokes their roles; nobody can remove themselves.
    assert admin.put(
        f"{API}/scopes/org:{directory.org}/members/{ada}/roles/org:viewer"
    ).is_success
    assert admin.delete(f"{API}/users/{ada}").status_code == 204
    assert admin.get(f"{API}/users/{ada}").status_code == 404
    assert client_as(ada).get(f"{API}/me/memberships").json() == []
    admin.post(f"{API}/users", json=person("me", id=directory.admin_id))
    assert admin.delete(f"{API}/users/{directory.admin_id}").status_code == 409
