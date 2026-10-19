"""The assistant calls this API as the person it talks to, with a token that
names the company groups their own sign-in names: what's linked to those
groups (here, an organization's agent permissions) is theirs in those calls
too."""

from typing import Any

from agent_access_world import FINANCE, permission
from key_world import ORG, VIEWER, World

from forge_admin.assistant.person_api import PersonApi
from forge_admin.assistant.user_tokens import UserTokens
from forge_admin.auth.access import set_caller
from forge_admin.auth.tokens import verify_identity

ME = f"/organizations/{ORG}/agent-access/me"


def test_a_token_is_kept_for_each_set_of_groups(world: World) -> None:
    settings = world.client.app.state.settings  # type: ignore[attr-defined]
    tokens = UserTokens(settings)
    plain = tokens.token(VIEWER)
    grouped = tokens.token(VIEWER, [FINANCE, "Auditors"])
    assert verify_identity(settings, plain).groups == ()
    assert set(verify_identity(settings, grouped).groups) == {FINANCE, "Auditors"}
    # The same groups in another order are the same token, reused.
    assert tokens.token(VIEWER, ["Auditors", FINANCE]) == grouped
    assert tokens.token(VIEWER) == plain


def test_the_assistant_calls_as_the_person_with_their_groups(world: World) -> None:
    permission(world, "invoices:approve", [FINANCE])
    api = PersonApi.of_app(world.client.app)  # type: ignore[arg-type]

    async def mine(groups: list[str]) -> Any:
        # As the request the assistant serves would have it: their sign-in.
        set_caller(VIEWER, groups)
        return await api.call(VIEWER, "GET", ME)

    try:
        assert world.run(lambda _: mine([FINANCE]))["permissions"] == [
            "invoices:approve"
        ]
        assert world.run(lambda _: mine([]))["permissions"] == []

        # Someone else's groups are never theirs: only the caller's count.
        async def someone_else() -> Any:
            set_caller("other-1", [FINANCE])
            return await api.call(VIEWER, "GET", ME)

        assert world.run(lambda _: someone_else())["permissions"] == []
    finally:
        world.run(lambda _: api.aclose())
