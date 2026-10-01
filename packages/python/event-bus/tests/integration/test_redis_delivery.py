"""Delivery against a real Redis: the code review's reproductions, now
regression tests. Skipped unless EVENT_BUS_TEST_REDIS_URL names a Redis
(``make event-bus-test-redis`` starts the shared one). Each test works under
a key prefix of its own and deletes it afterwards."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from event_bus.bus import EventBus
from event_bus.config import EventBusConfig, RedisConfig
from event_bus.consumer.group import ConsumerGroup, ConsumerGroupConfig
from event_bus.consumer.retry import FixedDelay
from event_bus.core.event import Event
from event_bus.transport.redis.transport import RedisTransport

URL = os.environ.get("EVENT_BUS_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set EVENT_BUS_TEST_REDIS_URL to run")


@pytest.fixture
async def prefix() -> AsyncIterator[str]:
    name = f"ebtest-{uuid.uuid4().hex[:8]}"
    yield name
    client = aioredis.from_url(URL or "")
    keys = [key async for key in client.scan_iter(f"{name}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def config(prefix: str, **overrides: object) -> RedisConfig:
    return RedisConfig(url=URL or "", key_prefix=prefix, **overrides)  # type: ignore[arg-type]


def fast(**overrides: object) -> ConsumerGroupConfig:
    settings: dict[str, object] = {
        "block_ms": 50,
        "claim_interval": 0.05,
        "claim_idle_ms": 500,
        "retry_policy": FixedDelay(0.05),
        "max_retries": 2,
        **overrides,
    }
    return ConsumerGroupConfig(**settings)  # type: ignore[arg-type]


async def eventually(predicate, within: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_a_publish_burst_queues_for_connections_instead_of_failing(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    async with EventBus(transport) as bus:
        results = await asyncio.gather(
            *(bus.publish("orders.created", {"n": i}) for i in range(200)),
            return_exceptions=True,
        )
        assert [r for r in results if isinstance(r, BaseException)] == []
        assert transport._conn_mgr.circuit_breaker.state == "closed"


async def test_more_workers_than_blocking_connections_just_wait(prefix: str) -> None:
    transport = RedisTransport(config(prefix, max_blocking_connections=4))
    handled: Counter[str] = Counter()

    async def handle(event: Event) -> None:
        handled[event.data["g"]] += 1

    bus = EventBus(transport)
    for g in range(3):
        bus.create_consumer_group(["t"], f"g{g}", handle, fast(concurrency=4))
    async with bus:
        for g in range(3):
            await bus.publish("t", {"g": f"g{g}"})
        await eventually(lambda: sum(handled.values()) == 9)


async def test_a_malformed_entry_is_dead_lettered_and_its_batch_goes_on(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    handled: list[int] = []

    async def handle(event: Event) -> None:
        handled.append(event.data["n"])

    bus = EventBus(transport)
    bus.create_consumer_group(["t"], "g", handle, fast())
    raw = aioredis.from_url(URL or "")
    async with bus:
        await raw.xadd(f"{prefix}:stream:t", {"topic": "t", "data": "{not json", "meta": "{}"})
        await bus.publish("t", {"n": 1})
        await eventually(lambda: handled == [1])
        [letter] = await transport.read_dlq(group_name="g")
        assert letter.event.data["raw"]["data"] == "{not json"
        assert await transport.get_pending("t", "g", min_idle_ms=0) == []
    await raw.aclose()


async def test_a_slow_batch_isnt_taken_over_by_another_replica(prefix: str) -> None:
    counts: Counter[int] = Counter()

    async def slow(event: Event) -> None:
        counts[event.data["n"]] += 1
        await asyncio.sleep(0.1)

    t1, t2 = RedisTransport(config(prefix)), RedisTransport(config(prefix))
    await t1.connect()
    await t2.connect()
    settings = {"batch_size": 10, "claim_idle_ms": 300, "heartbeat_interval": 0.05}
    a = ConsumerGroup(t1, ["t"], "g", slow, fast(consumer_name="A", **settings))
    b = ConsumerGroup(t2, ["t"], "g", slow, fast(consumer_name="B", **settings))
    await a.start()
    for n in range(10):
        await t1.publish(Event(topic="t", data={"n": n}))
    await asyncio.sleep(0.2)  # A holds the whole batch: 1 s of work
    await b.start()
    await eventually(lambda: len(counts) == 10)
    await asyncio.sleep(0.5)
    await a.stop()
    await b.stop()
    await t1.disconnect()
    await t2.disconnect()
    assert all(c == 1 for c in counts.values()), dict(counts)


async def test_dead_letters_are_replayed_into_their_group_only(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    handled: Counter[str] = Counter()
    fixed = False

    async def broken(event: Event) -> None:
        handled["broken"] += 1
        if not fixed:
            raise RuntimeError("bug")

    async def healthy(event: Event) -> None:
        handled["healthy"] += 1

    bus = EventBus(transport)
    bus.create_consumer_group(["t"], "broken", broken, fast(max_retries=0))
    bus.create_consumer_group(["t"], "healthy", healthy, fast())
    async with bus:
        await bus.publish("t", {"x": 1})

        async def dead_lettered() -> int:
            return len(await transport.read_dlq(group_name="broken"))

        for _ in range(100):
            if await dead_lettered() == 1 and handled["healthy"] == 1:
                break
            await asyncio.sleep(0.05)
        fixed = True
        assert await bus.replay_dead_letters("broken") == (1, 0)
        await asyncio.sleep(0.2)
        assert await dead_lettered() == 0
    assert handled == {"broken": 2, "healthy": 1}


async def test_trimming_keeps_what_a_slow_group_still_needs(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    await transport.connect()
    await transport.create_consumer_group("t", "fast")
    await transport.create_consumer_group("t", "slow")
    for n in range(4):
        await transport.publish(Event(topic="t", data={"n": n}))
    done = await transport.read_group("t", "fast", "c", count=10, block_ms=0)
    await transport.ack_many("t", "fast", [e.stream_id for e in done])
    slow = await transport.read_group("t", "slow", "c", count=2, block_ms=0)
    await transport.ack("t", "slow", slow[0].stream_id)

    await transport.trim_consumed("t")
    rest = await transport.read_group("t", "slow", "c", count=10, block_ms=0)
    assert [e.event.data["n"] for e in rest] == [2, 3]
    pending = await transport.get_pending("t", "slow", min_idle_ms=0)
    assert [p["message_id"].decode() for p in pending][0] == slow[1].stream_id
    await transport.disconnect()


async def test_events_published_right_after_start_reach_subscribers(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    got = asyncio.Event()
    bus = EventBus(transport)

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        got.set()

    async with bus:
        await bus.publish("orders.created", {"id": 1})
        await asyncio.wait_for(got.wait(), timeout=3.0)


async def test_idempotency_uses_redis_to_skip_a_republished_event(prefix: str) -> None:
    transport = RedisTransport(config(prefix))
    handled = 0

    async def handle(event: Event) -> None:
        nonlocal handled
        handled += 1

    bus = EventBus(transport, EventBusConfig(enable_idempotency=True))
    bus.create_consumer_group(["t"], "g", handle, fast())
    async with bus:
        await bus.publish("t", {}, event_id="evt-1")
        await bus.publish("t", {}, event_id="evt-1")
        await eventually(lambda: handled >= 1)
        await asyncio.sleep(0.3)
    assert handled == 1
