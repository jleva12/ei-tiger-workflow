"""The local development seed against MySQL."""

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.users import UserCreate
from forge_admin.cli import seed
from forge_admin.models import CasbinRule, User

pytestmark = pytest.mark.mysql

API = "/api/v1"
InDb = Callable[[Callable[[AsyncSession], Awaitable[object]]], object]


def test_seed_makes_a_site_administrator_once(
    in_db: InDb, client_as: Callable[[str], TestClient]
) -> None:
    tag = uuid4().hex[:8]
    admin_id = f"seed-{tag}"
    profile = UserCreate(
        id=admin_id,
        first_name="Ada",
        last_name="Lovelace",
        email=f"Ada-{tag}@Example.com",
        msid=f"ALOVE{tag}",
    )
    try:
        assert in_db(lambda session: seed.seed(session, profile)) is True
        assert in_db(lambda session: seed.seed(session, profile)) is False

        admin = client_as(admin_id)
        me = admin.get(f"{API}/me").json()
        assert me["subject"] == admin_id
        assert (me["user"]["email"], me["user"]["msid"]) == (
            f"ada-{tag}@example.com",
            f"alove{tag}",
        )
        access = admin.get(f"{API}/me/access").json()
        assert access["roles"] == ["site:admin"]
        assert ["site:admin", "*", "*"] in access["policies"]

        # The site role covers every organization; creating one also makes
        # them its administrator.
        org = admin.post(
            f"{API}/organizations", json={"name": f"Seeded {admin_id}"}
        ).json()
        scoped = admin.get(
            f"{API}/me/access", params={"scope": f"org:{org['id']}"}
        ).json()
        assert scoped["roles"] == ["org:admin", "site:admin"]
        assert admin.delete(f"{API}/organizations/{org['id']}").status_code == 204
    finally:

        async def unassign(session: AsyncSession) -> None:
            await session.execute(delete(CasbinRule).where(CasbinRule.v0 == admin_id))
            await session.execute(delete(User).where(User.id == admin_id))
            await session.commit()

        in_db(unassign)
