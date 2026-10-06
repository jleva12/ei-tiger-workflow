"""An organization's API keys: making them (the key shown once), the roles
they may hold, using them as a member with that role would, and deleting
them."""

from datetime import UTC, datetime, timedelta
from typing import Any

from key_world import (
    ADMIN,
    KEYS,
    MEMBER,
    NEIGHBOUR,
    ORG,
    OTHER_ORG,
    VIEWER,
    World,
)
from sqlalchemy import select

from forge_admin.models import ApiKey, CasbinRule

# What organizations:read reads.
ORGANIZATION = f"/organizations/{ORG}"


def g_lines(world: World, subject: str) -> list[tuple[str | None, str | None]]:
    async def read(session: Any) -> list[tuple[str | None, str | None]]:
        rows = await session.execute(
            select(CasbinRule.v1, CasbinRule.v2).where(
                CasbinRule.ptype == "g", CasbinRule.v0 == subject
            )
        )
        return list(rows)

    result: list[tuple[str | None, str | None]] = world.run(read)
    return result


# ------------------------------------------------------------ making keys


def test_an_admin_makes_a_key_and_sees_it_once(world: World) -> None:
    made = world.make_key(name="CI")

    assert made["secret"].startswith("fk_") and len(made["secret"]) == 46
    assert made["hint"] == f"fk_…{made['secret'][-4:]}"
    assert made["role"] == "org:api" and made["role_name"] == "API caller"
    assert made["subject"] == f"apikey:{made['id']}"
    assert made["created_by_name"] == "Admin Person"
    assert made["expires_at"] is None and made["expired"] is False
    assert g_lines(world, made["subject"]) == [("org:api", f"org:{ORG}*")]

    listed = world.call("GET", KEYS, ADMIN).json()
    assert [key["name"] for key in listed] == ["CI"]
    assert "secret" not in listed[0]
    one = world.call("GET", f"{KEYS}/{made['id']}", ADMIN).json()
    assert "secret" not in one and one["hint"] == made["hint"]

    # Only its SHA-256 is kept.
    async def stored(session: Any) -> ApiKey:
        return await session.get(ApiKey, made["id"])

    row = world.run(stored)
    assert made["secret"] not in (row.secret_sha256, row.hint)


def test_members_and_viewers_cant_manage_keys(world: World) -> None:
    for user in (MEMBER, VIEWER, NEIGHBOUR):
        assert world.call("GET", KEYS, user).status_code == 403
        assert world.call("POST", KEYS, user, {"name": "x"}).status_code == 403


def test_a_key_holds_an_organization_role_its_maker_could_give(world: World) -> None:
    assert (
        world.call("POST", KEYS, ADMIN, {"name": "a", "role": "site:admin"}).status_code
        == 422
    )
    assert (
        world.call("POST", KEYS, ADMIN, {"name": "b", "role": "org:nope"}).status_code
        == 422
    )
    viewer = world.make_key(name="Reports", role="org:viewer")
    assert viewer["role"] == "org:viewer"
    assert world.call("POST", KEYS, ADMIN, {"name": "Reports"}).status_code == 409


def test_a_key_cant_be_given_permissions_its_maker_lacks(world: World) -> None:
    keeper = "keeper-1"

    async def make_keeper(session: Any) -> None:
        session.add(
            CasbinRule(ptype="g", v0=keeper, v1="org:keykeeper", v2=f"org:{ORG}*")
        )
        await session.commit()

    world.run(make_keeper)
    from datetime import timedelta as _td

    from forge_admin.auth.tokens import mint_subject_token

    settings = world.client.app.state.settings  # type: ignore[attr-defined]
    world.tokens[keeper] = mint_subject_token(settings, keeper, lifetime=_td(minutes=5))

    refused = world.call("POST", KEYS, keeper, {"name": "x", "role": "org:api"})
    assert refused.status_code == 403
    assert "agents:run" in refused.json()["detail"]
    # The admin's key can't be raised past them by someone who couldn't make it.
    made = world.make_key(name="Viewer key", role="org:viewer")
    assert (
        world.call(
            "PATCH", f"{KEYS}/{made['id']}", keeper, {"role": "org:admin"}
        ).status_code
        == 403
    )


def test_expiry_is_in_the_future_and_within_two_years(world: World) -> None:
    now = datetime.now(UTC)
    past = (now - timedelta(days=1)).isoformat()
    far = (now + timedelta(days=900)).isoformat()
    assert (
        world.call("POST", KEYS, ADMIN, {"name": "a", "expires_at": past}).status_code
        == 422
    )
    assert (
        world.call("POST", KEYS, ADMIN, {"name": "b", "expires_at": far}).status_code
        == 422
    )
    soon = (now + timedelta(days=90)).isoformat()
    made = world.make_key(name="c", expires_at=soon)
    assert made["expires_at"] is not None and made["expired"] is False


# ------------------------------------------------------------ using keys


