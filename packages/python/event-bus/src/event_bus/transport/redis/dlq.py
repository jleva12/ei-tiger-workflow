from __future__ import annotations

import logging
import time
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.client import Pipeline

from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.serialization import Serializer
from event_bus.interfaces.transport import UNDECODABLE_HEADER

logger = logging.getLogger(__name__)

# Warn once a DLQ reaches this share of its length cap.
CAP_WARNING_RATIO = 0.9
# ...at most this often per DLQ.
CAP_WARNING_INTERVAL = 300.0


class DeadLetterQueue:
    """
    Dead letters, one Redis stream per consumer group
    (``<prefix>:dlq:<group>``; ``<prefix>:dlq`` for none), so a failed event
    is replayed into the group that failed it only, never into groups that
    already processed it.

    Each entry keeps the event (or, for an entry that couldn't be decoded,
    its raw fields), the error, the group, and the original stream and ID.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        serializer: Serializer,
        key_prefix: str = "eventbus",
        max_length: int | None = 50_000,
    ) -> None:
        self._client = client
        self._serializer = serializer
        self._key_prefix = key_prefix
        self._max_length = max_length
        self._last_cap_warning: dict[str, float] = {}

    def stream_key(self, group_name: str | None = None) -> str:
        base = f"{self._key_prefix}:dlq"
        return f"{base}:{group_name}" if group_name else base

    def _payload(
        self,
        fields: dict[bytes, bytes],
        error: str,
        group_name: str | None,
        stream_name: str,
        stream_id: str,
    ) -> dict[bytes, bytes]:
        return {
            **fields,
            b"error": error.encode(),
            b"group": (group_name or "").encode(),
            b"original_stream": stream_name.encode(),
            b"original_id": stream_id.encode(),
            b"dlq_timestamp": str(time.time()).encode(),
        }

    def event_fields(self, event: Event) -> dict[bytes, bytes]:
        return {
            b"topic": event.topic.encode(),
            b"data": self._serializer.serialize(event.data),
            b"meta": self._serializer.serialize(
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
            ),
        }

    def add_to_pipeline(
        self,
        pipe: Pipeline,
        fields: dict[bytes, bytes],
        error: str,
        group_name: str | None,
        stream_name: str,
        stream_id: str,
    ) -> None:
        """Queue the dead letter's XADD on a pipeline (e.g. with the XACK)."""
        pipe.xadd(
            self.stream_key(group_name),
            self._payload(fields, error, group_name, stream_name, stream_id),  # type: ignore[arg-type]
            maxlen=self._max_length,
            approximate=True,
        )

    async def send(self, envelope: EventEnvelope, error: str, group_name: str | None = None) -> str:
        """Add a dead letter (without acking the original)."""
        msg_id = await self._client.xadd(
            self.stream_key(group_name),
            self._payload(  # type: ignore[arg-type]
                self.event_fields(envelope.event),
                error,
                group_name,
                envelope.stream_name,
                envelope.stream_id,
            ),
            maxlen=self._max_length,
            approximate=True,
        )
        result = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        logger.info(
            "Event %s sent to DLQ %s (dlq_id=%s): %s",
            envelope.event.event_id,
            self.stream_key(group_name),
            result,
            error,
        )
        await self.check_capacity(group_name)
        return result

    async def check_capacity(self, group_name: str | None) -> None:
        """Warn (rate-limited) when the DLQ nears its cap: past it, the
        oldest dead letters are dropped."""
        if not self._max_length:
            return
        key = self.stream_key(group_name)
        length = int(await self._client.xlen(key))
        now = time.monotonic()
        if length >= self._max_length * CAP_WARNING_RATIO and (
            now - self._last_cap_warning.get(key, -CAP_WARNING_INTERVAL) >= CAP_WARNING_INTERVAL
        ):
            self._last_cap_warning[key] = now
            logger.warning(
                "DLQ %s holds %d of at most %d entries: past the cap the oldest are dropped",
                key,
                length,
                self._max_length,
            )

    async def read(self, count: int = 10, group_name: str | None = None) -> list[EventEnvelope]:
        """
        Dead letters, oldest first; each envelope's ``stream_id`` is the DLQ
        entry's ID. An entry that couldn't be decoded when it was read from
        its stream comes back with its raw fields under ``data["raw"]``.
        """
        results = await self._client.xrange(self.stream_key(group_name), count=count)
        envelopes = []
        for msg_id, fields in results:
            envelopes.append(
                EventEnvelope(
                    event=self._decode(fields),
                    stream_id=msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                    stream_name=self.stream_key(group_name),
                )
            )
        return envelopes

    def _decode(self, fields: dict[bytes, bytes]) -> Event:
        try:
            meta = self._serializer.deserialize(fields[b"meta"])
            return Event(
                topic=fields[b"topic"].decode(),
                data=self._serializer.deserialize(fields[b"data"]),
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
        except Exception:
            raw: dict[str, Any] = {
                k.decode(errors="replace"): v.decode(errors="replace") for k, v in fields.items()
            }
            topic = raw.get("topic", "") or "unknown"
            return Event(
                topic=topic,
                data={"raw": raw},
                metadata=EventMetadata(headers={UNDECODABLE_HEADER: "1"}),
            )

    async def delete(self, message_id: str, group_name: str | None = None) -> None:
        await self._client.xdel(self.stream_key(group_name), message_id)
