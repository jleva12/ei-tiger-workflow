"""Tests for event_bus/consumer/group.py — error/retry/DLQ paths."""

import asyncio

import pytest

from event_bus.bus import EventBus
from event_bus.config import EventBusConfig
from event_bus.consumer.group import ConsumerGroup, ConsumerGroupConfig
from event_bus.consumer.retry import FixedDelay
from event_bus.core.event import Event, EventMetadata
from event_bus.testing.fake_transport import InMemoryTransport
from tests.helpers import FakeMetrics


@pytest.fixture
def transport() -> InMemoryTransport:
    return InMemoryTransport()


@pytest.fixture
def metrics() -> FakeMetrics:
    return FakeMetrics()


class TestSingleHandlerFailure:
    async def test_retries_on_failure(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """Handler failure triggers retry delay (sleep), event not DLQ'd yet."""
        call_count = 0

        async def handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("transient error")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.05),
            max_retries=3,
        )
        bus = EventBus(transport, EventBusConfig(), metrics=metrics)
        bus.create_consumer_group(["t"], "g", handler=handler, config=config)

        async with bus:
            await bus.publish("t", data={"x": 1})
            await asyncio.sleep(0.5)

        # Handler was called (at least once for the initial attempt)
        assert call_count >= 1
        failed = metrics.find("increment", "events.failed")
        assert len(failed) >= 1

    async def test_dlq_after_max_retries(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """Event goes to DLQ after max_retries exceeded."""

        async def always_fail(event: Event) -> None:
            raise RuntimeError("permanent error")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=0,  # immediately DLQ
        )

        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=always_fail,
            config=config,
            metrics=metrics,
        )

        await transport.connect()
        # Publish an event with retry_count already at max
        event = Event(
            topic="t",
            data={"x": 1},
            metadata=EventMetadata(retry_count=0),
        )
        await transport.publish(event)
        await transport.create_consumer_group("t", "g")

        await cg.start()
        await asyncio.sleep(0.3)
        await cg.stop()

        dlq = metrics.find("increment", "events.dead_lettered")
        assert len(dlq) >= 1
        assert len(transport._dlq) >= 1

    async def test_poison_message_reaches_dlq_after_reclaims(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """Repeated reclaims advance retry count until the message is DLQ'd."""
        retry_counts: list[int] = []

        async def always_fail(event: Event) -> None:
            retry_counts.append(event.metadata.retry_count)
            raise RuntimeError("poison")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=20,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=2,
            claim_idle_ms=1,
            claim_interval=0.02,
        )
        bus = EventBus(transport, EventBusConfig(), metrics=metrics)
        bus.create_consumer_group(["t"], "g", handler=always_fail, config=config)

        dlq_retry_count = -1
        async with bus:
            await bus.publish("t", data={"x": 1})
            for _ in range(50):
                if transport._dlq:
                    break
                await asyncio.sleep(0.02)
            assert len(transport._dlq) == 1
            dlq_retry_count = transport._dlq[0][0].event.metadata.retry_count

        assert retry_counts[:3] == [0, 1, 2]
        assert dlq_retry_count == 2
        dlq = metrics.find("increment", "events.dead_lettered")
        assert len(dlq) == 1


class TestBatchHandler:
    async def test_batch_success_acks_all(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """All envelopes processed on batch success."""
        received_batches: list[list[Event]] = []

        async def handler(events: list[Event]) -> None:
            received_batches.append(events)

        config = ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=100)
        bus = EventBus(transport, EventBusConfig(), metrics=metrics)
        bus.create_consumer_group(["t"], "batch-g", handler=handler, config=config)

        async with bus:
            await bus.publish("t", data={"x": 1})
            await bus.publish("t", data={"x": 2})
            await asyncio.sleep(0.5)

        assert len(received_batches) >= 1
        total_events = sum(len(b) for b in received_batches)
        assert total_events == 2
        processed = metrics.find("increment", "events.processed")
        assert len(processed) >= 1

    async def test_batch_failure_dlq_for_exhausted(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """Exhausted events go to DLQ on batch failure."""

        async def always_fail(events: list[Event]) -> None:
            raise RuntimeError("batch failed")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=10,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=0,  # immediately DLQ
        )

        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=always_fail,
            config=config,
            metrics=metrics,
        )

        await transport.connect()
        # Publish event with retry_count already at max_retries
        event = Event(
            topic="t",
            data={"x": 1},
            metadata=EventMetadata(retry_count=0),
        )
        await transport.publish(event)
        await transport.create_consumer_group("t", "g")

        await cg.start()
        await asyncio.sleep(0.3)
        await cg.stop()

        failed = metrics.find("increment", "events.failed")
        assert len(failed) >= 1
        dlq = metrics.find("increment", "events.dead_lettered")
        assert len(dlq) >= 1


