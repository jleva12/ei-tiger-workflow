"""One turn at a time in each conversation.

A conversation (an agent's session for a person) runs one turn at a time:
two at once would each write to the session under the other (the second's
first write fails as stale, after its tools already ran), and their tools'
writes to what the conversation keeps (an intake's record, its requests)
would overwrite each other. So a turn first takes the conversation's
**lease**, before anything of it runs, and gives it back once it's over,
its outcome kept. Another turn asking for it meanwhile waits a moment
(``turn_wait``), then is refused (:class:`TurnBusy`).

A lease runs out unless it's renewed: the turn holding it renews it while
it runs, so a process that stops mid-turn frees the conversation once its
lease runs out. A turn whose lease ran out and went to another
(:attr:`TurnLease.lost`) stops before it writes anything more: its next
event isn't kept, its next model or tool call isn't made
(:class:`TurnLeasePlugin`).

Where leases are kept is a :class:`TurnLeases`: this process
(:class:`MemoryTurnLeases`, the default), or a database every process of a
deployment shares (the hosted runtime's admin MySQL), so a conversation's
turns are one at a time across them all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from google.adk.plugins.base_plugin import BasePlugin

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event
    from google.adk.models.llm_request import LlmRequest
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext

log = logging.getLogger(__name__)

#: Seconds a lease lasts unless it's renewed; the turn holding it renews it
#: three times as often.
LEASE_SECONDS = 30.0

#: Seconds a turn waits for the conversation's lease before it's refused:
#: long enough for the turn before it to finish keeping its outcome.
TURN_WAIT = 5.0


@dataclass(frozen=True)
class TurnKey:
    """A conversation: the agent (its ID, whichever version), the person and the session."""

    app_name: str
    user_id: str
    session_id: str


class TurnBusy(RuntimeError):
    """Another turn is running in the conversation: try again once it's over."""

    def __init__(self, key: TurnKey) -> None:
        super().__init__("Another turn is still running in this conversation: try again once it's over.")
        self.key = key


class TurnLeaseLost(RuntimeError):
    """The turn's lease ran out and another turn took the conversation: this one stops."""


class TurnLeases(Protocol):
    """Where conversations' leases are kept."""

    async def acquire(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        """
        :return: Whether ``owner`` has the conversation's lease now, for
            ``seconds``: nobody had it, its lease ran out, or it's theirs.
        """
        ...

    async def renew(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        """:return: Whether ``owner`` still had the lease, now renewed for ``seconds``."""
        ...

    async def release(self, key: TurnKey, *, owner: str) -> None:
        """Gives the lease back, if ``owner`` still has it."""
        ...


class MemoryTurnLeases:
    """Leases kept in this process: turns are one at a time within it only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._held: dict[TurnKey, tuple[str, float]] = {}

    async def acquire(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        now = self.clock()
        held = self._held.get(key)
        if held is not None and held[0] != owner and held[1] > now:
            return False
        self._held[key] = (owner, now + seconds)
        return True

    async def renew(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        held = self._held.get(key)
        if held is None or held[0] != owner:
            return False
        self._held[key] = (owner, self.clock() + seconds)
        return True

    async def release(self, key: TurnKey, *, owner: str) -> None:
        held = self._held.get(key)
        if held is not None and held[0] == owner:
            del self._held[key]


@dataclass(eq=False)
class TurnLease:
    """A conversation's lease, held by a turn: renewed while it's held, until it's released."""

    key: TurnKey
    owner: str
    leases: TurnLeases
    seconds: float
    #: The lease ran out and another turn took the conversation.
    lost: bool = False
    _renewing: asyncio.Task[None] | None = field(default=None, repr=False)
    _released: bool = field(default=False, repr=False)
    _holder: weakref.ref[Any] | None = field(default=None, repr=False)

    @classmethod
    async def take(
        cls,
        leases: TurnLeases,
        key: TurnKey,
        *,
        seconds: float = LEASE_SECONDS,
        wait: float = TURN_WAIT,
    ) -> TurnLease:
        """
        :return: The conversation's lease, once it's free (waiting at most
            ``wait`` seconds), renewing itself until released.
        :raises TurnBusy: Another turn still has it.
        """
        owner = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait)
        delay = 0.05
        while not await leases.acquire(key, owner=owner, seconds=seconds):
            left = deadline - loop.time()
            if left <= 0:
                raise TurnBusy(key)
            await asyncio.sleep(min(delay, left))
            delay = min(delay * 2, 0.5)
        lease = cls(key, owner, leases, seconds)
        lease._renewing = asyncio.create_task(lease._renew(), name=f"turn-lease-{key.session_id}")
        return lease

    def held_by(self, holder: object) -> None:
        """
        Gives the lease back should ``holder`` be dropped without giving it
        back itself: a response whose stream never started (its client went
        first), so its ``finally`` never ran.
        """
        loop = asyncio.get_running_loop()

        def gone(_: object) -> None:
            if not self._released and not loop.is_closed():
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.release()))

        self._holder = weakref.ref(holder, gone)

    def check(self) -> None:
        """:raises TurnLeaseLost: The lease went to another turn: this one mustn't carry on."""
        if self.lost:
            raise TurnLeaseLost(
                "This turn took too long to answer and another took the conversation: it stopped."
            )

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self.seconds / 3)
            try:
                if not await self.leases.renew(self.key, owner=self.owner, seconds=self.seconds):
                    log.warning("Turn lease of session %s went to another turn", self.key.session_id)
                    self.lost = True
                    return
            except Exception:
                # Kept until it runs out: renewing again may still catch it.
                log.warning("Couldn't renew the turn lease of session %s", self.key.session_id, exc_info=True)

    async def release(self) -> None:
        """Gives the lease back (once; again does nothing), even while its caller is being cancelled."""
        if self._released:
            return
        self._released = True
        if self._renewing is not None:
            self._renewing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._renewing

        async def give_back() -> None:
            try:
                await self.leases.release(self.key, owner=self.owner)
            except Exception:
                # It runs out on its own.
                log.warning(
                    "Couldn't give back the turn lease of session %s", self.key.session_id, exc_info=True
                )

        # Its own task, so a cancelled caller still frees the conversation.
        await asyncio.shield(asyncio.ensure_future(give_back()))


_held: ContextVar[TurnLease | None] = ContextVar("forge_turn_lease", default=None)


@contextlib.contextmanager
def holding(lease: TurnLease) -> Iterator[None]:
    """Runs a turn's steps as the turn holding ``lease`` (:class:`TurnLeasePlugin` checks it)."""
    token = _held.set(lease)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            _held.reset(token)


def current_lease() -> TurnLease | None:
    """:return: The lease of the turn running here; None outside one."""
    return _held.get()


class TurnLeasePlugin(BasePlugin):
    """
    Stops a turn whose conversation's lease went to another turn before it
    writes anything more: each event is checked before it's kept, each
    model and tool call before it's made (:meth:`TurnLease.check`).
    """

    def __init__(self) -> None:
        super().__init__(name="forge_turn_lease")

    @staticmethod
    def _check() -> None:
        lease = _held.get()
        if lease is not None:
            lease.check()

    async def on_event_callback(self, *, invocation_context: InvocationContext, event: Event) -> Event | None:
        self._check()
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Any:
        self._check()
        return None

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        self._check()
        return None
