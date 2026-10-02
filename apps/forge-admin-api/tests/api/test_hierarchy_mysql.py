"""Organizations and scoped roles, end to end against MySQL.

A site administrator creates two organizations, Acme and Globex, then assigns
roles; each user then acts as themselves.
"""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from forge_admin.agents.person import Person, database_people
from forge_admin.config import Settings
from forge_admin.db.session import create_engine

pytestmark = pytest.mark.mysql

API = "/api/v1"


@dataclass
class Site:
    admin: TestClient
    admin_id: str
    tag: str
    acme: str
    globex: str

    def user(self, name: str) -> str:
        return f"{name}-{self.tag}"

    def name(self, organization: str) -> str:
        return f"{organization} {self.tag}"

    def assign(self, scope: str, user: str, role: str) -> int:
        return self.admin.put(
            f"{API}/scopes/{scope}/members/{user}/roles/{role}"
        ).status_code


def names(response: object) -> list[str]:
    return [item["name"] for item in response.json()]  # type: ignore[attr-defined]


@pytest.fixture
def site(site_admin: str, client_as: Callable[[str], TestClient]) -> Iterator[Site]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    acme, globex = (
        admin.post(f"{API}/organizations", json={"name": f"{name} {tag}"}).json()["id"]
        for name in ("Acme", "Globex")
    )
    yield Site(admin, site_admin, tag, acme, globex)

    # Deleting an organization also revokes the roles assigned in it.
    for organization in admin.get(f"{API}/organizations").json():
        if organization["name"].endswith(f" {tag}"):
            admin.delete(f"{API}/organizations/{organization['id']}")


def test_site_admin_sets_up_organizations(site: Site) -> None:
    acme = site.admin.get(f"{API}/organizations/{site.acme}").json()
    assert acme["name"] == site.name("Acme")
    assert acme["domain"] == f"org:{site.acme}"
    assert acme["created_by"] == site.admin_id

    listed = [
        o["name"]
        for o in site.admin.get(f"{API}/organizations").json()
        if o["name"].endswith(site.tag)
    ]
    assert listed == [site.name("Acme"), site.name("Globex")]
    taken = site.admin.post(f"{API}/organizations", json={"name": site.name("Acme")})
    assert taken.status_code == 409

    renamed = site.admin.patch(
        f"{API}/organizations/{site.globex}", json={"description": "Sales"}
    )
    assert renamed.status_code == 200 and renamed.json()["description"] == "Sales"


def test_roles_apply_in_their_organization_only(
    site: Site, client_as: Callable[[str], TestClient]
) -> None:
    oscar, mo, olga = site.user("oscar"), site.user("mo"), site.user("olga")
    assert site.assign(f"org:{site.acme}", oscar, "org:admin") == 200
    assert site.assign(f"org:{site.acme}", mo, "org:member") == 200
    assert site.assign(f"org:{site.acme}", olga, "org:viewer") == 200

    # An organization admin runs their organization, not another, nor the site.
    as_oscar = client_as(oscar)
    changed = as_oscar.patch(
        f"{API}/organizations/{site.acme}", json={"description": "x"}
    )
    assert changed.status_code == 200
    elsewhere = as_oscar.patch(
        f"{API}/organizations/{site.globex}", json={"description": "x"}
    )
    assert elsewhere.status_code == 403
    assert as_oscar.post(f"{API}/organizations", json={"name": "X"}).status_code == 403
    assert (
        as_oscar.delete(f"{API}/organizations/{site.acme}").status_code == 403
    )  # only the site deletes one
    assert names(as_oscar.get(f"{API}/organizations")) == [site.name("Acme")]

    # A member reads their organization only.
    as_mo = client_as(mo)
    assert as_mo.get(f"{API}/organizations/{site.acme}").status_code == 200
    assert as_mo.get(f"{API}/organizations/{site.globex}").status_code == 403
    assert (
        as_mo.patch(
            f"{API}/organizations/{site.acme}", json={"description": "x"}
        ).status_code
        == 403
    )
    access = as_mo.get(f"{API}/me/access", params={"scope": f"org:{site.acme}"}).json()
    assert access["roles"] == ["org:member"]
    assert ["org:member", "agents", "run"] in access["policies"]
    elsewhere_access = as_mo.get(
        f"{API}/me/access", params={"scope": f"org:{site.globex}"}
    ).json()
    assert elsewhere_access["roles"] == []
    assert as_mo.get(f"{API}/me/memberships").json() == [
        {"scope": f"org:{site.acme}", "role": "org:member"}
    ]

    # A viewer reads, and changes nothing.
    as_olga = client_as(olga)
    assert names(as_olga.get(f"{API}/organizations")) == [site.name("Acme")]
    assert (
        as_olga.patch(
            f"{API}/organizations/{site.acme}", json={"description": "x"}
        ).status_code
        == 403
    )