class TestWorkerLoop:
    async def test_continues_after_error(self, transport: InMemoryTransport) -> None:
        """Worker loop doesn't crash on handler error — continues processing."""
        call_count = 0

        async def handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=5,
        )
        bus = EventBus(transport, EventBusConfig())
        bus.create_consumer_group(["t"], "g", handler=handler, config=config)

        async with bus:
            await bus.publish("t", data={"x": 1})
            await bus.publish("t", data={"x": 2})
            await asyncio.sleep(0.5)

        # Both events were attempted
        assert call_count >= 2

    async def test_cancellation(self, transport: InMemoryTransport) -> None:
        """CancelledError exits cleanly."""

        async def handler(event: Event) -> None:
            pass

        config = ConsumerGroupConfig(concurrency=1, block_ms=100)
        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=handler,
            config=config,
        )

        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await cg.start()
        await asyncio.sleep(0.1)
        await cg.stop()  # should cancel workers cleanly


class TestStopBehavior:
    async def test_stop_cancels_all_tasks(self, transport: InMemoryTransport) -> None:
        """stop() cancels workers + reclaimer."""

        async def handler(event: Event) -> None:
            pass

        config = ConsumerGroupConfig(concurrency=2, block_ms=100)
        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=handler,
            config=config,
        )

        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await cg.start()

        assert len(cg._workers) == 2
        assert cg._reclaimer_task is not None

        await cg.stop()

        assert len(cg._workers) == 0
        assert cg._reclaimer_task is None

    async def test_stop_resets_stop_event(self, transport: InMemoryTransport) -> None:
        """Group can be restarted after stop."""

        async def handler(event: Event) -> None:
            pass

        config = ConsumerGroupConfig(concurrency=1, block_ms=100)
        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=handler,
            config=config,
        )

        await transport.connect()
        await transport.create_consumer_group("t", "g")

        # First start/stop
        await cg.start()
        await cg.stop()
        assert cg._stopping.is_set() is True

        # Restart
        await cg.start()
        assert cg._stopping.is_set() is False
        assert len(cg._workers) == 1
        await cg.stop()


class TestMultipleTopics:
    async def test_multiple_topics(self, transport: InMemoryTransport) -> None:
        """Worker reads from all topics."""
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        config = ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=100)
        bus = EventBus(transport, EventBusConfig())
        bus.create_consumer_group(["a", "b"], "g", handler=handler, config=config)

        async with bus:
            await bus.publish("a", data={"src": "a"})
            await bus.publish("b", data={"src": "b"})
            await asyncio.sleep(0.5)

        topics = {e.topic for e in received}
        assert "a" in topics
        assert "b" in topics

    async def test_multiple_topics_do_not_block_behind_empty_topic(
        self, transport: InMemoryTransport
    ) -> None:
        """A later topic with queued work is read without per-topic blocking delay."""
        received = asyncio.Event()

        async def handler(event: Event) -> None:
            if event.topic == "b":
                received.set()

        await transport.connect()
        await transport.publish(Event(topic="b", data={"src": "b"}))

        config = ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=200)
        bus = EventBus(transport, EventBusConfig())
        bus.create_consumer_group(["a", "b"], "g", handler=handler, config=config)

        async with bus:
            await asyncio.wait_for(received.wait(), timeout=0.1)


class TestMetrics:
    async def test_increment_on_success(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """events.processed incremented on success."""

        async def handler(event: Event) -> None:
            pass

        config = ConsumerGroupConfig(concurrency=1, batch_size=1, block_ms=100)
        bus = EventBus(transport, EventBusConfig(), metrics=metrics)
        bus.create_consumer_group(["t"], "g", handler=handler, config=config)

        async with bus:
            await bus.publish("t", data={"x": 1})
            await asyncio.sleep(0.3)

        processed = metrics.find("increment", "events.processed")
        assert len(processed) >= 1

    async def test_increment_on_failure(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """events.failed incremented on failure."""

        async def handler(event: Event) -> None:
            raise RuntimeError("fail")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=5,
        )
        bus = EventBus(transport, EventBusConfig(), metrics=metrics)
        bus.create_consumer_group(["t"], "g", handler=handler, config=config)

        async with bus:
            await bus.publish("t", data={"x": 1})
            await asyncio.sleep(0.3)

        failed = metrics.find("increment", "events.failed")
        assert len(failed) >= 1

    async def test_increment_on_dlq(
        self, transport: InMemoryTransport, metrics: FakeMetrics
    ) -> None:
        """events.dead_lettered incremented when event goes to DLQ."""

        async def handler(event: Event) -> None:
            raise RuntimeError("fail")

        config = ConsumerGroupConfig(
            concurrency=1,
            batch_size=1,
            block_ms=100,
            retry_policy=FixedDelay(delay=0.01),
            max_retries=0,
        )

        cg = ConsumerGroup(
            transport=transport,
            topics=["t"],
            group_name="g",
            handler=handler,
            config=config,
            metrics=metrics,
        )

        await transport.connect()
        event = Event(topic="t", data={"x": 1}, metadata=EventMetadata(retry_count=0))
        await transport.publish(event)
        await transport.create_consumer_group("t", "g")

        await cg.start()
        await asyncio.sleep(0.3)
        await cg.stop()

        dlq = metrics.find("increment", "events.dead_lettered")
        assert len(dlq) >= 1
