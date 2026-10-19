"""
A run's ADK sessions as one attempt of it writes them: each event it appends
lands only while the attempt has the run (its worker's lease on it, held
until the event is in: the control's ``holding``, ``RunStore.holding`` on the
worker), so an attempt whose lease ran out appends nothing more, even while
its worker goes on (its clock behind the others', or it stood still). Once
another attempt took the run, the old one's append would land first and make
the new one's stale: ADK refuses an append to a session that changed since it
was read, so the run would fail.

Everything else is the sessions' own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from google.adk import Event
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.base_session_service import GetSessionConfig, ListSessionsResponse


class FencedSessions(BaseSessionService):
    """
    :param sessions: Where the run's sessions are.
    :param holding: Holds the run for as long as its block (raising, e.g.
        ``LostLease``, when the attempt lost it).
    """

    def __init__(self, sessions: BaseSessionService, holding: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self.sessions = sessions
        self.holding = holding
        # One at a time (ADK appends to a session one at a time anyway), so an
        # attempt holds its run on one connection at most while they land.
        self._appending = asyncio.Lock()

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return await self.sessions.append_event(session, event)  # kept in memory only
        async with self._appending, self.holding():
            return await self.sessions.append_event(session, event)

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        return await self.sessions.create_session(
            app_name=app_name, user_id=user_id, state=state, session_id=session_id
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        return await self.sessions.get_session(app_name=app_name, user_id=user_id, session_id=session_id, config=config)

    async def list_sessions(self, *, app_name: str, user_id: str | None = None) -> ListSessionsResponse:
        return await self.sessions.list_sessions(app_name=app_name, user_id=user_id)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        await self.sessions.delete_session(app_name=app_name, user_id=user_id, session_id=session_id)

    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        return await self.sessions.get_user_state(app_name=app_name, user_id=user_id)

    async def flush(self) -> None:
        await self.sessions.flush()
