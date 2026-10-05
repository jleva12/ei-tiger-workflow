"""The company's groups linked to permissions, without MySQL: the tables on
SQLite and the real Casbin enforcer over them. A site administrator links a
group; someone whose token names the group gets its permissions on the whole
site, and loses them when the link changes or goes.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Integer, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.access import groups_of_caller
from forge_admin.auth.authorization import create_enforcer, get_enforcer
from forge_admin.auth.security import authenticate
from forge_admin.auth.tokens import TokenError, groups_of, mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.models import CasbinRule, Permission, User

API = "/api/v1"
ADMIN, PERSON = "admin-1", "person-1"
GROUP = "CN=Forge Writers,OU=Groups"
SECRET = SecretStr("s" * 40)


def sqlite_tables() -> MetaData:
    """The tables, as SQLite takes them: without MySQL's microsecond defaults."""
    tables = MetaData()
    for name in (
        "organizations",
        "users",
        "casbin_rule",
        "authz_permissions",
        "authz_group_links",
    ):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    # SQLite numbers rows only for an INTEGER primary key.
    tables.tables["casbin_rule"].c.id.type = Integer()
    return tables


@dataclass
class Site:
    client: TestClient
    settings: Settings
    permissions: dict[str, int]
    sessions: async_sessionmaker[AsyncSession]

    def token(self, user: str, groups: list[str] | None = None) -> str:
        token = mint_subject_token(self.settings, user, lifetime=timedelta(minutes=5))
        if not groups:
            return token
        claims = jwt.decode(token, options={"verify_signature": False})
        return jwt.encode(
            {**claims, "groups": groups},
            SECRET.get_secret_value(),
            algorithm="HS256",
        )

    def call(
        self,
        method: str,
        path: str,
        user: str = ADMIN,
        body: Any = None,
        groups: list[str] | None = None,
    ) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.token(user, groups)}"},
            json=body,
        )

    def rules(self) -> list[tuple[str | None, ...]]:
        async def read() -> list[tuple[str | None, ...]]:
            async with self.sessions() as session:
                found = await session.scalars(
                    select(CasbinRule).where(CasbinRule.v0.like("group:%"))
                )
                return sorted((r.v0, r.v1, r.v2) for r in found)

        assert self.client.portal is not None
        return self.client.portal.call(read)


