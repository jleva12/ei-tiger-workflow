"""Chat pages: a published agent shared at a link, which is the credential.
It answers whether or not the runtime is public, only for its agent's latest
published version, and only into visitors' conversations."""

import json
from pathlib import Path
from typing import Any

import pytest
from key_world import AGENT, MEMBER, ORG, OTHER_AGENT, RUNTIME, World, agent_document

from forge_admin.chat_agents.store import new_page_token

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
    folder = tmp_path / (
        "public" if world.client.app.state.settings.agent_runtime_public else "private"
    )  # type: ignore[attr-defined]
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


def turn(app: str, user: str) -> dict[str, Any]:
    return {
        "appName": app,
        "userId": user,
        "sessionId": "s1",
        "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
    }


@pytest.fixture
def private(make_world: Any) -> World:
    world: World = make_world(public=False)
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
        # Its agents' names, which the chat labels replies and hand-offs with.
        "agents": {"support_assistant": "Support assistant"},
        # Its workflow tools' names: the chat shows a call of one as its run.
        "workflows": {"triage_ticket": "Triage ticket"},
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


def test_a_page_answers_when_the_runtime_is_private(
    private: World, tmp_path: Path
) -> None:
    shared(private, tmp_path)
    sessions = f"/apps/{AGENT}/users/{VISITOR}/sessions"
    # The runtime itself wants a caller...
    refused = private.call("POST", sessions, None, {}, prefix=RUNTIME)
    assert refused.status_code == 401
    # ...the page's link is one, for its agent.
    assert page(private, "GET", f"/apps/{AGENT}").status_code == 200
    made = page(private, "POST", sessions, {})
    assert made.status_code == 201, made.text
    listed = page(private, "GET", sessions)
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
    for user in (MEMBER, "apikey:abc", "customer-1", "visitor_short"):
        sessions = f"/apps/{AGENT}/users/{user}/sessions"
        assert page(private, "GET", sessions).status_code == 403, user
        assert (
            page(private, "POST", "/run_sse", turn(AGENT, user)).status_code == 403
        ), user


def test_a_visitors_turn_is_let_through(world: World, tmp_path: Path) -> None:
    shared(world, tmp_path)
    # Past the page's checks: no such conversation (it's made first).
    missing = page(world, "POST", "/run_sse", turn(AGENT, VISITOR))
    assert missing.status_code == 404
    assert missing.json()["detail"] == "No such conversation"