def test_a_key_is_authorized_as_its_role_through_either_header(world: World) -> None:
    caller = world.make_key(name="Caller")["secret"]
    viewer = world.make_key(name="Viewer", role="org:viewer")["secret"]

    # API caller only runs agents: it doesn't read the organization.
    assert world.call("GET", ORGANIZATION, key=caller).status_code == 403
    assert world.call("GET", ORGANIZATION, key=viewer).status_code == 200
    assert (
        world.call("GET", ORGANIZATION, key=viewer, header="x-api-key").status_code
        == 200
    )
    # Never another organization's.
    other = f"/organizations/{OTHER_ORG}"
    assert world.call("GET", other, key=viewer).status_code == 403


def test_a_wrong_or_mixed_credential_is_refused(world: World) -> None:
    assert world.call("GET", ORGANIZATION, key="fk_" + "x" * 43).status_code == 401
    assert world.call("GET", ORGANIZATION, key="fk_short").status_code == 401
    secret = world.make_key(name="Viewer", role="org:viewer")["secret"]
    mixed = world.client.get(
        f"/api/v1{ORGANIZATION}",
        headers={
            "Authorization": f"Bearer {world.tokens[MEMBER]}",
            "X-API-Key": secret,
        },
    )
    assert mixed.status_code == 401


def test_keys_cant_use_peoples_routes(world: World) -> None:
    secret = world.make_key(name="Admin key", role="org:admin")["secret"]
    assert world.call("GET", KEYS, key=secret).status_code == 403
    assert (
        world.call("POST", KEYS, body={"name": "child"}, key=secret).status_code == 403
    )
    assert world.call("GET", "/users", key=secret).status_code == 403


def test_an_expired_key_stops_working(world: World) -> None:
    made = world.make_key(name="Viewer", role="org:viewer")

    async def expire(session: Any) -> None:
        key = await session.get(ApiKey, made["id"])
        key.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await session.commit()

    world.run(expire)
    refused = world.call("GET", ORGANIZATION, key=made["secret"])
    assert refused.status_code == 401
    assert "expired" in refused.json()["detail"]
    assert world.call("GET", KEYS, ADMIN).json()[0]["expired"] is True


def test_a_key_notes_when_it_was_used_without_changing_who_changed_it(
    world: World,
) -> None:
    made = world.make_key(name="Viewer", role="org:viewer")
    assert made["last_used_at"] is None
    world.call("GET", ORGANIZATION, key=made["secret"])
    used = world.call("GET", f"{KEYS}/{made['id']}", ADMIN).json()
    assert used["last_used_at"] is not None
    assert used["updated_by"] == made["updated_by"] == ADMIN


def test_the_deployment_key_is_satisfied_by_an_organization_key(
    make_world: Any,
) -> None:
    from pydantic import SecretStr

    world = make_world(api_key=SecretStr("deployment-key"))
    # A person needs the deployment's key beside their token...
    assert world.call("GET", ORGANIZATION, ADMIN).status_code == 401
    made = world.client.post(
        f"/api/v1{KEYS}",
        headers={
            "Authorization": f"Bearer {world.tokens[ADMIN]}",
            "X-API-Key": "deployment-key",
        },
        json={"name": "Viewer", "role": "org:viewer"},
    )
    assert made.status_code == 201
    # ...an organization's key stands in for it.
    assert world.call("GET", ORGANIZATION, key=made.json()["secret"]).status_code == 200


# ------------------------------------------------------------ changing keys


def test_renaming_and_changing_a_keys_role(world: World) -> None:
    made = world.make_key(name="Viewer", role="org:viewer")
    changed = world.call(
        "PATCH", f"{KEYS}/{made['id']}", ADMIN, {"name": "Runner", "role": "org:api"}
    )
    assert changed.status_code == 200
    assert changed.json()["name"] == "Runner" and changed.json()["role"] == "org:api"
    assert g_lines(world, made["subject"]) == [("org:api", f"org:{ORG}*")]
    assert world.call("GET", ORGANIZATION, key=made["secret"]).status_code == 403


def test_deleting_a_key_refuses_it_from_the_next_call(world: World) -> None:
    made = world.make_key(name="Viewer", role="org:viewer")
    assert world.call("GET", ORGANIZATION, key=made["secret"]).status_code == 200
    assert world.call("DELETE", f"{KEYS}/{made['id']}", ADMIN).status_code == 204
    assert world.call("GET", ORGANIZATION, key=made["secret"]).status_code == 401
    assert g_lines(world, made["subject"]) == []
    assert world.call("GET", f"{KEYS}/{made['id']}", ADMIN).status_code == 404


def test_keys_arent_members(world: World) -> None:
    made = world.make_key(name="CI")
    members = world.call("GET", f"/scopes/org:{ORG}/members", ADMIN).json()
    assert made["subject"] not in {member["subject_id"] for member in members}
    assert {member["subject_id"] for member in members} == {ADMIN, MEMBER, VIEWER}
    assign = world.call(
        "PUT", f"/scopes/org:{ORG}/members/{made['subject']}/roles/org:admin", ADMIN
    )
    assert assign.status_code == 422
    revoke = world.call(
        "DELETE", f"/scopes/org:{ORG}/members/{made['subject']}/roles/org:api", ADMIN
    )
    assert revoke.status_code == 422
    assert g_lines(world, made["subject"]) == [("org:api", f"org:{ORG}*")]
