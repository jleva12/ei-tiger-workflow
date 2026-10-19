"""Chat pages: a published agent shared at a link, which is the credential:
the only way in without a key or a sign-in. It answers only for its agent's
latest published version, and only into its visitors' conversations, each
visitor who the token the API signed for them says."""

import json
from pathlib import Path
from typing import Any

import pytest
from key_world import AGENT, MEMBER, ORG, OTHER_AGENT, RUNTIME, World, agent_document
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.chat_agents.store import new_page_token
from forge_admin.models import Organization

LINK = new_page_token()
PAGE = f"/pages/{LINK}"
VISITOR = "visitor_" + "a1b2c3d4" * 4


class Pages:
    """The chat agents' store, as far as chat pages read it: Acme's agent,
    published as version 2, shared at LINK."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {
            LINK: {
                "_id": AGENT,
                "organization_id": ORG,
                "published_version": 2,
                "chat_page": {
                    "token": LINK,
                    "enabled": True,
                    "suggestions": ["Where's my order?"],
                    "hide_history": True,
                },
            }
        }

    async def find_chat_page(self, token: str) -> dict[str, Any] | None:
        return self.pages.get(token)

    async def aclose(self) -> None:
        pass


def shared(world: World, tmp_path: Path) -> Pages:
    """Acme's agent published as versions 1 and 2, and shared as a page."""
    folder = tmp_path / "agents"
    for version in (1, 2):
        doc = agent_document(AGENT, ORG, f"Acme helper v{version}")
        doc["description"] = "Answers Acme's customers."
        (folder / f"{AGENT}@{version}.json").write_text(
            json.dumps({**doc, "version": version})
        )
    pages = Pages()
    world.client.app.state.chat_agents = pages  # type: ignore[attr-defined]
    return pages


def page(world: World, method: str, path: str, body: Any = None, **kw: Any) -> Any:
    """A call through the page, as its visitors make them: without credentials."""
    return world.call(method, f"{PAGE}{path}", None, body, prefix=RUNTIME, **kw)


def visit(world: World, token: str | None = None) -> tuple[str, dict[str, str]]:
    """A visitor of the page, as it opens: their user ID, and the header its chat sends."""
    headers = {"X-Forge-Visitor": token} if token else None
    made = page(world, "POST", "/visitors", headers=headers)
    assert made.status_code == 200, made.text
    return made.json()["visitor_id"], {"X-Forge-Visitor": made.json()["token"]}


def turn(app: str, user: str) -> dict[str, Any]:
    return {
        "appName": app,
        "userId": user,
        "sessionId": "s1",
        "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
    }


@pytest.fixture
def private(make_world: Any) -> World:
    world: World = make_world()
    return world


def test_a_page_says_what_it_shows(world: World, tmp_path: Path) -> None:
    shared(world, tmp_path)
    # Version 2 calls a workflow too (the page only reads it: it isn't built).
    published = next(tmp_path.glob(f"*/{AGENT}@2.json"))
    doc = json.loads(published.read_text())
    doc["nodes"].append(
        {
            "id": "triage",
            "kind": "adk_workflow",
            "name": "Triage ticket",
            "config": {"workflow": "ag_triage00001"},
            "outputs": [],
        }
    )
    doc["edges"].append(
        {"id": "e1", "source": "agent", "source_output": "tools", "target": "triage"}
    )
    published.write_text(json.dumps(doc))
    found = page(world, "GET", "")
    assert found.status_code == 200, found.text
    assert found.json() == {
        "app_name": AGENT,
        "name": "Acme helper v2",
        "description": "Answers Acme's customers.",
        "version": 2,
        # Offered under the message box while a conversation is empty.
        "suggestions": ["Where's my order?"],
        # What the page leaves out; an older page has both, and follow-ups.
        "hide_history": True,
        "hide_top_bar": False,
        "follow_ups": True,
        # No button opening it in the desktop app until it's turned on.
        "desktop_link": False,
        # Its replies stream until it's set to send them whole.
        "streaming": True,
        # Its agents' names, which the chat labels replies and hand-offs with.
        "agents": {"support_assistant": "Support assistant"},
        # Its workflow tools' names: the chat shows a call of one as its run.
        "workflows": {"triage_ticket": "Triage ticket"},
        # The model its agent starts on (the default: it names none), which the
        # context meter measures against before the first reply.
        "model": {
            "id": "openai/gpt-test",
            "name": "gpt-test",
            "context_window": 128000,
            "max_tokens": 16384,
        },
        # Its words and look, as a page that never set them has them.
        "title": "",
        "welcome_title": "",
        "welcome_message": "",
        "placeholder": "",
        "footer_note": "",
        "theme": "light",
        "theme_toggle": True,
        "accent": None,
        "bubbles": "neutral",
        "corners": "default",
        "font": "default",
        "width": "default",
    }


def test_a_link_that_doesnt_answer_is_not_found(world: World, tmp_path: Path) -> None:
    pages = shared(world, tmp_path)
    other = f"/pages/{new_page_token()}"
    assert world.call("GET", other, None, prefix=RUNTIME).status_code == 404
    assert world.call("GET", "/pages/nope", None, prefix=RUNTIME).status_code == 422
    # Turned off, or given a new link: the old one stops at once.
    pages.pages.clear()
    gone = page(world, "GET", f"/apps/{AGENT}")
    assert gone.status_code == 404
    assert "no chat page" in gone.json()["detail"]


