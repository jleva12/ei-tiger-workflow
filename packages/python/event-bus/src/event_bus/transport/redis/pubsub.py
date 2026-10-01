from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import AsyncIterator
from typing import Any, cast

import redis.asyncio as aioredis
from redis.asyncio.client import Pipeline

from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.serialization import Serializer
from event_bus.core.topic import topic_to_channel

logger = logging.getLogger(__name__)

# Pub/sub payloads carry the stream entry's already-serialized fields in a
# small frame, so an event is serialized once per publish:
# MAGIC, then the lengths of topic, data and meta (big-endian uint32), then
# the three byte strings.
FRAME_MAGIC = b"EBF1"
_LENGTHS = struct.Struct(">III")
# How long one poll for a message waits; bounds how quickly a stop is noticed.
POLL_TIMEOUT = 1.0
# Bound on the unsubscribe at the end, which talks to a possibly dead socket.
UNSUBSCRIBE_TIMEOUT = 2.0


def encode_frame(topic: bytes, data: bytes, meta: bytes) -> bytes:
    return FRAME_MAGIC + _LENGTHS.pack(len(topic), len(data), len(meta)) + topic + data + meta


def decode_frame(payload: bytes) -> tuple[bytes, bytes, bytes] | None:
    """The frame's (topic, data, meta), or None when it isn't one."""
    if not payload.startswith(FRAME_MAGIC):
        return None
    start = len(FRAME_MAGIC)
    topic_len, data_len, meta_len = _LENGTHS.unpack_from(payload, start)
    start += _LENGTHS.size
    end = start + topic_len + data_len + meta_len
    if end != len(payload):
        raise ValueError("pub/sub frame lengths don't match its size")
    topic = payload[start : start + topic_len]
    data = payload[start + topic_len : start + topic_len + data_len]
    meta = payload[start + topic_len + data_len : end]
    return topic, data, meta


class RedisPubSubManager:
    """PUBLISH and PSUBSCRIBE for real-time (at-most-once) delivery."""

    def __init__(
        self,
        client: aioredis.Redis,
        serializer: Serializer,
        key_prefix: str = "eventbus",
        subscribe_client: aioredis.Redis | None = None,
        decode_event: Any = None,
    ) -> None:
        self._client = client
        self._serializer = serializer
        self._key_prefix = key_prefix
        self._subscribe_client = subscribe_client or client
        # (topic, data, meta) bytes -> Event; the stream manager's decoder.
        self._decode_event = decode_event

    def channel(self, topic: str) -> str:
        return topic_to_channel(topic, self._key_prefix)

    def add_to_pipeline(self, pipe: Pipeline, event: Event, fields: dict[bytes, bytes]) -> None:
        """Queue the event's PUBLISH on a pipeline, from its encoded fields."""
        pipe.publish(
            self.channel(event.topic),
            encode_frame(fields[b"topic"], fields[b"data"], fields[b"meta"]),
        )

    def _decode(self, payload: bytes) -> Event:
        frame = decode_frame(payload)
        if frame is not None and self._decode_event is not None:
            event: Event = self._decode_event(*frame)
            return event
        # The earlier format: one serialized {"topic", "data", "meta"} object.
        decoded = self._serializer.deserialize(payload)
        meta = decoded["meta"]
        return Event(
            topic=decoded["topic"],
            data=decoded["data"],
            metadata=EventMetadata(
                event_id=meta["event_id"],
                timestamp=meta["timestamp"],
                source=meta.get("source", ""),
                correlation_id=meta.get("correlation_id", ""),
                causation_id=meta.get("causation_id", ""),
                content_type=meta.get("content_type", "application/json"),
                retry_count=int(meta.get("retry_count", 0)),
                headers=dict(meta.get("headers", {})),
            ),
        )

    async def subscribe(
        self,
        patterns: list[str],
        ready: asyncio.Event | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        """
        Subscribe to Pub/Sub channels matching glob patterns and yield
        EventEnvelopes as they arrive. ``ready`` is set once Redis has
        confirmed every pattern. A malformed message is skipped; a dead
        connection raises (the periodic PING or the socket timeout notices).
        """
        pubsub = self._subscribe_client.pubsub()
        channel_patterns = [self.channel(p) for p in patterns]
        logger.debug("PSUBSCRIBE %s", channel_patterns)
        await pubsub.psubscribe(*channel_patterns)
        confirmed = 0
        try:
            while True:
                message = await pubsub.get_message(timeout=POLL_TIMEOUT)
                if message is None:
                    continue
                kind = message["type"]
                if kind == "psubscribe":
                    confirmed += 1
                    if ready is not None and confirmed >= len(channel_patterns):
                        ready.set()
                    continue
                if kind not in ("pmessage", "message"):
                    continue
                try:
                    event = self._decode(cast(bytes, message["data"]))
                except Exception as exc:
                    logger.warning("Skipping a malformed pub/sub message: %s", exc)
                    continue
                yield EventEnvelope(event=event, stream_id="", stream_name="")
        finally:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(UNSUBSCRIBE_TIMEOUT):
                    await pubsub.punsubscribe(*channel_patterns)
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]
