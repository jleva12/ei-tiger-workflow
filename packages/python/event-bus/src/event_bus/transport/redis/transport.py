from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from event_bus.config import RedisConfig
from event_bus.core.errors import EventBusConnectionError, PublishError
from event_bus.core.event import Event, EventEnvelope
from event_bus.core.serialization import JsonSerializer, MsgpackSerializer, Serializer
from event_bus.interfaces.transport import IdempotencyStore
from event_bus.resilience.idempotency import IdempotencyFilter
from event_bus.transport.redis.connection import RedisConnectionManager, is_connectivity_error
from event_bus.transport.redis.dlq import DeadLetterQueue
from event_bus.transport.redis.pubsub import RedisPubSubManager
from event_bus.transport.redis.stream import RawEntry, RedisStreamManager

logger = logging.getLogger(__name__)


def _default_serializer(config: RedisConfig) -> Serializer:
    return MsgpackSerializer() if config.serializer == "msgpack" else JsonSerializer()


class RedisTransport:
    """
    Handles interactions with Redis for event-driven communication.

    Dual-write architecture: every publish writes to a Redis Stream (durable,
    ordered, consumed by consumer groups at least once) and Redis Pub/Sub
    (real-time, at most once), in one round trip. Only the stream write
    decides whether a publish succeeded.
    """

    def __init__(
        self,
        config: RedisConfig,
        serializer: Serializer | None = None,
    ) -> None:
        self._config = config
        self._serializer: Serializer = serializer or _default_serializer(config)
        self._conn_mgr = RedisConnectionManager(config)
        self._stream_mgr: RedisStreamManager | None = None
        self._pubsub_mgr: RedisPubSubManager | None = None
        self._dlq: DeadLetterQueue | None = None
        logger.debug(
            "RedisTransport created (url=%s, prefix=%s)", config.safe_url, config.key_prefix
        )

    async def connect(self) -> None:
        await self._conn_mgr.connect()
        self._stream_mgr = RedisStreamManager(
            client=self._conn_mgr.client,
            blocking_client=self._conn_mgr.blocking_client,
            serializer=self._serializer,
            key_prefix=self._config.key_prefix,
            max_stream_length=self._config.max_stream_length,
        )
        self._pubsub_mgr = RedisPubSubManager(
            client=self._conn_mgr.client,
            serializer=self._serializer,
            key_prefix=self._config.key_prefix,
            subscribe_client=self._conn_mgr.pubsub_client,
            decode_event=self._stream_mgr.decode_event,
        )
        self._dlq = DeadLetterQueue(
            client=self._conn_mgr.client,
            serializer=self._serializer,
            key_prefix=self._config.key_prefix,
            max_length=self._config.dlq_max_length,
        )
        logger.info("RedisTransport connected to %s", self._config.safe_url)

    async def disconnect(self) -> None:
        await self._conn_mgr.disconnect()
        self._stream_mgr = None
        self._pubsub_mgr = None
        self._dlq = None
        logger.info("RedisTransport disconnected")

    async def is_healthy(self) -> bool:
        return self._conn_mgr.is_healthy

    # -- Connection state --

    def _streams(self) -> RedisStreamManager:
        if self._stream_mgr is None:
            raise EventBusConnectionError("The Redis transport is not connected")
        return self._stream_mgr

    def _pubsub(self) -> RedisPubSubManager:
        if self._pubsub_mgr is None:
            raise EventBusConnectionError("The Redis transport is not connected")
        return self._pubsub_mgr

    def _dead_letters(self) -> DeadLetterQueue:
        if self._dlq is None:
            raise EventBusConnectionError("The Redis transport is not connected")
        return self._dlq

    # -- Publishing (dual-write: Stream + Pub/Sub, one round trip) --

    async def publish(self, event: Event) -> str:
        [stream_id] = await self._publish([event])
        return stream_id

    async def publish_many(self, events: list[Event]) -> list[str]:
        if not events:
            return []
        return await self._publish(events)

    async def _publish(self, events: list[Event]) -> list[str]:
        streams, pubsub = self._streams(), self._pubsub()
        # Encoding errors are the caller's: raised before the circuit breaker
        # is involved.
        encoded = [(event, streams.encode(event)) for event in events]
        breaker = self._conn_mgr.circuit_breaker
        probe = breaker.acquire()
        try:
            pipe = self._conn_mgr.client.pipeline(transaction=False)
            for event, fields in encoded:
                streams.add_to_pipeline(pipe, event, fields)
                pubsub.add_to_pipeline(pipe, event, fields)
            results = await pipe.execute(raise_on_error=False)
        except Exception as exc:
            self._settle_failure(probe, exc)
            raise PublishError(f"Publishing {len(events)} event(s) failed: {exc}") from exc
        stream_ids: list[str] = []
        stream_error: BaseException | None = None
        for index, (event, _) in enumerate(encoded):
            stream_result, pubsub_result = results[2 * index], results[2 * index + 1]
            if isinstance(stream_result, BaseException):
                stream_error = stream_error or stream_result
                continue
            stream_ids.append(
                stream_result.decode() if isinstance(stream_result, bytes) else str(stream_result)
            )
            if isinstance(pubsub_result, BaseException):
                logger.warning(
                    "Pub/Sub publish failed for event %s (non-fatal): %s",
                    event.event_id,
                    pubsub_result,
                )
        if stream_error is not None:
            self._settle_failure(probe, stream_error)
            raise PublishError(
                f"{len(events) - len(stream_ids)} of {len(events)} event(s) weren't "
                f"written: {stream_error}"
            ) from stream_error
        breaker.on_success(probe)
        return stream_ids

    def _settle_failure(self, probe: bool, exc: BaseException) -> None:
        breaker = self._conn_mgr.circuit_breaker
        if is_connectivity_error(exc):
            breaker.on_failure(probe)
        else:
            breaker.on_ignored(probe)

    # -- Consumer groups --

    async def create_consumer_group(self, topic: str, group_name: str, start_id: str = "0") -> None:
        await self._streams().create_group(topic, group_name, start_id)

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
        entries = await self._streams().read_raw(topics, group_name, consumer_name, count, block_ms)
        return await self._decode_entries(entries, group_name, delivery_counts=None)

    async def _decode_entries(
        self,
        entries: list[RawEntry],
        group_name: str,
        delivery_counts: dict[str, int] | None,
    ) -> list[EventEnvelope]:
        """Decode each entry; dead-letter (and ack) the ones that can't be."""
        streams = self._streams()
        envelopes: list[EventEnvelope] = []
        for stream_name, msg_id, fields in entries:
            count = delivery_counts.get(msg_id, 1) if delivery_counts else 1
            try:
                envelopes.append(streams.decode(stream_name, msg_id, fields, delivery_count=count))
            except Exception as exc:
                logger.error(
                    "Dead-lettering undecodable entry %s on %s for group '%s': %s",
                    msg_id,
                    stream_name,
                    group_name,
                    exc,
                )
                await self._dead_letter_raw(
                    stream_name, msg_id, fields, f"undecodable: {exc!r}", group_name
                )
        return envelopes

    async def _dead_letter_raw(
        self,
        stream_name: str,
        msg_id: str,
        fields: dict[bytes, bytes],
        error: str,
        group_name: str,
    ) -> None:
        pipe = self._conn_mgr.client.pipeline(transaction=True)
        self._dead_letters().add_to_pipeline(pipe, fields, error, group_name, stream_name, msg_id)
        pipe.xack(stream_name, group_name, msg_id)
        await pipe.execute()

    async def ack(self, topic: str, group_name: str, message_id: str) -> None:
        await self._streams().ack(topic, group_name, message_id)

    async def ack_many(self, topic: str, group_name: str, message_ids: list[str]) -> None:
        await self._streams().ack(topic, group_name, *message_ids)

    async def get_pending(
        self,
        topic: str,
        group_name: str,
        min_idle_ms: int = 60_000,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._streams().get_pending(topic, group_name, min_idle_ms, count)

    async def claim_message(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_id: str,
        min_idle_ms: int = 60_000,
    ) -> EventEnvelope | None:
        entries = await self._streams().claim_raw(
            topic, group_name, consumer_name, [message_id], min_idle_ms
        )
        envelopes = await self._decode_entries(entries, group_name, delivery_counts=None)
        return envelopes[0] if envelopes else None

    async def claim_idle(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int = 100,
    ) -> list[EventEnvelope]:
        streams = self._streams()
        pending = await streams.get_pending(topic, group_name, min_idle_ms, count)
        if not pending:
            return []
        # Each claim counts one more delivery.
        delivery_counts = {
            (p["message_id"].decode() if isinstance(p["message_id"], bytes) else p["message_id"]): (
                int(p["times_delivered"]) + 1
            )
            for p in pending
        }
        entries = await streams.claim_raw(
            topic, group_name, consumer_name, list(delivery_counts), min_idle_ms
        )
        return await self._decode_entries(entries, group_name, delivery_counts)

    async def touch(
        self,
        topic: str,
        group_name: str,
        consumer_name: str,
        message_ids: list[str],
        idle_ms: int = 0,
    ) -> None:
        await self._streams().touch(topic, group_name, consumer_name, message_ids, idle_ms)

    # -- Housekeeping --

    async def trim_consumed(self, topic: str) -> int:
        return await self._streams().trim_consumed(topic)

    async def remove_idle_consumers(
        self, topic: str, group_name: str, idle_ms: int, keep: set[str]
    ) -> int:
        return await self._streams().remove_idle_consumers(topic, group_name, idle_ms, keep)

    def idempotency_store(self, ttl_seconds: int, lease_seconds: int) -> IdempotencyStore:
        return IdempotencyFilter(
            self._conn_mgr.client,
            key_prefix=self._config.key_prefix,
            ttl_seconds=ttl_seconds,
            lease_seconds=lease_seconds,
        )

    # -- Pub/Sub --

    async def subscribe_pubsub(
        self, patterns: list[str], ready: asyncio.Event | None = None
    ) -> AsyncIterator[EventEnvelope]:
        async for envelope in self._pubsub().subscribe(patterns, ready):
            yield envelope

    # -- DLQ --

    async def dead_letter(self, envelope: EventEnvelope, error: str, group_name: str) -> None:
        dlq = self._dead_letters()
        pipe = self._conn_mgr.client.pipeline(transaction=True)
        dlq.add_to_pipeline(
            pipe,
            dlq.event_fields(envelope.event),
            error,
            group_name,
            envelope.stream_name,
            envelope.stream_id,
        )
        pipe.xack(envelope.stream_name, group_name, envelope.stream_id)
        await pipe.execute()
        logger.info(
            "Event %s dead-lettered for group '%s': %s",
            envelope.event.event_id,
            group_name,
            error,
        )
        await dlq.check_capacity(group_name)

    async def send_to_dlq(
        self, envelope: EventEnvelope, error: str, group_name: str | None = None
    ) -> None:
        await self._dead_letters().send(envelope, error, group_name)

    async def read_dlq(self, count: int = 10, group_name: str | None = None) -> list[EventEnvelope]:
        return await self._dead_letters().read(count, group_name)

    async def delete_dlq(self, message_id: str, group_name: str | None = None) -> None:
        await self._dead_letters().delete(message_id, group_name)
