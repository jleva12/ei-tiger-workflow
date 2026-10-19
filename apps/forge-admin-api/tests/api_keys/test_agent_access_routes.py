"""An organization's own agent permissions: its admins make them and link
them to company groups and its own API keys; they count only in that
organization, and apart from the site's roles. There are no agent roles."""

from typing import Any

from agent_access_world import ACCESS, FINANCE, link, permission, with_groups
from key_world import ADMIN, MEMBER, NEIGHBOUR, ORG, OTHER_ORG, VIEWER, World
from sqlalchemy import select

from forge_admin.agent_access.policy import agent_access, agent_allows
from forge_admin.models import AgentCasbinRule, CasbinRule

OTHER_ACCESS = f"/organizations/{OTHER_ORG}/agent-access"
FINANCE_SUBJECT = "group:CN=Finance%2COU=Groups"


def grant_role(world: World, user: str, role: str, pattern: str) -> None:
    """Give someone one of the site's roles where a domain pattern says."""

    async def give(session: Any) -> None:
        session.add(CasbinRule(ptype="g", v0=user, v1=role, v2=pattern))
        await session.commit()

    world.run(give)


def lines(world: World) -> set[tuple[str | None, ...]]:
    async def read(session: Any) -> list[AgentCasbinRule]:
        return list(await session.scalars(select(AgentCasbinRule)))

    return {
        (rule.ptype, rule.v0, rule.v1, rule.v2, rule.v3) for rule in world.run(read)
    }


def my_access(
    world: World, user: str, groups: list[str] | None = None, base: str = ACCESS
) -> Any:
    # A sign-in naming the groups; without any, the world's own token.
    if groups:
        headers = with_groups(world, user, groups)
        return world.call("GET", f"{base}/me", None, headers=headers).json()
    return world.call("GET", f"{base}/me", user).json()


# ------------------------------------------------------------ setting it up


def test_an_admin_links_permissions_to_company_groups(world: World) -> None:
    made = world.call(
        "POST",
        f"{ACCESS}/permissions",
        ADMIN,
        {"key": "invoices:approve", "description": "Approve", "groups": [FINANCE]},
    )
    assert made.status_code == 201, made.text
    assert made.json()["groups"] == [FINANCE] and made.json()["api_keys"] == []
    assert lines(world) == {
        ("p", FINANCE_SUBJECT, f"org:{ORG}", "invoices", "approve"),
    }
    listed = world.call("GET", f"{ACCESS}/permissions", ADMIN).json()
    assert [(p["key"], p["groups"]) for p in listed] == [
        ("invoices:approve", [FINANCE])
    ]
    groups = world.call("GET", f"{ACCESS}/groups", ADMIN).json()
    assert [
        (g["group_name"], [p["key"] for p in g["permissions"]]) for g in groups
    ] == [(FINANCE, ["invoices:approve"])]

    # Whoever's sign-in names the group holds it; nobody else does.
    mine = my_access(world, VIEWER, [FINANCE])
    assert mine["permissions"] == ["invoices:approve"]
    assert mine["policies"] == [[FINANCE_SUBJECT, "invoices", "approve"]]
    assert my_access(world, VIEWER)["permissions"] == []
    assert my_access(world, MEMBER)["permissions"] == []


def test_only_those_who_manage_agent_access_change_it(world: World) -> None:
    permission(world, "invoices:approve")
    for user in (MEMBER, VIEWER, NEIGHBOUR):
        for path in ("/permissions", "/groups"):
            # None of them holds members:read in Acme.
            assert world.call("GET", f"{ACCESS}{path}", user).status_code == 403
        assert (
            world.call(
                "POST", f"{ACCESS}/permissions", user, {"key": "x:y"}
            ).status_code
            == 403
        )
    # Roles and who holds them aren't a thing here.
    assert world.call("GET", f"{ACCESS}/roles", ADMIN).status_code == 404
    assert world.call("GET", f"{ACCESS}/members", ADMIN).status_code == 404


def test_keys_are_checked_and_unique_in_the_organization(world: World) -> None:
    permission(world, "invoices:approve")
    taken = world.call(
        "POST", f"{ACCESS}/permissions", ADMIN, {"key": "invoices:approve"}
    )
    assert taken.status_code == 409
    assert (
        world.call(
            "POST", f"{ACCESS}/permissions", ADMIN, {"key": "Invoices approve"}
        ).status_code
        == 422
    )


# ------------------------------------------------------------ API keys


def test_a_permission_links_the_organizations_own_api_keys(world: World) -> None:
    made = world.make_key(name="Outside app")
    approve = permission(world, "invoices:approve")
    linked = link(world, approve, api_keys=[made["id"]])
    assert linked["api_keys"] == [{"id": made["id"], "name": "Outside app"}]
    assert ("p", made["subject"], f"org:{ORG}", "invoices", "approve") in lines(world)

    async def check(session: Any) -> bool:
        return await agent_allows(session, ORG, made["subject"], "invoices:approve")

    assert world.run(check)
    # Not one that isn't the organization's.
    refused = world.call(
        "PATCH", f"{ACCESS}/permissions/{approve}", ADMIN, {"api_keys": ["nope"]}
    )
    assert refused.status_code == 422
    # Deleting the key unlinks it.
    assert (
        world.call(
            "DELETE", f"/organizations/{ORG}/api-keys/{made['id']}", ADMIN
        ).status_code
        == 204
    )
    listed = world.call("GET", f"{ACCESS}/permissions", ADMIN).json()
    assert listed[0]["api_keys"] == []
    assert all(line[1] != made["subject"] for line in lines(world))


