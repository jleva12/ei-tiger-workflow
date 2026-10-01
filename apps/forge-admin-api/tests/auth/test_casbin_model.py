"""The shared Casbin model and the scope helpers, evaluated in memory."""

import asyncio

import casbin
import pytest

from forge_admin.auth.access import SITE, Level, Scope, assignment_pattern
from forge_admin.auth.authorization import new_enforcer

ORG = "org:00000000-0000-4000-8000-00000000000a"
OTHER_ORG = "org:00000000-0000-4000-8000-00000000000d"


def enforcer() -> casbin.AsyncEnforcer:
    e = new_enforcer()

    async def seed() -> None:
        await e.add_policy("site:admin", "*", "*")
        await e.add_policy("org:viewer", "organizations", "read")
        await e.add_policy("org:admin", "organizations", "update")
        await e.add_policy("org:member", "organizations", "read")
        await e.add_policy("org:member", "workflows", "run")
        await e.add_grouping_policy("root", "site:admin", assignment_pattern("site"))
        await e.add_grouping_policy("olga", "org:viewer", assignment_pattern(ORG))
        await e.add_grouping_policy("oscar", "org:admin", assignment_pattern(ORG))
        await e.add_grouping_policy("mo", "org:member", assignment_pattern(ORG))
        await e.add_grouping_policy("mo", "org:viewer", assignment_pattern(OTHER_ORG))

    asyncio.run(seed())
    return e


@pytest.mark.parametrize(
    ("subject", "domain", "permission", "allowed"),
    [
        # A role applies in the organization it's held in...
        ("oscar", ORG, "organizations:update", True),
        ("olga", ORG, "organizations:read", True),
        ("mo", ORG, "workflows:run", True),
        ("mo", OTHER_ORG, "organizations:read", True),
        # ...but not in another one, nor on the site.
        ("oscar", OTHER_ORG, "organizations:update", False),
        ("olga", OTHER_ORG, "organizations:read", False),
        ("mo", OTHER_ORG, "workflows:run", False),
        ("oscar", "site", "organizations:update", False),
        # Only what the role grants.
        ("mo", ORG, "organizations:update", False),
        ("olga", ORG, "organizations:update", False),
        # The site covers everything.
        ("root", OTHER_ORG, "anything:delete", True),
        ("root", "site", "organizations:create", True),
        ("nobody", ORG, "organizations:read", False),
    ],
)
def test_scoped_grants(
    subject: str, domain: str, permission: str, allowed: bool
) -> None:
    resource, action = permission.split(":")
    assert enforcer().enforce(subject, domain, resource, action) is allowed


def test_roles_are_those_held_in_the_organization_or_on_the_site() -> None:
    e = enforcer()
    assert asyncio.run(e.get_implicit_roles_for_user("oscar", ORG)) == ["org:admin"]
    assert asyncio.run(e.get_implicit_roles_for_user("mo", OTHER_ORG)) == ["org:viewer"]
    assert asyncio.run(e.get_implicit_roles_for_user("root", ORG)) == ["site:admin"]
    assert asyncio.run(e.get_implicit_roles_for_user("oscar", OTHER_ORG)) == []


def test_scope_parsing_and_patterns() -> None:
    organization = Scope.parse("org:00000000-0000-4000-8000-00000000000a")
    assert organization == Scope(Level.ORG, "00000000-0000-4000-8000-00000000000a")
    assert str(organization) == ORG
    assert Scope.parse("site") is SITE
    assert Scope.from_pattern(assignment_pattern(ORG)) == organization
    assert Scope.from_pattern("*") is SITE
    assert assignment_pattern("site") == "*"
    for bad in (
        "org",
        "org:not-a-uuid",
        "team:00000000-0000-4000-8000-00000000000c",
        "app:00000000-0000-4000-8000-0000000000a1",
        "tenant:00000000-0000-4000-8000-00000000000c",
    ):
        with pytest.raises(ValueError):
            Scope.parse(bad)
