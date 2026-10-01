from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.client import Pipeline
from redis.exceptions import ResponseError

from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.serialization import Serializer
from event_bus.core.topic import topic_to_stream_key
from event_bus.interfaces.transport import NoGroupError

logger = logging.getLogger(__name__)

# One stream entry as Redis returns it: (stream key, message ID, fields).
RawEntry = tuple[str, str, dict[bytes, bytes]]

# Warn when consumers fall this far behind the stream's length cap.
LAG_WARNING_RATIO = 0.8


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def parse_stream_id(value: Any) -> tuple[int, int]:
    ms, _, seq = _text(value).partition("-")
    return int(ms), int(seq or 0)


def format_stream_id(value: tuple[int, int]) -> str:
    return f"{value[0]}-{value[1]}"


def _raise_if_nogroup(error: ResponseError) -> None:
    if str(error).startswith("NOGROUP"):
        raise NoGroupError(str(error)) from error


class RedisStreamManager:
    """
    Manages interactions with Redis streams for event-based communication.

    Handles XADD, XREADGROUP, XACK, XCLAIM, XPENDING, XTRIM and consumer group
    creation against Redis Streams. Entries are read raw and decoded one at a
    time (:meth:`decode`), so the transport can dead-letter an undecodable
    entry without losing the rest of its batch.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        blocking_client: aioredis.Redis,
        serializer: Serializer,
        key_prefix: str = "eventbus",
        max_stream_length: int | None = 100_000,
    ) -> None:
        self._client = client
        self._blocking_client = blocking_client
        self._serializer = serializer
        self._key_prefix = key_prefix
        self._max_stream_length = max_stream_length

    def stream_key(self, topic: str) -> str:
        return topic_to_stream_key(topic, self._key_prefix)

    # -- Publishing --

    def add_to_pipeline(self, pipe: Pipeline, event: Event, fields: dict[bytes, bytes]) -> None:
        """Queue the event's XADD on a pipeline."""
        pipe.xadd(
            name=self.stream_key(event.topic),
            fields=fields,  # type: ignore[arg-type]
            maxlen=self._max_stream_length,
            approximate=True,
        )

    async def publish(self, event: Event) -> str:
        message_id = await self._client.xadd(
            name=self.stream_key(event.topic),
            fields=self.encode(event),  # type: ignore[arg-type]
            maxlen=self._max_stream_length,
            approximate=True,
        )
        return _text(message_id)

    # -- Consumer groups --

    async def create_group(self, topic: str, group_name: str, start_id: str = "0") -> None:
        stream_key = self.stream_key(topic)
        try:
            await self._client.xgroup_create(
                name=stream_key, groupname=group_name, id=start_id, mkstream=True
            )
            logger.debug("Consumer group '%s' created on '%s'", group_name, stream_key)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug("Consumer group '%s' already exists on '%s'", group_name, stream_key)

    async def read_raw(
        self,
        topics: list[str],
        group_name: str,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 2000,
    ) -> list[RawEntry]:
        streams = {self.stream_key(topic): ">" for topic in topics}
        client = self._blocking_client if block_ms > 0 else self._client
        try:
            results = await client.xreadgroup(
                groupname=group_name,
                consumername=consumer_name,
                streams=streams,  # type: ignore[arg-type]
                count=count,
                block=block_ms if block_ms > 0 else None,
            )
        except ResponseError as e:
            _raise_if_nogroup(e)
            raise
        entries: list[RawEntry] = []
        for stream_name, messages in results or []:
            for msg_id, fields in messages:
                entries.append((_text(stream_name), _text(msg_id), fields))
        return entries

    async def ack(self, topic: str, group_name: str, *message_ids: str) -> None:
        if message_ids:
            await self._client.xack(self.stream_key(topic), group_name, *message_ids)

    async def get_pending(
        self,
        topic: str,
        group_name: str,
        min_idle_ms: int = 60_000,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            result = await self._client.xpending_range(
                name=self.stream_key(topic),
                groupname=group_name,
                min="-",
                max="+",
                count=count,
                idle=min_idle_ms,
            )
        except ResponseError as e:
            _raise_if_nogroup(e)
            raise
        return list(result)

    async def claim_raw(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_ids: list[str],
        min_idle_ms: int = 60_000,
    ) -> list[RawEntry]:
        """XCLAIM several messages in one call. Entries trimmed from the
        stream are skipped (Redis 7 also drops them from the pending list)."""
        if not message_ids:
            return []
        stream_key = self.stream_key(topic)
        results = await self._client.xclaim(
            name=stream_key,
            groupname=group_name,
            consumername=consumer_name,
            min_idle_time=min_idle_ms,
            message_ids=message_ids,  # type: ignore[arg-type]
        )
        return [(stream_key, _text(msg_id), fields) for msg_id, fields in results if fields]

    async def touch(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_ids: list[str],
        idle_ms: int = 0,
    ) -> None:
        if not message_ids:
            return
        # JUSTID: take ownership and set the idle time without counting a
        # delivery attempt.
        await self._client.xclaim(
            name=self.stream_key(topic),
            groupname=group_name,
            consumername=consumer_name,
            min_idle_time=0,
            message_ids=message_ids,  # type: ignore[arg-type]
            idle=max(0, idle_ms),
            justid=True,
        )

    # -- Housekeeping --

    async def trim_consumed(self, topic: str) -> int:
        stream_key = self.stream_key(topic)
        try:
            groups = await self._client.xinfo_groups(stream_key)
        except ResponseError:
            return 0  # no such stream
        if not groups:
            return 0  # nobody consumes it: only the length cap applies
        floor: tuple[int, int] | None = None
        for group in groups:
            name = _text(group["name"])
            if int(group.get("pending") or 0) > 0:
                summary = await self._client.xpending(stream_key, name)
                candidate = parse_stream_id(summary["min"])
            else:
                ms, seq = parse_stream_id(group["last-delivered-id"])
                candidate = (ms, seq + 1)  # everything up to it was delivered and acked
            floor = candidate if floor is None else min(floor, candidate)
            lag = group.get("lag")
            if (
                self._max_stream_length
                and lag is not None
                and int(lag) > self._max_stream_length * LAG_WARNING_RATIO
            ):
                logger.warning(
                    "Consumer group '%s' is %d entries behind on '%s' (length cap %d): "
                    "past the cap, unprocessed events are dropped",
                    name,
                    int(lag),
                    stream_key,
                    self._max_stream_length,
                )
        if floor is None:
            return 0
        removed = await self._client.xtrim(
            stream_key, minid=format_stream_id(floor), approximate=True
        )
        return int(removed or 0)

    async def remove_idle_consumers(
        self, topic: str, group_name: str, idle_ms: int, keep: set[str]
    ) -> int:
        stream_key = self.stream_key(topic)
        try:
            consumers = await self._client.xinfo_consumers(stream_key, group_name)
        except ResponseError:
            return 0
        removed = 0
        for consumer in consumers:
            name = _text(consumer["name"])
            if name in keep or int(consumer.get("pending") or 0) > 0:
                continue
            if int(consumer.get("idle") or 0) <= idle_ms:
                continue
            await self._client.xgroup_delconsumer(stream_key, group_name, name)
            removed += 1
            logger.info("Removed idle consumer '%s' from group '%s'", name, group_name)
        return removed

    # -- Encoding / Decoding --

    def encode(self, event: Event) -> dict[bytes, bytes]:
        return {
            b"topic": event.topic.encode(),
            b"data": self._serializer.serialize(event.data),
            b"meta": self.encode_meta(event),
        }

    def encode_meta(self, event: Event) -> bytes:
        return self._serializer.serialize(
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

    def decode_event(self, topic: bytes, data: bytes, meta: bytes) -> Event:
        """:raises Exception: The entry is malformed (missing fields, not
        deserializable)."""
        meta_dict = self._serializer.deserialize(meta)
        metadata = EventMetadata(
            event_id=meta_dict["event_id"],
            timestamp=meta_dict["timestamp"],
            source=meta_dict.get("source", ""),
            correlation_id=meta_dict.get("correlation_id", ""),
            causation_id=meta_dict.get("causation_id", ""),
            content_type=meta_dict.get("content_type", "application/json"),
            retry_count=int(meta_dict.get("retry_count", 0)),
            headers=dict(meta_dict.get("headers", {})),
        )
        return Event(
            topic=topic.decode(), data=self._serializer.deserialize(data), metadata=metadata
        )

    def decode(
        self,
        stream_name: str,
        msg_id: str,
        fields: dict[bytes, bytes],
        delivery_count: int = 0,
    ) -> EventEnvelope:
        """:raises Exception: The entry is malformed."""
        event = self.decode_event(fields[b"topic"], fields[b"data"], fields[b"meta"])
        return EventEnvelope(
            event=event,
            stream_id=msg_id,
            stream_name=stream_name,
            delivery_count=delivery_count,
        )
