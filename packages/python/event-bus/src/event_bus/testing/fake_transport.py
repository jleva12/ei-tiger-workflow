from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from event_bus.core.errors import EventBusConnectionError
from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.serialization import JsonSerializer, Serializer
from event_bus.core.topic import TopicPattern, topic_to_stream_key
from event_bus.interfaces.transport import IdempotencyStore, NoGroupError
from event_bus.resilience.idempotency import InMemoryIdempotencyStore


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class _Pending:
    envelope: EventEnvelope
    index: int  # absolute position in the stream
    consumer: str
    delivered_at_ms: int
    times_delivered: int


@dataclass
class _Group:
    cursor: int  # absolute index of the next undelivered entry
    pending: dict[str, _Pending] = field(default_factory=dict)
    consumers: dict[str, int] = field(default_factory=dict)  # name -> last active ms


@dataclass
class _Stream:
    entries: list[EventEnvelope] = field(default_factory=list)
    offset: int = 0  # absolute index of entries[0] (entries before were trimmed)
    groups: dict[str, _Group] = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.offset + len(self.entries)


class InMemoryTransport:
    """
    In-memory Transport for unit tests. No Redis required.

    It models what the Redis transport does, so tests exercise real
    behaviour: events round-trip through the serializer (consumers get what
    Redis would give them, e.g. a datetime comes back as a string), stream
    envelopes carry the Redis stream key, a consumer group must exist before
    it's read (``NoGroupError``) and honours its ``start_id``, pending
    messages have idle times and delivery counts, leases can be renewed
    (``touch``), the subscription signals ``ready``, and ``disconnect``
    keeps state (use :meth:`reset` to clear it) but refuses use until
    reconnected.
    """

    def __init__(
        self,
        key_prefix: str = "eventbus",
        max_stream_length: int | None = None,
        serializer: Serializer | None = None,
    ) -> None:
        self._key_prefix = key_prefix
        self._max_stream_length = max_stream_length
        self._serializer: Serializer = serializer or JsonSerializer()
        self._connected = False
        self._streams: dict[str, _Stream] = defaultdict(_Stream)
        # (envelope with its DLQ id as stream_id, error, group)
        self._dlq: list[tuple[EventEnvelope, str, str | None]] = []
        self._pubsub_queues: list[asyncio.Queue[EventEnvelope | None]] = []
        self._counter = 0
        self._dlq_counter = 0
        self._new_data = asyncio.Event()

        # Exposed for test assertions
        self.published_events: list[Event] = []

    # -- Lifecycle --

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        # Signal all active pub/sub listeners to exit
        for q in self._pubsub_queues:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)
        self._pubsub_queues.clear()
        self._new_data.set()

    def reset(self) -> None:
        """Forget every stream, group, dead letter and published event."""
        self._streams.clear()
        self._dlq.clear()
        self.published_events.clear()
        self._counter = 0
        self._dlq_counter = 0

    async def is_healthy(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise EventBusConnectionError("The in-memory transport is not connected")

    def _stream_key(self, topic: str) -> str:
        return topic_to_stream_key(topic, self._key_prefix)

    # -- Publishing --

    def _round_trip(self, event: Event) -> Event:
        """The event as a consumer would read it back from Redis."""
        meta = self._serializer.deserialize(
            self._serializer.serialize(
                {
                    "event_id": event.metadata.event_id,
                    "timestamp": event.metadata.timestamp,
                    "source": event.metadata.source,
                    "correlation_id": event.metadata.correlation_id,
                    "causation_id": event.metadata.causation_id,
                    "content_type": event.metadata.content_type,
                    "retry_count": event.metadata.retry_count,
                    "headers": event.metadata.headers,
                }
            )
        )
        return Event(
            topic=event.topic,
            data=self._serializer.deserialize(self._serializer.serialize(event.data)),
            metadata=EventMetadata(
                event_id=meta["event_id"],
                timestamp=meta["timestamp"],
                source=meta["source"],
                correlation_id=meta["correlation_id"],
                causation_id=meta["causation_id"],
                content_type=meta["content_type"],
                retry_count=int(meta["retry_count"]),
                headers=dict(meta["headers"]),
            ),
        )

    async def publish(self, event: Event) -> str:
        self._require_connected()
        stored = self._round_trip(event)
        self._counter += 1
        stream_id = f"{self._counter}-0"
        stream = self._streams[event.topic]
        stream.entries.append(
            EventEnvelope(
                event=stored, stream_id=stream_id, stream_name=self._stream_key(event.topic)
            )
        )
        if self._max_stream_length is not None and len(stream.entries) > self._max_stream_length:
            # Like MAXLEN: trims the oldest, delivered or not.
            dropped = len(stream.entries) - self._max_stream_length
            del stream.entries[:dropped]
            stream.offset += dropped
        self.published_events.append(event)
        live = EventEnvelope(event=self._round_trip(event), stream_id="", stream_name="")
        for q in self._pubsub_queues:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(live)
        self._new_data.set()
        return stream_id

    async def publish_many(self, events: list[Event]) -> list[str]:
        return [await self.publish(e) for e in events]

    # -- Consumer groups --

    async def create_consumer_group(self, topic: str, group_name: str, start_id: str = "0") -> None:
        self._require_connected()
        stream = self._streams[topic]
        if group_name not in stream.groups:
            stream.groups[group_name] = _Group(cursor=stream.end if start_id == "$" else 0)

    def _group(self, topic: str, group_name: str) -> tuple[_Stream, _Group]:
        stream = self._streams.get(topic)
        group = stream.groups.get(group_name) if stream is not None else None
        if stream is None or group is None:
            raise NoGroupError(
                f"NOGROUP No such key '{self._stream_key(topic)}' or consumer group '{group_name}'"
            )
        return stream, group

    async def read_group(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 2000,
    ) -> list[EventEnvelope]:
        return await self.read_group_many([topic], group_name, consumer_name, count, block_ms)

    async def read_group_many(
        self,
        topics: list[str],
        group_name: str,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 2000,
    ) -> list[EventEnvelope]:
        self._require_connected()
        deadline = time.monotonic() + max(0, block_ms) / 1000
        while True:
            # Clear before reading: a publish after the read sets it again,
            # so its wake-up isn't lost.
            self._new_data.clear()
            messages = self._read_nowait(topics, group_name, consumer_name, count)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0 or not self._connected:
                return messages
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new_data.wait(), timeout=remaining)

    def _read_nowait(
        self, topics: list[str], group_name: str, consumer_name: str, count: int
    ) -> list[EventEnvelope]:
        messages: list[EventEnvelope] = []
        now = _now_ms()
        for topic in topics:
            stream, group = self._group(topic, group_name)
            group.consumers[consumer_name] = now
            start = max(group.cursor, stream.offset)
            taken = stream.entries[start - stream.offset : start - stream.offset + count]
            group.cursor = start + len(taken)
            for i, envelope in enumerate(taken):
                delivered = replace(envelope, delivery_count=1)
                group.pending[envelope.stream_id] = _Pending(
                    delivered, start + i, consumer_name, now, 1
                )
                messages.append(delivered)
        return messages

    async def ack(self, topic: str, group_name: str, message_id: str) -> None:
        await self.ack_many(topic, group_name, [message_id])

    async def ack_many(self, topic: str, group_name: str, message_ids: list[str]) -> None:
        self._require_connected()
        _, group = self._group(topic, group_name)
        for message_id in message_ids:
            group.pending.pop(message_id, None)

    async def get_pending(
        self,
        topic: str,
        group_name: str,
        min_idle_ms: int = 60_000,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        self._require_connected()
        _, group = self._group(topic, group_name)
        now = _now_ms()
        results: list[dict[str, Any]] = []
        for message_id, entry in sorted(group.pending.items(), key=lambda kv: kv[1].index):
            idle_ms = now - entry.delivered_at_ms
            if idle_ms >= min_idle_ms:
                results.append(
                    {
                        "message_id": message_id,
                        "consumer": entry.consumer,
                        "time_since_delivered": idle_ms,
                        "times_delivered": entry.times_delivered,
                    }
                )
            if len(results) >= count:
                break
        return results

    def _claim(
        self, group: _Group, consumer_name: str, message_id: str, min_idle_ms: int, now: int
    ) -> EventEnvelope | None:
        entry = group.pending.get(message_id)
        if entry is None or now - entry.delivered_at_ms < min_idle_ms:
            return None
        entry.consumer = consumer_name
        entry.delivered_at_ms = now
        entry.times_delivered += 1
        entry.envelope = replace(entry.envelope, delivery_count=entry.times_delivered)
        group.consumers[consumer_name] = now
        return entry.envelope

    async def claim_message(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_id: str,
        min_idle_ms: int = 60_000,
    ) -> EventEnvelope | None:
        self._require_connected()
        _, group = self._group(topic, group_name)
        return self._claim(group, consumer_name, message_id, min_idle_ms, _now_ms())

    async def claim_idle(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int = 100,
    ) -> list[EventEnvelope]:
        pending = await self.get_pending(topic, group_name, min_idle_ms, count)
        _, group = self._group(topic, group_name)
        now = _now_ms()
        claimed = [
            self._claim(group, consumer_name, p["message_id"], min_idle_ms, now) for p in pending
        ]
        return [envelope for envelope in claimed if envelope is not None]

    async def touch(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_ids: list[str],
        idle_ms: int = 0,
    ) -> None:
        self._require_connected()
        _, group = self._group(topic, group_name)
        now = _now_ms()
        for message_id in message_ids:
            entry = group.pending.get(message_id)
            if entry is not None:
                entry.consumer = consumer_name
                entry.delivered_at_ms = now - max(0, idle_ms)
        group.consumers[consumer_name] = now

    # -- Housekeeping --

    async def trim_consumed(self, topic: str) -> int:
        self._require_connected()
        stream = self._streams.get(topic)
        if stream is None or not stream.groups:
            return 0
        floor = min(
            min((p.index for p in group.pending.values()), default=group.cursor)
            for group in stream.groups.values()
        )
        dropped = max(0, floor - stream.offset)
        if dropped:
            del stream.entries[:dropped]
            stream.offset += dropped
        return dropped

    async def remove_idle_consumers(
        self, topic: str, group_name: str, idle_ms: int, keep: set[str]
    ) -> int:
        self._require_connected()
        _, group = self._group(topic, group_name)
        now = _now_ms()
        busy = {p.consumer for p in group.pending.values()}
        stale = [
            name
            for name, last in group.consumers.items()
            if name not in keep and name not in busy and now - last > idle_ms
        ]
        for name in stale:
            del group.consumers[name]
        return len(stale)

    def consumers(self, topic: str, group_name: str) -> list[str]:
        """The group's known consumers (for tests)."""
        return sorted(self._group(topic, group_name)[1].consumers)

    def stream_length(self, topic: str) -> int:
        """Entries currently retained in the topic's stream (for tests)."""
        stream = self._streams.get(topic)
        return len(stream.entries) if stream is not None else 0

    def idempotency_store(self, ttl_seconds: int, lease_seconds: int) -> IdempotencyStore:
        return InMemoryIdempotencyStore(ttl_seconds=ttl_seconds, lease_seconds=lease_seconds)

    # -- Pub/Sub --

    async def subscribe_pubsub(
        self, patterns: list[str], ready: asyncio.Event | None = None
    ) -> AsyncIterator[EventEnvelope]:
        self._require_connected()
        queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue(maxsize=1024)
        self._pubsub_queues.append(queue)
        compiled = [TopicPattern(p) for p in patterns]
        if ready is not None:
            ready.set()
        try:
            while True:
                envelope = await queue.get()
                if envelope is None:
                    break
                if any(p.matches(envelope.event.topic) for p in compiled):
                    yield envelope
        finally:
            if queue in self._pubsub_queues:
                self._pubsub_queues.remove(queue)

    # -- DLQ --

    def _add_dlq(self, envelope: EventEnvelope, error: str, group_name: str | None) -> None:
        self._dlq_counter += 1
        key = f"{self._key_prefix}:dlq" + (f":{group_name}" if group_name else "")
        self._dlq.append(
            (
                replace(envelope, stream_id=f"{self._dlq_counter}-0", stream_name=key),
                error,
                group_name,
            )
        )

    async def dead_letter(self, envelope: EventEnvelope, error: str, group_name: str) -> None:
        self._require_connected()
        _, group = self._group(envelope.event.topic, group_name)
        self._add_dlq(envelope, error, group_name)
        group.pending.pop(envelope.stream_id, None)

    async def send_to_dlq(
        self, envelope: EventEnvelope, error: str, group_name: str | None = None
    ) -> None:
        self._require_connected()
        self._add_dlq(envelope, error, group_name)

    async def read_dlq(self, count: int = 10, group_name: str | None = None) -> list[EventEnvelope]:
        self._require_connected()
        return [e for e, _, group in self._dlq if group == group_name][:count]

    async def delete_dlq(self, message_id: str, group_name: str | None = None) -> None:
        self._require_connected()
        self._dlq = [
            item
            for item in self._dlq
            if not (item[2] == group_name and item[0].stream_id == message_id)
        ]
