from __future__ import annotations

import abc
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol

from event_bus.core.event import Event, EventEnvelope
from event_bus.core.topic import TopicPattern

EventHandler = Callable[[Event], Coroutine[Any, Any, None]]
BatchEventHandler = Callable[[list[Event]], Coroutine[Any, Any, None]]


class Subscriber(Protocol):
    """Anything that receives events pushed by the bus's shared listener."""

    @property
    def patterns(self) -> list[TopicPattern]:
        """Topic patterns the bus matches before calling :meth:`on_event`."""
        ...

    async def on_event(self, envelope: EventEnvelope) -> None:
        """Accept one event. Must not block: the shared listener awaits it."""
        ...

    async def start(self) -> None:
        """Start delivering events. Calling it while running is a no-op."""
        ...

    async def stop(self) -> None:
        """Stop delivering events.

        Must return in bounded time, be safe to call repeatedly, and leave the
        subscriber restartable with :meth:`start`.
        """
        ...


class BaseSubscriber(abc.ABC):
    """Convenience base class with shared pattern-matching logic.

    Patterns are not validated here (server-side code may use any pattern);
    adapters that take patterns from clients validate them first.
    """

    def __init__(self, patterns: Sequence[str | TopicPattern]) -> None:
        self._patterns = [p if isinstance(p, TopicPattern) else TopicPattern(p) for p in patterns]
        self._running = False

    @property
    def patterns(self) -> list[TopicPattern]:
        return self._patterns

    def accepts(self, topic: str) -> bool:
        return any(p.matches(topic) for p in self._patterns)

    @abc.abstractmethod
    async def on_event(self, envelope: EventEnvelope) -> None: ...

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
