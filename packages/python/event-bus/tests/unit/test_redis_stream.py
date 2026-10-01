"""Tests for Redis stream transport helpers."""

from __future__ import annotations

from typing import Any

import pytest
import redis.exceptions as redis_errors
from redis.exceptions import ResponseError

from event_bus.core.serialization import JsonSerializer
from event_bus.interfaces.transport import NoGroupError
from event_bus.transport.redis.connection import is_connectivity_error
from event_bus.transport.redis.pubsub import decode_frame, encode_frame
from event_bus.transport.redis.stream import (
    RedisStreamManager,
    format_stream_id,
    parse_stream_id,
)

SERIALIZER = JsonSerializer()


def _fields(event_id: str = "evt-1") -> dict[bytes, bytes]:
    return {
        b"topic": b"orders.created",
        b"data": SERIALIZER.serialize({"id": "1"}),
        b"meta": SERIALIZER.serialize({"event_id": event_id, "timestamp": 1.0, "retry_count": 0}),
    }


class FakeRedisStreamClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int,
        block: int | None,
    ) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "groupname": groupname,
                "consumername": consumername,
                "streams": streams,
                "count": count,
                "block": block,
            }
        )
        return [(b"eventbus:stream:orders.created", [(b"1-0", _fields())])]


def _manager(client: Any) -> RedisStreamManager:
    return RedisStreamManager(client=client, blocking_client=client, serializer=SERIALIZER)


async def test_read_raw_maps_topics_and_returns_raw_entries() -> None:
    client = FakeRedisStreamClient()
    manager = _manager(client)

    entries = await manager.read_raw(
        ["orders.created", "payments.created"], "workers", "consumer-1", count=5, block_ms=100
    )

    assert client.calls[0]["streams"] == {
        "eventbus:stream:orders.created": ">",
        "eventbus:stream:payments.created": ">",
    }
    assert client.calls[0]["count"] == 5
    assert client.calls[0]["block"] == 100
    [(stream, msg_id, fields)] = entries
    assert (stream, msg_id) == ("eventbus:stream:orders.created", "1-0")

    envelope = manager.decode(stream, msg_id, fields, delivery_count=1)
    assert envelope.event.topic == "orders.created"
    assert envelope.event.data == {"id": "1"}
    assert envelope.stream_id == "1-0"
    assert envelope.delivery_count == 1


async def test_a_missing_group_raises_nogroup() -> None:
    client = FakeRedisStreamClient(ResponseError("NOGROUP No such key 'x' or consumer group 'g'"))
    with pytest.raises(NoGroupError):
        await _manager(client).read_raw(["x"], "g", "c")


def test_decode_raises_on_a_malformed_entry() -> None:
    manager = _manager(FakeRedisStreamClient())
    with pytest.raises(Exception):  # noqa: B017 - any decode error is dead-lettered
        manager.decode("s", "1-0", {b"topic": b"t", b"data": b"{not json", b"meta": b"{}"})


def test_stream_ids_parse_and_format() -> None:
    assert parse_stream_id(b"1700000000000-3") == (1700000000000, 3)
    assert parse_stream_id("5") == (5, 0)
    assert format_stream_id((12, 4)) == "12-4"


def test_pubsub_frames_round_trip_and_reject_bad_lengths() -> None:
    frame = encode_frame(b"orders.created", b'{"id":"1"}', b'{"event_id":"e"}')
    assert decode_frame(frame) == (b"orders.created", b'{"id":"1"}', b'{"event_id":"e"}')
    assert decode_frame(b'{"topic": "legacy"}') is None
    with pytest.raises(ValueError):
        decode_frame(frame + b"extra")


@pytest.mark.parametrize(
    ("error", "counts"),
    [
        (redis_errors.ConnectionError("Connection refused"), True),
        (redis_errors.TimeoutError("Timeout reading"), True),
        (OSError("network down"), True),
        (redis_errors.MaxConnectionsError("Too many connections"), False),
        (redis_errors.ConnectionError("No connection available."), False),
        (redis_errors.ResponseError("WRONGTYPE"), False),
        (ValueError("bad payload"), False),
    ],
)
def test_only_connectivity_errors_count_against_the_breaker(error: Exception, counts: bool) -> None:
    assert is_connectivity_error(error) is counts