def test_a_page_answers_without_a_key_or_a_sign_in(
    private: World, tmp_path: Path
) -> None:
    shared(private, tmp_path)
    visitor, headers = visit(private)
    sessions = f"/apps/{AGENT}/users/{visitor}/sessions"
    # The runtime itself wants a caller...
    refused = private.call("POST", sessions, None, {}, prefix=RUNTIME)
    assert refused.status_code == 401
    # ...the page's link is one, for its agent, with its visitor's token.
    assert page(private, "GET", f"/apps/{AGENT}").status_code == 200
    made = page(private, "POST", sessions, {}, headers=headers)
    assert made.status_code == 201, made.text
    listed = page(private, "GET", sessions, headers=headers)
    assert [s["id"] for s in listed.json()] == [made.json()["id"]]


def test_a_page_talks_only_to_its_agents_latest_published_version(
    private: World, tmp_path: Path
) -> None:
    shared(private, tmp_path)
    assert page(private, "GET", f"/apps/{AGENT}@2").status_code == 200
    for app in (f"{AGENT}@1", f"{AGENT}@draft", OTHER_AGENT):
        assert page(private, "GET", f"/apps/{app}").status_code == 404, app
    other = page(private, "POST", "/run_sse", turn(OTHER_AGENT, VISITOR))
    assert other.status_code == 404
    older = page(private, "POST", "/run_sse", turn(f"{AGENT}@1", VISITOR))
    assert older.status_code == 404
    assert "latest published" in older.json()["detail"]


def test_a_pages_conversations_are_its_visitors(private: World, tmp_path: Path) -> None:
    shared(private, tmp_path)
    visitor, headers = visit(private)
    assert visitor.startswith("visitor_")
    # Without the token the API signed, nobody's.
    sessions = f"/apps/{AGENT}/users/{visitor}/sessions"
    assert page(private, "GET", sessions).status_code == 401
    assert page(private, "POST", "/run_sse", turn(AGENT, visitor)).status_code == 401
    forged = {"X-Forge-Visitor": headers["X-Forge-Visitor"][:-4] + "AAAA"}
    assert page(private, "GET", sessions, headers=forged).status_code == 401
    elsewhere = headers["X-Forge-Visitor"].replace(AGENT, OTHER_AGENT)
    assert (
        page(
            private, "GET", sessions, headers={"X-Forge-Visitor": elsewhere}
        ).status_code
        == 401
    )
    # With it, only the visitor it says.
    for user in (MEMBER, "apikey:abc", "customer-1", "visitor_short", VISITOR):
        others = f"/apps/{AGENT}/users/{user}/sessions"
        assert page(private, "GET", others, headers=headers).status_code == 403, user
        assert (
            page(
                private, "POST", "/run_sse", turn(AGENT, user), headers=headers
            ).status_code
            == 403
        ), user


def test_visitors_never_reach_each_others_conversations(
    private: World, tmp_path: Path
) -> None:
    shared(private, tmp_path)
    ada, ada_headers = visit(private)
    bob, bob_headers = visit(private)
    assert ada != bob
    made = page(
        private, "POST", f"/apps/{AGENT}/users/{ada}/sessions", {}, headers=ada_headers
    )
    one = f"/apps/{AGENT}/users/{ada}/sessions/{made.json()['id']}"
    # Knowing Ada's visitor ID isn't being her.
    assert page(private, "GET", one, headers=bob_headers).status_code == 403
    assert page(private, "DELETE", one, headers=bob_headers).status_code == 403
    assert page(private, "GET", one, headers=ada_headers).status_code == 200
    # A page sending its token gets a new one, for the same visitor.
    again, renewed = visit(private, ada_headers["X-Forge-Visitor"])
    assert again == ada
    assert page(private, "GET", one, headers=renewed).status_code == 200
    # One that isn't good starts a new visitor.
    fresh, _ = visit(private, "v1.nope")
    assert fresh not in (ada, bob)


def test_a_visitors_turn_is_let_through(world: World, tmp_path: Path) -> None:
    shared(world, tmp_path)
    visitor, headers = visit(world)
    # Past the page's checks: no such conversation (it's made first).
    missing = page(world, "POST", "/run_sse", turn(AGENT, visitor), headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "No such conversation"


def test_page_rejects_orphaned_organization(private: World, tmp_path: Path) -> None:
    shared(private, tmp_path)
    assert page(private, "GET", "").status_code == 200

    async def delete_organization(session: AsyncSession) -> None:
        organization = await session.get(Organization, ORG)
        assert organization is not None
        await session.delete(organization)
        await session.commit()

    private.run(delete_organization)
    assert page(private, "GET", "").status_code == 404
    assert page(private, "POST", "/visitors").status_code == 404
    assert page(private, "GET", f"/apps/{AGENT}").status_code == 404
    sessions = f"/apps/{AGENT}/users/{VISITOR}/sessions"
    assert page(private, "POST", sessions, {}).status_code == 404
    assert page(private, "POST", "/run_sse", turn(AGENT, VISITOR)).status_code == 404
