from __future__ import annotations

from typing import Protocol

from event_bus.core.event import Event


class Publisher(Protocol):
    """Anything that can publish events."""

    async def publish(self, event: Event) -> str:
        """Publish an event. Returns the stream message ID."""
        ...

    async def publish_many(self, events: list[Event]) -> list[str]:
        """Publish multiple events in one round trip (pipelined).

        Not atomic or transactional: the events are sent in a single
        non-transactional pipeline, so if an error occurs some of them may
        already have been published. Returns the stream message IDs in the
        same order as *events*.
        """
        ...