# ------------------------------------------------------------ one organization only


def test_an_organizations_permissions_count_only_there(world: World) -> None:
    grant_role(world, ADMIN, "org:admin", f"org:{OTHER_ORG}*")
    approve = permission(world, "invoices:approve", [FINANCE])

    # Globex hasn't linked the group to anything.
    assert world.call("GET", f"{OTHER_ACCESS}/permissions", ADMIN).json() == []
    assert (
        world.call(
            "PATCH",
            f"{OTHER_ACCESS}/permissions/{approve}",
            ADMIN,
            {"description": "x"},
        ).status_code
        == 404
    )
    assert my_access(world, NEIGHBOUR, [FINANCE])["permissions"] == ["invoices:approve"]
    assert (
        my_access(world, NEIGHBOUR, [FINANCE], base=OTHER_ACCESS)["permissions"] == []
    )

    async def check(session: Any) -> tuple[bool, bool]:
        return (
            await agent_allows(
                session, ORG, NEIGHBOUR, "invoices:approve", groups=[FINANCE]
            ),
            await agent_allows(
                session, OTHER_ORG, NEIGHBOUR, "invoices:approve", groups=[FINANCE]
            ),
        )

    assert world.run(check) == (True, False)


def test_agent_permissions_are_apart_from_the_sites(world: World) -> None:
    # An agent permission named like a site permission is the organization's own.
    permission(world, "api_keys:manage", [FINANCE])
    assert my_access(world, VIEWER, [FINANCE])["permissions"] == ["api_keys:manage"]
    # It grants nothing on the site's side.
    assert (
        world.call(
            "GET",
            f"/organizations/{ORG}/api-keys",
            None,
            headers=with_groups(world, VIEWER, [FINANCE]),
        ).status_code
        == 403
    )


# ------------------------------------------------------------ company groups


def test_unlinking_and_relinking_groups(world: World) -> None:
    approve = permission(world, "invoices:approve", [FINANCE, "Auditors"])
    permission(world, "reports:read", ["Auditors"])
    assert link(world, approve, groups=[FINANCE])["groups"] == [FINANCE]
    names = [
        g["group_name"] for g in world.call("GET", f"{ACCESS}/groups", ADMIN).json()
    ]
    assert names == ["Auditors", FINANCE]

    auditors = next(
        g
        for g in world.call("GET", f"{ACCESS}/groups", ADMIN).json()
        if g["group_name"] == "Auditors"
    )
    assert (
        world.call("DELETE", f"{ACCESS}/groups/{auditors['id']}", ADMIN).status_code
        == 204
    )
    listed = {
        p["key"]: p["groups"]
        for p in world.call("GET", f"{ACCESS}/permissions", ADMIN).json()
    }
    assert listed == {"invoices:approve": [FINANCE], "reports:read": []}
    # A group left linked to nothing is forgotten.
    link(world, approve, groups=[])
    assert world.call("GET", f"{ACCESS}/groups", ADMIN).json() == []


# ------------------------------------------------------------ changes carry over


def test_renaming_and_deleting_carry_over(world: World) -> None:
    approve = permission(world, "invoices:approve", [FINANCE])
    renamed = world.call(
        "PATCH",
        f"{ACCESS}/permissions/{approve}",
        ADMIN,
        {"key": "invoices:sign_off"},
    )
    assert renamed.status_code == 200 and renamed.json()["groups"] == [FINANCE]
    assert my_access(world, MEMBER, [FINANCE])["permissions"] == ["invoices:sign_off"]

    assert (
        world.call("DELETE", f"{ACCESS}/permissions/{approve}", ADMIN).status_code
        == 204
    )
    assert my_access(world, MEMBER, [FINANCE])["permissions"] == []
    assert lines(world) == set()
    assert world.call("GET", f"{ACCESS}/groups", ADMIN).json() == []


def test_deleting_the_organization_takes_its_agent_access(world: World) -> None:
    grant_role(world, ADMIN, "site:admin", "*")
    permission(world, "invoices:approve", [FINANCE])
    assert lines(world)

    assert world.call("DELETE", f"/organizations/{ORG}", ADMIN).status_code == 204
    assert lines(world) == set()


def test_access_reads_a_subjects_groups(world: World) -> None:
    permission(world, "invoices:*", [FINANCE])
    permission(world, "reports:read", ["Auditors"])

    async def read_access(session: Any) -> Any:
        return await agent_access(session, ORG, MEMBER, groups=[FINANCE, "Auditors"])

    assert world.run(read_access).permissions == ["invoices:*", "reports:read"]

    async def allowed(session: Any) -> list[bool]:
        return [
            await agent_allows(session, ORG, MEMBER, "invoices:pay", [FINANCE]),
            await agent_allows(session, ORG, MEMBER, "reports:read", [FINANCE]),
            await agent_allows(session, ORG, MEMBER, "invoices:pay"),
        ]

    assert world.run(allowed) == [True, False, False]