def test_whoever_creates_an_organization_administers_it(site: Site) -> None:
    # The fixture's site administrator created both: each is theirs to run.
    for organization in (site.acme, site.globex):
        members = site.admin.get(f"{API}/scopes/org:{organization}/members").json()
        assert [(m["subject_id"], m["role"]) for m in members] == [
            (site.admin_id, "org:admin")
        ]
    mine = [
        (o["name"], o["roles"])
        for o in site.admin.get(f"{API}/me/organizations").json()
        if o["name"].endswith(site.tag)
    ]
    assert mine == [
        (site.name("Acme"), ["org:admin"]),
        (site.name("Globex"), ["org:admin"]),
    ]
    # A name that's taken creates nothing, the assignment included.
    before = len(site.admin.get(f"{API}/me/memberships").json())
    taken = site.admin.post(f"{API}/organizations", json={"name": site.name("Acme")})
    assert taken.status_code == 409
    assert len(site.admin.get(f"{API}/me/memberships").json()) == before


def test_member_management(site: Site, client_as: Callable[[str], TestClient]) -> None:
    oscar, mo = site.user("oscar"), site.user("mo")
    site.assign(f"org:{site.acme}", oscar, "org:admin")
    as_oscar = client_as(oscar)
    members = f"{API}/scopes/org:{site.acme}/members"

    # An organization admin manages its members, but not another's or the site's.
    assert as_oscar.put(f"{members}/{mo}/roles/org:member").status_code == 200
    other = f"{API}/scopes/org:{site.globex}/members/{mo}/roles/org:member"
    assert as_oscar.put(other).status_code == 403
    assert (
        as_oscar.put(f"{API}/scopes/site/members/{mo}/roles/site:admin").status_code
        == 403
    )

    # Roles are assigned at their own level, and must exist.
    assert site.assign(f"org:{site.acme}", mo, "site:admin") == 422
    assert site.assign("site", mo, "org:admin") == 422
    assert site.assign(f"org:{site.acme}", mo, "org:nope") == 422
    assert site.assign(f"org:{site.acme}", mo, "team:lead") == 422
    assert site.assign(f"team:{site.acme}", mo, "org:member") == 422
    assert site.assign(f"org:{site.acme}", "org:admin", "org:member") == 422

    # By subject: mo-… before oscar-…; the creator (the site admin) aside.
    listed = [
        m for m in site.admin.get(members).json() if m["subject_id"] != site.admin_id
    ]
    assert [(m["subject_id"], m["role"], m["created_by"]) for m in listed] == [
        (mo, "org:member", oscar),
        (oscar, "org:admin", site.admin_id),
    ]
    theirs = as_oscar.get(
        f"{API}/authz/subjects/{mo}/access", params={"scope": f"org:{site.acme}"}
    )
    assert theirs.json()["roles"] == ["org:member"]

    assert as_oscar.delete(f"{members}/{mo}/roles/org:member").status_code == 204
    left = {m["subject_id"] for m in site.admin.get(members).json()}
    assert left == {oscar, site.admin_id}


