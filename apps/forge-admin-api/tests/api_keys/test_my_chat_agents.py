"""The agents the desktop app offers a person (``GET /me/chat-agents``): the
published chat agents of the organizations they're a member of, as their
latest published versions have them."""

from typing import Any

from key_world import AGENT, MEMBER, NEIGHBOUR, ORG, OTHER_AGENT, OTHER_ORG, World

#: An agent of Acme's that was never published.
DRAFT = "ca_acmedraft01"


class Agents:
    """The chat agents' store, as far as the listing reads it: Acme's helper
    published as version 2 and a draft beside it, Globex's helper as version 3."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {
            ORG: [
                {"_id": AGENT, "organization_id": ORG, "published_version": 2},
                {"_id": DRAFT, "organization_id": ORG, "published_version": None},
            ],
            OTHER_ORG: [
                {
                    "_id": OTHER_AGENT,
                    "organization_id": OTHER_ORG,
                    "published_version": 3,
                }
            ],
        }

    async def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.records.get(organization_id, [])

    async def aclose(self) -> None:
        pass


def offered(world: World, user: str | None) -> Any:
    world.client.app.state.chat_agents = Agents()  # type: ignore[attr-defined]
    return world.call("GET", "/me/chat-agents", user)


def test_a_member_is_offered_their_organizations_published_agents(world: World) -> None:
    got = offered(world, MEMBER)
    assert got.status_code == 200, got.text
    (agent,) = got.json()
    # Acme's published helper; not its draft, nor Globex's.
    assert {
        key: agent[key] for key in ("id", "organization_id", "organization_name")
    } == {
        "id": AGENT,
        "organization_id": ORG,
        "organization_name": "Acme",
    }
    assert (agent["name"], agent["version"]) == ("Acme helper", 2)
    # Its agents' names, which the chat labels replies with.
    assert agent["agents"] and all(isinstance(v, str) for v in agent["agents"].values())
    assert agent["workflows"] == {}
    # The model it starts on: the context meter's window before the first reply.
    assert agent["model"] == {
        "id": "openai/gpt-test",
        "name": "gpt-test",
        "context_window": 128000,
        "max_tokens": 16384,
    }


def test_each_person_is_offered_only_their_own_organizations(world: World) -> None:
    got = offered(world, NEIGHBOUR)
    assert got.status_code == 200, got.text
    assert [(a["id"], a["organization_name"], a["version"]) for a in got.json()] == [
        (OTHER_AGENT, "Globex", 3)
    ]


def test_it_needs_a_sign_in(world: World) -> None:
    assert offered(world, None).status_code == 401


def test_without_chat_agents_it_isnt_set_up(world: World) -> None:
    world.client.app.state.chat_agents = None  # type: ignore[attr-defined]
    assert world.call("GET", "/me/chat-agents", MEMBER).status_code == 503