@pytest.fixture
def site(settings: Settings) -> Iterator[Site]:
    configured = settings.model_copy(
        update={"jwt_secret": SECRET, "local_user_id": None, "jwt_issuer": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = create_enforcer(engine)

    async def session() -> Any:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer
    keys = ["permissions:create", "permissions:update", "users:create"]

    async def seed() -> dict[str, int]:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            permissions = [
                Permission(resource=k.split(":")[0], action=k.split(":")[1])
                for k in keys
            ]
            made.add_all(permissions)
            made.add_all(
                [
                    User(
                        id=ADMIN,
                        first_name="Ada",
                        last_name="Admin",
                        email="a@x.io",
                        msid="a",
                    ),
                    CasbinRule(ptype="p", v0="site:admin", v1="*", v2="*"),
                    CasbinRule(ptype="g", v0=ADMIN, v1="site:admin", v2="*"),
                ]
            )
            await made.commit()
            return {p.key: p.id for p in permissions}

    with TestClient(app) as client:
        assert client.portal is not None
        ids = client.portal.call(seed)
        yield Site(client, configured, ids, sessions)
        client.portal.call(engine.dispose)


def new_permission(key: str, groups: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "description": "", **({"groups": groups} if groups else {})}


def link(site: Site, key: str, groups: list[str]) -> Any:
    """Link a seeded permission to exactly these groups."""
    return site.call(
        "PATCH", f"/permissions/{site.permissions[key]}", body={"groups": groups}
    )


def test_a_linked_groups_members_get_its_permissions_everywhere(site: Site) -> None:
    # Without the link, the group's members can't.
    assert (
        site.call("POST", "/permissions", PERSON, new_permission("a:b"), [GROUP])
    ).status_code == 403

    linked = link(site, "permissions:create", [GROUP])
    assert linked.status_code == 200, linked.json()
    assert linked.json()["groups"] == [GROUP]
    # One Casbin token, for all the name's commas.
    assert site.rules() == [
        ("group:CN=Forge Writers%2COU=Groups", "permissions", "create")
    ]

    # A member of the group can; someone else can't, nor the member without it.
    assert (
        site.call("POST", "/permissions", PERSON, new_permission("a:b"), [GROUP])
    ).status_code == 201
    assert (
        site.call("POST", "/permissions", PERSON, new_permission("a:c"))
    ).status_code == 403
    assert (
        site.call("POST", "/permissions", PERSON, new_permission("a:c"), ["Other"])
    ).status_code == 403
    # Only what's linked.
    person = {"email": "z@x.io", "first_name": "Z", "last_name": "Z", "msid": "z"}
    assert site.call("POST", "/users", PERSON, person, [GROUP]).status_code == 403

    # Their access says so, in every scope, for the web console.
    access = site.call("GET", "/me/access", PERSON, groups=[GROUP]).json()
    assert ["group:CN=Forge Writers%2COU=Groups", "permissions", "create"] in (
        access["policies"]
    )
    assert site.call("GET", "/me/access", PERSON).json()["policies"] == []


def test_a_permission_links_many_groups_and_a_group_many_permissions(
    site: Site,
) -> None:
    other = "Forge Leads"
    assert link(site, "permissions:create", [GROUP, other]).json()["groups"] == sorted(
        [GROUP, other]
    )
    link(site, "users:create", [other])
    # Either group gets it.
    for key, group in (("x:one", GROUP), ("x:two", other)):
        made = site.call("POST", "/permissions", PERSON, new_permission(key), [group])
        assert made.status_code == 201
    listed = {p["key"]: p["groups"] for p in site.call("GET", "/permissions").json()}
    assert listed["permissions:create"] == sorted([GROUP, other])
    assert listed["users:create"] == [other]
    groups = {
        g["group_name"]: [p["key"] for p in g["permissions"]]
        for g in site.call("GET", "/permission-groups").json()
    }
    assert groups == {
        other: ["permissions:create", "users:create"],
        GROUP: ["permissions:create"],
    }

    # Unlinking one group from one permission leaves the rest.
    assert link(site, "permissions:create", [other]).json()["groups"] == [other]
    assert (
        site.call("POST", "/permissions", PERSON, new_permission("y:z"), [GROUP])
    ).status_code == 403
    # A group linked to nothing is forgotten.
    assert [g["group_name"] for g in site.call("GET", "/permission-groups").json()] == [
        other
    ]


def test_a_new_permission_comes_linked(site: Site) -> None:
    made = site.call(
        "POST", "/permissions", body=new_permission("reports:read", [GROUP])
    )
    assert made.status_code == 201, made.json()
    assert made.json()["groups"] == [GROUP]
    assert site.rules() == [("group:CN=Forge Writers%2COU=Groups", "reports", "read")]
    # A taken key is a 409, links and all.
    again = site.call(
        "POST", "/permissions", body=new_permission("reports:read", ["X"])
    )
    assert again.status_code == 409


def test_links_follow_a_permission_renamed_or_deleted(site: Site) -> None:
    link(site, "users:create", [GROUP])
    path = f"/permissions/{site.permissions['users:create']}"
    renamed = site.call("PATCH", path, body={"key": "users:add"})
    assert renamed.json()["groups"] == [GROUP]
    assert site.rules() == [("group:CN=Forge Writers%2COU=Groups", "users", "add")]
    assert site.call("DELETE", path).status_code == 204
    assert site.rules() == []
    assert site.call("GET", "/permission-groups").json() == []


def test_a_group_is_unlinked_from_everything(site: Site) -> None:
    link(site, "users:create", [GROUP])
    link(site, "permissions:create", [GROUP])
    group = site.call("GET", "/permission-groups").json()[0]
    assert (
        site.call("DELETE", f"/permission-groups/{group['id']}", PERSON).status_code
        == 403
    )
    assert site.call("DELETE", f"/permission-groups/{group['id']}").status_code == 204
    assert site.rules() == []
    assert all(p["groups"] == [] for p in site.call("GET", "/permissions").json())


def test_only_those_who_update_permissions_link_groups(site: Site) -> None:
    assert link(site, "users:create", [GROUP]).status_code == 200
    path = f"/permissions/{site.permissions['users:create']}"
    assert site.call("PATCH", path, PERSON, {"groups": []}).status_code == 403
    # Anyone signed in reads them.
    assert [
        g["group_name"] for g in site.call("GET", "/permission-groups", PERSON).json()
    ] == [GROUP]


@pytest.mark.parametrize(
    "name", [" Leading", "Trailing ", "", "Ünïcode", "x" * 201, "," * 100]
)
def test_a_group_name_the_directory_couldnt_have_is_refused(
    site: Site, name: str
) -> None:
    assert link(site, "users:create", [name]).status_code == 422


def test_nobody_signs_in_as_a_group(site: Site) -> None:
    with pytest.raises(TokenError):
        mint_subject_token(
            site.settings, f"group:{GROUP}", lifetime=timedelta(minutes=1)
        )
    created = site.call(
        "POST",
        "/users",
        body={
            "id": "group:x",
            "email": "g@x.io",
            "first_name": "G",
            "last_name": "G",
            "msid": "g",
        },
    )
    assert created.status_code == 422


def test_a_tokens_groups_claim_is_read_as_names() -> None:
    assert groups_of(["A", "B", "A", " bad", 3, "C"]) == ("A", "B", "C")
    assert groups_of("Solo") == ("Solo",)
    assert groups_of(None) == ()
    assert groups_of({"A": 1}) == ()


def test_a_local_user_has_their_configured_groups(settings: Settings) -> None:
    # FORGE_ADMIN_LOCAL_USER_GROUPS reaches authorization as a token's would.
    configured = settings.model_copy(
        update={"local_user_id": PERSON, "local_user_groups": [GROUP, " bad"]}
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=configured)),
        state=SimpleNamespace(),
    )

    async def authenticated() -> tuple[str, ...]:
        await authenticate(request, None, None)  # type: ignore[arg-type]
        return groups_of_caller(PERSON)

    assert asyncio.run(authenticated()) == (GROUP,)
    assert request.state.groups == (GROUP,)
