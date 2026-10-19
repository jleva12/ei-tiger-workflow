"""Helpers for an organization's agent access in the API-keys world
(key_world): its agent permissions, linked to company groups and API keys,
sign-ins naming company groups, and someone who builds Acme's agents
(``agents:manage``), who may use them all."""

from datetime import timedelta
from typing import Any

import jwt
from key_world import ADMIN, ORG, World

from forge_admin.auth.tokens import mint_subject_token
from forge_admin.models import CasbinRule, Permission, Role

ACCESS = f"/organizations/{ORG}/agent-access"
FINANCE = "CN=Finance,OU=Groups"
BUILDER = "builder-1"


def add_builder(world: World) -> None:
    """Make BUILDER someone who builds Acme's agents, signed in."""

    async def builder(session: Any) -> None:
        session.add(Permission(resource="agents", action="manage"))
        session.add(Role(key="org:builder", name="Builder"))
        # As the organization's admins and members do: they run its agents too.
        for resource, action in (
            ("agents", "manage"),
            ("agents", "run"),
            ("organizations", "read"),
        ):
            session.add(CasbinRule(ptype="p", v0="org:builder", v1=resource, v2=action))
        session.add(
            CasbinRule(ptype="g", v0=BUILDER, v1="org:builder", v2=f"org:{ORG}*")
        )
        await session.commit()

    world.run(builder)
    settings = world.client.app.state.settings  # type: ignore[attr-defined]
    world.tokens[BUILDER] = mint_subject_token(
        settings, BUILDER, lifetime=timedelta(minutes=5)
    )


def limited_to(*permission_ids: int) -> dict[str, Any]:
    """A record's ``access``: those holding one of these may use it."""
    return {
        "permission_ids": list(permission_ids),
        "updated_at": "2026-10-15T00:00:00Z",
        "updated_by": ADMIN,
        "updated_by_name": None,
    }


def permission(world: World, key: str, groups: list[str] | None = None) -> int:
    """One of Acme's agent permissions, made by its admin: its ID."""
    made = world.call(
        "POST", f"{ACCESS}/permissions", ADMIN, {"key": key, "groups": groups or []}
    )
    assert made.status_code == 201, made.text
    return int(made.json()["id"])


def link(
    world: World,
    permission_id: int,
    *,
    groups: list[str] | None = None,
    api_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Link one of Acme's agent permissions to exactly these groups or keys."""
    body: dict[str, Any] = {}
    if groups is not None:
        body["groups"] = groups
    if api_keys is not None:
        body["api_keys"] = api_keys
    linked = world.call("PATCH", f"{ACCESS}/permissions/{permission_id}", ADMIN, body)
    assert linked.status_code == 200, linked.text
    return dict(linked.json())


def with_groups(world: World, user: str, groups: list[str]) -> dict[str, str]:
    """Headers signing someone in whose sign-in names these company groups."""
    settings = world.client.app.state.settings  # type: ignore[attr-defined]
    claims = jwt.decode(world.tokens[user], options={"verify_signature": False})
    token = jwt.encode(
        {**claims, settings.jwt_groups_claim: groups},
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}
