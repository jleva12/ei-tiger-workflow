"""
A conversation's title, kept in its ADK session's state (``forge:title``), so
the chats that list a person's conversations can show what each was about.

ADK sessions have no title of their own, and a chat's thread list keeps the
name it gives a new conversation only in the browser. So the server names it
after its first message, as part of that turn: the title rides in the
turn's own state change (``with_title``), applied with the person's message.
It's never written on its own while a turn runs: ADK refuses the runner's
next write to a session another writer has moved on (``StaleSessionError``),
which would fail the turn.

A conversation from before titles is named the next time it's used, after
its first message still.
"""

from collections.abc import Mapping
from typing import Any

from google.adk.sessions import Session
from google.genai import types

#: Where a conversation keeps its title.
TITLE_KEY = "forge:title"
#: The longest title, as the chats name a conversation (``titleFrom``).
MOST = 60


def title_from(text: str) -> str:
    """:return: A title for a conversation that starts with ``text``: its
    first line, cut to ``MOST`` characters."""
    line = text.strip().split("\n", 1)[0].strip()
    return line if len(line) <= MOST else line[: MOST - 3].rstrip() + "…"


def text_of(content: types.Content | None) -> str:
    """:return: What a message says: its text parts, not its reasoning or files."""
    parts = (content.parts if content else None) or ()
    return "\n".join(part.text for part in parts if part.text and not part.thought).strip()


def with_title(
    state_delta: Mapping[str, Any] | None,
    session: Session | None,
    message: types.Content | None,
) -> dict[str, Any] | None:
    """
    :param state_delta: The state the turn sets.
    :param session: The conversation, with its events when it has a past.
    :param message: The person's message starting the turn.
    :return: The turn's state change, naming a conversation that has no
        title yet after its first message (the session's first one with
        text, or this one); as it was otherwise.
    """
    if session is None or session.state.get(TITLE_KEY):
        return dict(state_delta) if state_delta is not None else None
    first = next(
        (
            text_of(event.content)
            for event in session.events
            if event.author == "user" and text_of(event.content)
        ),
        "",
    )
    title = title_from(first or text_of(message))
    if not title:
        return dict(state_delta) if state_delta is not None else None
    return {**(state_delta or {}), TITLE_KEY: title}
