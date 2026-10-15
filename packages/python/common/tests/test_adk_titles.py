"""A conversation's title (``forge_common.adk.titles``): named after its first
message, with the turn that message starts, and kept from then on."""

from google.adk.events import Event
from google.adk.sessions import Session
from google.genai import types

from forge_common.adk.titles import TITLE_KEY, title_from, with_title


def said(text: str | None = None, *, thought: str | None = None) -> types.Content:
    parts = []
    if thought:
        parts.append(types.Part(text=thought, thought=True))
    if text is not None:
        parts.append(types.Part(text=text))
    return types.Content(role="user", parts=parts)


def conversation(*events: Event, **state: object) -> Session:
    return Session(id="s1", app_name="ca_x", user_id="ada", state=dict(state), events=list(events))


def test_a_title_is_the_first_line_cut_as_the_chats_cut_it() -> None:
    assert title_from("  Where's my order?\nIt's A-1042.") == "Where's my order?"
    long = "Please ship order A-1042 to the Lisbon store as soon as you possibly can"
    assert title_from(long) == long[:57].rstrip() + "…"
    assert len(title_from(long)) == 58
    assert title_from("   ") == ""


def test_a_new_conversation_is_named_after_its_first_message_with_the_turn() -> None:
    delta = with_title({"model": "m"}, conversation(), said("Ship A-1042"))
    assert delta == {"model": "m", TITLE_KEY: "Ship A-1042"}
    # A turn setting no state of its own sets the title alone.
    assert with_title(None, conversation(), said("Hi")) == {TITLE_KEY: "Hi"}


def test_a_titled_conversation_keeps_its_title() -> None:
    titled = conversation(**{TITLE_KEY: "Ship A-1042"})
    assert with_title({"model": "m"}, titled, said("And B-7731?")) == {"model": "m"}
    assert with_title(None, titled, said("And B-7731?")) is None


def test_an_older_conversation_is_named_after_its_own_first_message() -> None:
    older = conversation(
        Event(author="user", content=said(thought="hm")),
        Event(author="user", content=said("Where are the stores?")),
        Event(
            author="agent", content=types.Content(role="model", parts=[types.Part(text="Lisbon…")])
        ),
    )
    assert with_title(None, older, said("And Porto?")) == {TITLE_KEY: "Where are the stores?"}


def test_a_message_without_text_names_nothing_yet() -> None:
    files = types.Content(
        role="user", parts=[types.Part(inline_data=types.Blob(mime_type="image/png", data=b"x"))]
    )
    assert with_title({"model": "m"}, conversation(), files) == {"model": "m"}
    assert with_title(None, conversation(), None) is None
    assert with_title(None, None, said("Hi")) is None
