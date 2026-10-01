from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from event_bus.core.errors import EventBusError
from event_bus.core.event import Event, EventEnvelope


class NoGroupError(EventBusError):
    """A consumer group (or its stream) doesn't exist, e.g. the stream key was
    deleted or evicted. Consumer groups recreate it and carry on."""


IdempotencyStatus = Literal["new", "done", "busy"]

# The DLQ marks an entry it couldn't decode with this header (its raw fields
# are in the event's data): there's nothing to replay.
UNDECODABLE_HEADER = "x-eventbus-undecodable"


class IdempotencyStore(Protocol):
    """
    Per-group processing records, so an event (by ID) is handled once per
    consumer group even when it's published more than once.

    ``begin`` claims an event for processing with a lease; ``complete`` marks
    it done for the TTL; ``release`` gives the claim up after a failure so a
    retry can process it.
    """

    async def begin(self, group: str, event_id: str) -> tuple[IdempotencyStatus, str | None]:
        """``("new", token)`` when claimed; ``("done", None)`` when already
        processed; ``("busy", None)`` while another consumer holds it."""
        ...

    async def complete(self, group: str, event_id: str, token: str) -> None: ...

    async def release(self, group: str, event_id: str, token: str) -> None: ...


class Transport(Protocol):
    """
    Abstraction over the messaging infrastructure.

    The EventBus talks to a Transport, never directly to Redis.
    This enables testing with an InMemoryTransport.

    Delivery is at-least-once: a message stays pending in its consumer group
    until acked or dead-lettered, and pending messages whose lease lapses
    (``claim_idle``) are delivered again.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    # -- Publishing --

    async def publish(self, event: Event) -> str:
        """Publish to both stream (durable) and pub/sub (real-time). Returns stream ID."""
        ...

    async def publish_many(self, events: list[Event]) -> list[str]: ...

    # -- Stream consumption (consumer groups) --

    async def create_consumer_group(
        self,
        topic: str,
        group_name: str,
        start_id: str = "0",
    ) -> None: ...

    async def read_group(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 2000,
    ) -> list[EventEnvelope]: ...

    async def read_group_many(
        self,
        topics: list[str],
        group_name: str,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 2000,
    ) -> list[EventEnvelope]:
        """
        New messages for the consumer. Entries that can't be decoded are
        dead-lettered (and acked) here, never returned.

        :raises NoGroupError: The group or its stream doesn't exist.
        """
        ...

    async def ack(self, topic: str, group_name: str, message_id: str) -> None: ...

    async def ack_many(self, topic: str, group_name: str, message_ids: list[str]) -> None:
        """Acknowledge several messages in one round trip."""
        ...

    async def get_pending(
        self,
        topic: str,
        group_name: str,
        min_idle_ms: int = 60_000,
        count: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def claim_message(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_id: str,
        min_idle_ms: int = 60_000,
    ) -> EventEnvelope | None: ...

    async def claim_idle(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int = 100,
    ) -> list[EventEnvelope]:
        """
        Take over pending messages idle for at least ``min_idle_ms`` (their
        lease lapsed: the consumer died, or a failed message's retry is due),
        each with its ``delivery_count``. Undecodable entries are dead-lettered.
        """
        ...

    async def touch(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_ids: list[str],
        idle_ms: int = 0,
    ) -> None:
        """
        Set the idle time of pending messages the consumer holds, without
        counting a delivery: ``0`` renews their lease (a heartbeat); a larger
        value makes them claimable sooner (``claim_idle_ms - delay`` schedules
        a retry after ``delay``).
        """
        ...

    # -- Housekeeping --

    async def trim_consumed(self, topic: str) -> int:
        """
        Trim the entries every consumer group of the topic has processed,
        never one that's pending or not yet delivered to a group. Returns how
        many entries were removed (approximate).
        """
        ...

    async def remove_idle_consumers(
        self, topic: str, group_name: str, idle_ms: int, keep: set[str]
    ) -> int:
        """Delete the group's consumers idle longer than ``idle_ms`` with no
        pending messages (dead processes' names), except those in ``keep``."""
        ...

    def idempotency_store(self, ttl_seconds: int, lease_seconds: int) -> IdempotencyStore: ...

    # -- Pub/Sub (real-time) --

    def subscribe_pubsub(
        self,
        patterns: list[str],
        ready: asyncio.Event | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        """
        Yield events from Redis Pub/Sub matching the given channel patterns.
        ``ready`` is set once the subscription is confirmed: events published
        after that are received.
        """
        ...

    # -- Dead Letter Queue (one per consumer group) --

    async def dead_letter(self, envelope: EventEnvelope, error: str, group_name: str) -> None:
        """Move a message to the group's DLQ and ack it, atomically."""
        ...

    async def send_to_dlq(
        self, envelope: EventEnvelope, error: str, group_name: str | None = None
    ) -> None: ...

    async def read_dlq(self, count: int = 10, group_name: str | None = None) -> list[EventEnvelope]:
        """Dead letters, oldest first; each envelope's ``stream_id`` is its DLQ
        entry ID (for ``delete_dlq``)."""
        ...

    async def delete_dlq(self, message_id: str, group_name: str | None = None) -> None: ...
