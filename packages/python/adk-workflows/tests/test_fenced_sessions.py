"""A run's ADK sessions as one attempt of it writes them (fenced_sessions.py):
each event lands while the attempt holds its run, and none once it lost it."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from google.adk import Event
from google.adk.sessions import InMemorySessionService, Session
from google.genai import types

from forge_task_adk_workflows.fenced_sessions import FencedSessions
from forge_task_adk_workflows.run_store import LostLease

APP, USER, SESSION = "adk_workflows", "user-alice", "session-1"


class Lease:
    """The attempt's hold on its run, as the worker's control gives it (``holding``)."""

    def __init__(self) -> None:
        self.lost = False
        self.taken = 0
        self.open = 0

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        if self.lost:
            raise LostLease("run r1 is no longer w1's, or its lease ran out")
        self.taken += 1
        self.open += 1
        try:
            yield
        finally:
            self.open -= 1


class Stored(InMemorySessionService):
    """Where the sessions are: says whether the run was held as each event landed."""

    def __init__(self, lease: Lease) -> None:
        super().__init__()
        self.lease = lease
        self.held: list[bool] = []

    async def append_event(self, session: Session, event: Event) -> Event:
        self.held.append(self.lease.open > 0)
        return await super().append_event(session, event)


def said(text: str, *, partial: bool = False) -> Event:
    return Event(
        invocation_id="r1",
        author="step",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        partial=partial,
    )


async def opened() -> tuple[FencedSessions, Stored, Lease, Session]:
    lease = Lease()
    stored = Stored(lease)
    sessions = FencedSessions(stored, lease)
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id=SESSION)
    return sessions, stored, lease, session


async def texts(sessions: FencedSessions) -> list[str]:
    found = await sessions.get_session(app_name=APP, user_id=USER, session_id=SESSION)
    assert found is not None
    return [part.text or "" for event in found.events if event.content for part in event.content.parts or []]


async def test_each_event_lands_while_the_attempt_holds_its_run() -> None:
    sessions, stored, lease, session = await opened()
    await sessions.append_event(session, said("one"))
    await sessions.append_event(session, said("two"))
    assert stored.held == [True, True]
    assert (lease.taken, lease.open) == (2, 0)  # held for each, and let go after
    assert await texts(sessions) == ["one", "two"]


async def test_an_attempt_that_lost_its_run_appends_nothing() -> None:
    sessions, stored, lease, session = await opened()
    await sessions.append_event(session, said("one"))
    lease.lost = True  # another worker took the run
    with pytest.raises(LostLease):
        await sessions.append_event(session, said("too late"))
    assert stored.held == [True]
    assert await texts(sessions) == ["one"]


async def test_reading_and_partial_events_dont_hold_the_run() -> None:
    sessions, stored, lease, session = await opened()
    lease.lost = True
    await sessions.append_event(session, said("stream", partial=True))  # kept in memory only
    assert await sessions.get_session(app_name=APP, user_id=USER, session_id=SESSION) is not None
    assert [s.id for s in (await sessions.list_sessions(app_name=APP, user_id=USER)).sessions] == [SESSION]
    await sessions.flush()
    assert lease.taken == 0
    assert await texts(sessions) == []
