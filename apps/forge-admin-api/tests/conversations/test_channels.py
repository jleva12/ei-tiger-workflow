"""How a hosted runtime turn's channel is told (``chat_agents.runtime.authorize_agent``):
A2A, the builder's chat (a signed-in person, with Forge's console header), or
the run API (anyone else, or without the header)."""

import asyncio
import contextvars
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from forge_admin.auth.runtime_access import RuntimeCaller
from forge_admin.chat_agents import runtime
from forge_admin.overview.recording import CLIENT_HEADER, current_channel


def request(console: bool) -> Request:
    headers = [(CLIENT_HEADER.lower().encode(), b"console")] if console else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


@pytest.mark.parametrize(
    ("action", "caller", "console", "channel"),
    [
        ("a2a", None, False, "a2a"),
        ("a2a", "member-1", True, "a2a"),
        ("run", "member-1", True, "console"),
        ("run", "member-1", False, "api"),
        ("run", "apikey:k-1", True, "api"),
        ("run", None, True, "api"),
        ("card", "member-1", True, None),
    ],
)
def test_a_turns_channel_is_where_it_came_in(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    caller: str | None,
    console: bool,
    channel: str | None,
) -> None:
    # Let in already (admit_agent): who they are is what their credential says.
    monkeypatch.setattr(runtime, "caller_of", lambda _request: RuntimeCaller(caller))

    # Who may use the agent is checked apart (test_agent_gating).
    async def may_use(*_args: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "authorize_use", may_use)
    agent = SimpleNamespace(document=SimpleNamespace(organization_id="org-1"))

    async def told() -> str | None:
        await runtime.authorize_agent(
            request(console), agent, action=action, user_id=None
        )  # type: ignore[arg-type]
        return current_channel()

    assert contextvars.copy_context().run(asyncio.run, told()) == channel