def test_deleting_an_organization_revokes_its_roles(
    site: Site, client_as: Callable[[str], TestClient]
) -> None:
    mo = site.user("mo")
    site.assign(f"org:{site.acme}", mo, "org:member")
    site.assign(f"org:{site.globex}", mo, "org:viewer")

    assert site.admin.delete(f"{API}/organizations/{site.acme}").status_code == 204
    assert site.admin.get(f"{API}/organizations/{site.acme}").status_code == 404
    # Their roles in it went with it; others stay.
    assert client_as(mo).get(f"{API}/me/memberships").json() == [
        {"scope": f"org:{site.globex}", "role": "org:viewer"}
    ]


def test_members_active_in_an_organization(site: Site) -> None:
    mo, zed = site.user("mo"), site.user("zed")
    site.assign(f"org:{site.acme}", mo, "org:member")
    # Elsewhere: another organization.
    site.assign(f"org:{site.globex}", zed, "org:member")
    members = f"{API}/scopes/org:{site.acme}/members"

    def listed(scope_members: str = members, **params: bool) -> set[tuple[str, ...]]:
        response = site.admin.get(scope_members, params=params)
        return {(m["subject_id"], m["role"], m["scope"]) for m in response.json()}

    here = (mo, "org:member", f"org:{site.acme}")
    # Its creator, the site administrator, administers it.
    creator = (site.admin_id, "org:admin", f"org:{site.acme}")
    # The site administrator holds site:admin on the site, which applies here.
    above = (site.admin_id, "site:admin", "site")
    assert listed() == {here, creator}
    assert {here, above} <= listed(above=True)
    assert all(subject != zed for subject, _, _ in listed(above=True))
    # The site's members, with every organization's below it.
    everyone = listed(f"{API}/scopes/site/members", below=True)
    assert {here, above, (zed, "org:member", f"org:{site.globex}")} <= everyone


def test_my_organizations_are_those_i_am_a_member_of(
    site: Site, client_as: Callable[[str], TestClient]
) -> None:
    mo, olga = site.user("mo"), site.user("olga")
    site.assign(f"org:{site.acme}", mo, "org:member")
    site.assign(f"org:{site.acme}", mo, "org:admin")

    def organizations_of(user: str) -> list[tuple[str, list[str]]]:
        found = client_as(user).get(f"{API}/me/organizations").json()
        return [(o["name"], o["roles"]) for o in found]

    # A role in an organization makes you a member of it.
    assert organizations_of(mo) == [(site.name("Acme"), ["org:admin", "org:member"])]
    # A site role doesn't, and nobody without a role has organizations.
    other_admin = site.user("root")
    assert site.assign("site", other_admin, "site:admin") == 200
    assert organizations_of(other_admin) == []
    site.admin.delete(f"{API}/scopes/site/members/{other_admin}/roles/site:admin")
    assert organizations_of(olga) == []
    site.assign(f"org:{site.globex}", olga, "org:viewer")
    assert organizations_of(olga) == [(site.name("Globex"), ["org:viewer"])]


def test_the_assistant_knows_whom_it_talks_to(
    site: Site, mysql_settings: Settings
) -> None:
    mo = site.user("mo")
    site.assign(f"org:{site.acme}", mo, "org:admin")

    async def look_up(user: str) -> Person | None:
        engine = create_engine(mysql_settings)
        try:
            return await database_people(engine)(user)
        finally:
            await engine.dispose()

    person = asyncio.run(look_up(mo))
    assert person is not None and person.id == mo
    # Their organizations and their roles in each; they hold none site-wide.
    assert [(o.id, o.name, o.roles) for o in person.organizations] == [
        (site.acme, site.name("Acme"), ["org:admin"])
    ]
    assert person.roles == []
    admin = asyncio.run(look_up(site.admin_id))
    assert admin is not None
    # The organizations they created, as their administrator.
    created = {o.id: o.roles for o in admin.organizations}
    assert created[site.acme] == created[site.globex] == ["org:admin"]
    assert ("site:admin", "site") in [(r.role, r.scope) for r in admin.roles]
