"""Delivery guarantees of consumer groups: retries, leases, acks, shutdown,
recovery, idempotency and dead-letter replay (the review's findings)."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from event_bus.bus import EventBus
from event_bus.config import EventBusConfig
from event_bus.consumer.group import ConsumerGroup, ConsumerGroupConfig
from event_bus.consumer.retry import FixedDelay
from event_bus.core.event import Event
from event_bus.testing.fake_transport import InMemoryTransport
from tests.helpers import FakeMetrics


async def eventually(predicate, within: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def fast(**overrides: object) -> ConsumerGroupConfig:
    """Short timings so tests run quickly."""
    settings: dict[str, object] = {
        "block_ms": 20,
        "claim_interval": 0.02,
        "claim_idle_ms": 300,
        "retry_policy": FixedDelay(0.05),
        "max_retries": 3,
        **overrides,
    }
    return ConsumerGroupConfig(**settings)  # type: ignore[arg-type]


async def test_a_batch_handlers_retries_get_a_list() -> None:
    transport = InMemoryTransport()
    calls: list[str] = []

    async def handle(events: list[Event]) -> None:
        calls.append(type(events).__name__)
        if len(calls) == 1:
            raise RuntimeError("downstream unavailable")

    bus = EventBus(transport)
    bus.create_consumer_group(["orders.created"], "g", handle, fast())
    async with bus:
        await bus.publish("orders.created", {"n": 1})
        await eventually(lambda: len(calls) >= 2)
    assert calls[:2] == ["list", "list"]
    assert transport._dlq == []


async def test_a_failure_doesnt_hold_up_the_next_message() -> None:
    """The retry waits for its delay off the worker: later messages go on."""
    transport = InMemoryTransport()
    seen: list[tuple[str, float]] = []
    loop = asyncio.get_running_loop()

    async def handle(event: Event) -> None:
        seen.append((event.data["id"], loop.time()))
        if event.data["id"] == "a" and sum(1 for i, _ in seen if i == "a") == 1:
            raise RuntimeError("first attempt fails")

    bus = EventBus(transport)
    config = fast(concurrency=1, batch_size=1, retry_policy=FixedDelay(0.25))
    bus.create_consumer_group(["t"], "g", handle, config)
    async with bus:
        started = loop.time()
        await bus.publish("t", {"id": "a"})
        await bus.publish("t", {"id": "b"})
        await eventually(lambda: [i for i, _ in seen].count("a") == 2)
    times = {i: t - started for i, t in seen}
    first_b = next(t for i, t in seen if i == "b") - started
    retried_a = [t for i, t in seen if i == "a"][1] - started
    assert first_b < 0.2, "b waited behind a's retry"
    assert 0.2 <= retried_a < 1.0, f"a retried after {retried_a:.2f}s"
    assert set(times) == {"a", "b"}


async def test_an_ack_failure_isnt_a_handler_failure() -> None:
    class AckFails(InMemoryTransport):
        async def ack_many(self, topic: str, group_name: str, message_ids: list[str]) -> None:
            raise ConnectionError("ack lost")

    transport = AckFails()
    metrics = FakeMetrics()
    handled = 0

    async def handle(event: Event) -> None:
        nonlocal handled
        handled += 1

    bus = EventBus(transport, metrics=metrics)
    bus.create_consumer_group(["t"], "g", handle, fast(max_retries=0))
    async with bus:
        await bus.publish("t", {})
        await eventually(lambda: bool(metrics.find("increment", "events.ack_failed")))
    assert handled == 1
    assert metrics.find("increment", "events.failed") == []
    assert transport._dlq == []  # a processed event is never dead-lettered


async def test_leases_are_renewed_so_a_slow_batch_isnt_taken_over() -> None:
    """Two replicas, a slow handler and a batch taking longer than the lease:
    every event is still handled exactly once."""
    transport = InMemoryTransport()
    await transport.connect()
    counts: Counter[int] = Counter()

    async def slow(event: Event) -> None:
        counts[event.data["n"]] += 1
        await asyncio.sleep(0.05)

    def replica(name: str) -> ConsumerGroup:
        return ConsumerGroup(
            transport,
            ["t"],
            "g",
            slow,
            fast(consumer_name=name, batch_size=10, claim_idle_ms=150, heartbeat_interval=0.03),
        )

    a, b = replica("A"), replica("B")
    await a.start()
    for n in range(10):
        await transport.publish(Event(topic="t", data={"n": n}))
    await asyncio.sleep(0.05)  # A takes the whole batch: 0.5 s of work
    await b.start()
    await eventually(lambda: len(counts) == 10)
    await asyncio.sleep(0.3)
    await a.stop()
    await b.stop()
    assert all(c == 1 for c in counts.values()), dict(counts)


async def test_stop_finishes_the_message_in_hand_and_hands_back_the_rest() -> None:
    transport = InMemoryTransport()
    await transport.connect()
    finished: list[int] = []

    async def slow(event: Event) -> None:
        await asyncio.sleep(0.1)
        finished.append(event.data["n"])

    cg = ConsumerGroup(transport, ["t"], "g", slow, fast(batch_size=5, claim_idle_ms=10_000))
    await cg.start()
    for n in range(5):
        await transport.publish(Event(topic="t", data={"n": n}))
    await eventually(lambda: len(finished) >= 1)
    await cg.stop(timeout=2.0)

    handled = len(finished)
    assert 1 <= handled < 5
    # The rest are claimable right away, not after the 10 s lease.
    rest = await transport.claim_idle("t", "g", "other", min_idle_ms=10_000)
    assert len(rest) == 5 - handled


async def test_a_group_that_disappears_is_recreated() -> None:
    transport = InMemoryTransport()
    received: list[int] = []

    async def handle(event: Event) -> None:
        received.append(event.data["n"])

    bus = EventBus(transport)
    bus.create_consumer_group(["t"], "g", handle, fast())
    async with bus:
        await bus.publish("t", {"n": 1})
        await eventually(lambda: received == [1])
        transport.reset()  # the stream and its group are gone (e.g. evicted)
        await asyncio.sleep(0.1)
        await bus.publish("t", {"n": 2})
        await eventually(lambda: received == [1, 2])


async def test_idempotency_handles_a_republished_event_once_per_group() -> None:
    transport = InMemoryTransport()
    handled: Counter[str] = Counter()

    async def first(event: Event) -> None:
        handled["first"] += 1

    async def second(event: Event) -> None:
        handled["second"] += 1

    bus = EventBus(transport, EventBusConfig(enable_idempotency=True))
    bus.create_consumer_group(["t"], "first", first, fast())
    bus.create_consumer_group(["t"], "second", second, fast())
    async with bus:
        await bus.publish("t", {}, event_id="evt-1")
        await bus.publish("t", {}, event_id="evt-1")  # the publisher retried
        await eventually(lambda: handled["first"] >= 1 and handled["second"] >= 1)
        await asyncio.sleep(0.2)
    assert handled == {"first": 1, "second": 1}


async def test_idempotency_lets_a_failed_event_be_retried() -> None:
    transport = InMemoryTransport()
    attempts = 0

    async def flaky(event: Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first attempt fails")

    bus = EventBus(transport, EventBusConfig(enable_idempotency=True))
    bus.create_consumer_group(["t"], "g", flaky, fast())
    async with bus:
        await bus.publish("t", {}, event_id="evt-1")
        await eventually(lambda: attempts == 2)


async def test_dead_letters_are_replayed_into_the_failing_group_only() -> None:
    transport = InMemoryTransport()
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
        await eventually(lambda: len(transport._dlq) == 1 and handled["healthy"] == 1)
        [(letter, error, group)] = transport._dlq
        assert group == "broken" and error.startswith("RuntimeError: bug")

        fixed = True
        assert await bus.replay_dead_letters("broken") == (1, 0)
        await asyncio.sleep(0.1)
    assert handled == {"broken": 2, "healthy": 1}
    assert transport._dlq == []
    with pytest.raises(KeyError):
        await bus.replay_dead_letters("nobody")


async def test_the_reclaimer_trims_what_every_group_consumed() -> None:
    transport = InMemoryTransport()
    done = 0

    async def handle(event: Event) -> None:
        nonlocal done
        done += 1

    bus = EventBus(transport)
    bus.create_consumer_group(["t"], "g", handle, fast(trim_interval=0.05))
    async with bus:
        for n in range(5):
            await bus.publish("t", {"n": n})
        await eventually(lambda: done == 5)
        await eventually(lambda: transport.stream_length("t") == 0)


async def test_an_event_past_its_retries_is_dead_lettered_with_its_error() -> None:
    transport = InMemoryTransport()
    metrics = FakeMetrics()

    async def always_fail(event: Event) -> None:
        raise ValueError("nope")

    bus = EventBus(transport, metrics=metrics)
    bus.create_consumer_group(["t"], "g", always_fail, fast(max_retries=2))
    async with bus:
        await bus.publish("t", {})
        await eventually(lambda: len(transport._dlq) == 1)
    [(letter, error, group)] = transport._dlq
    assert (group, error) == ("g", "ValueError: nope")
    assert letter.event.metadata.retry_count == 2
    assert len(metrics.find("increment", "events.failed")) == 3
    assert len(metrics.find("increment", "events.dead_lettered")) == 1
