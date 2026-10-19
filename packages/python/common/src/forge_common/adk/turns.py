"""A turn's outcome shared by execution, its transport, and its audit record.

Model messages can finish before the invocation does (another agent, a tool,
or follow-up generation can still be working). Only the owner of the turn
settles it. An error event is a failure even when ADK does not raise it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

import anyio
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions import BaseSessionService

TURN_KEY = "forge:turn"
PERSIST_TIMEOUT = 5.0
TurnStatus = Literal["running", "succeeded", "failed", "cancelled"]
log = logging.getLogger(__name__)


@dataclass
class TurnOutcome:
    """One invocation, including its terminal error, rather than one message."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TurnStatus = "running"
    error_code: str | None = None
    error_message: str | None = None
    invocation_id: str | None = None
    native_failure: bool = field(default=False, init=False)
    _persisted: TurnStatus | None = field(default=None, init=False, repr=False)
    _persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def observe(self, event: Event) -> None:
        if event.invocation_id and self.invocation_id is None:
            self.invocation_id = event.invocation_id
        if event.error_code or event.error_message:
            self.native_failure = True
            self.fail(
                event.error_code or "AGENT_ERROR",
                event.error_message or "The agent couldn't finish its reply.",
            )
        elif event.interrupted:
            self.cancel()

    def fail(self, code: str, message: str, *, replace: bool = False) -> None:
        # A later exception or cancellation must not erase the original error.
        if self.status != "failed" or replace:
            self.status = "failed"
            self.error_code, self.error_message = code, message

    def cancel(self) -> None:
        if self.status != "failed":
            self.status = "cancelled"

    def succeed(self) -> None:
        if self.status == "running":
            self.status = "succeeded"

    def event(self, author: str, *, started: bool = False) -> Event:
        """An ADK-compatible control event; it adds no assistant message."""
        payload: dict[str, object] = {
            "version": 1,
            "runId": self.run_id,
            "status": "running" if started else self.status,
        }
        if not started and self.error_code:
            payload["errorCode"] = self.error_code
            payload["errorMessage"] = self.error_message
        return Event(
            author=author,
            invocation_id=self.invocation_id or "",
            custom_metadata={TURN_KEY: payload},
        )


def _persistence_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        try:
            task.result()
        except Exception:
            log.warning("Couldn't save a turn's terminal outcome", exc_info=True)


async def persist_turn(
    outcome: TurnOutcome,
    *,
    sessions: BaseSessionService,
    app_name: str,
    user_id: str,
    session_id: str,
    author: str,
    invocation_id: str | None = None,
) -> None:
    """Save otherwise transient failure/cancellation, with bounded cleanup.

    The outermost execution/transport owner calls this after settling its
    outcome. Native errors remain untouched. Control events contain no
    prompt-visible message or state change, and successful turns add none.
    """
    if outcome.status not in ("failed", "cancelled"):
        return

    async def write() -> None:
        async with outcome._persist_lock:
            if outcome._persisted is not None:
                return
            session = await sessions.get_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
            if session is None:
                return
            # Never infer from the last session event: it may belong to an
            # earlier turn, or a newer concurrent one that this turn must not alter.
            correlated = outcome.invocation_id or invocation_id or outcome.run_id
            for event in session.events:
                terminal = (event.custom_metadata or {}).get(TURN_KEY)
                if (
                    isinstance(terminal, dict)
                    and terminal.get("runId") == outcome.run_id
                    and terminal.get("status") in ("failed", "cancelled")
                ):
                    outcome._persisted = outcome.status
                    return
            event = outcome.event(author)
            event.invocation_id = correlated
            await sessions.append_event(session=session, event=event)
            outcome._persisted = outcome.status

    cancelled: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PERSIST_TIMEOUT
    # An independent task survives direct asyncio cancellation; the shield
    # handles AnyIO's repeated cancellation when a Starlette client disconnects.
    with anyio.CancelScope(shield=True):
        writing = asyncio.create_task(write())
        while not writing.done():
            try:
                done, _ = await asyncio.wait({writing}, timeout=max(0.0, deadline - loop.time()))
            except asyncio.CancelledError as error:
                cancelled = error
                continue
            if not done:
                writing.cancel()
                writing.add_done_callback(_persistence_done)
                log.warning("Saving turn %s exceeded %.1fs", outcome.run_id, PERSIST_TIMEOUT)
                break
        else:
            _persistence_done(writing)
    if cancelled is not None:
        raise cancelled
    await anyio.lowlevel.checkpoint_if_cancelled()


_turn: ContextVar[TurnOutcome | None] = ContextVar("forge_turn", default=None)


def current_turn() -> TurnOutcome | None:
    return _turn.get()


class TurnOutcomePlugin(BasePlugin):
    """Bind ADK's invocation before a turn can stop without model output."""

    def __init__(self) -> None:
        super().__init__(name="forge_turn_outcome")

    async def before_run_callback(self, *, invocation_context: InvocationContext) -> None:
        if (outcome := current_turn()) is not None and outcome.invocation_id is None:
            # Nested agents share the turn, but must not replace its owner's ID.
            outcome.invocation_id = invocation_context.invocation_id


@contextmanager
def turn_scope(outcome: TurnOutcome | None = None) -> Iterator[TurnOutcome]:
    """Nested execution uses the transport's outcome and correlation ID."""
    outcome = outcome or current_turn() or TurnOutcome()
    token = _turn.set(outcome)
    try:
        yield outcome
    finally:
        _turn.reset(token)
