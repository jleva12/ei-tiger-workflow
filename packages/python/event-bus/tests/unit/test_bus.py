import asyncio
from collections.abc import AsyncIterator

import pytest

from event_bus.bus import EventBus
from event_bus.config import EventBusConfig
from event_bus.consumer.group import ConsumerGroupConfig, _is_batch_handler
from event_bus.core.event import Event, EventEnvelope
from event_bus.core.topic import TopicPattern
from event_bus.testing.fake_transport import InMemoryTransport


@pytest.fixture
def transport() -> InMemoryTransport:
    return InMemoryTransport()


@pytest.fixture
def bus(transport: InMemoryTransport) -> EventBus:
    return EventBus(transport, EventBusConfig())


@pytest.mark.asyncio
async def test_publish(bus: EventBus, transport: InMemoryTransport) -> None:
    async with bus:
        stream_id = await bus.publish(
            topic="orders.created",
            data={"order_id": "test-1"},
            source="test",
        )
        assert stream_id == "1-0"
        assert len(transport.published_events) == 1
        assert transport.published_events[0].topic == "orders.created"
        assert transport.published_events[0].data == {"order_id": "test-1"}


@pytest.mark.asyncio
async def test_publish_event(bus: EventBus, transport: InMemoryTransport) -> None:
    async with bus:
        event = Event(topic="test.topic", data={"key": "value"})
        stream_id = await bus.publish_event(event)
        assert stream_id == "1-0"
        assert len(transport.published_events) == 1


@pytest.mark.asyncio
async def test_publish_many(bus: EventBus, transport: InMemoryTransport) -> None:
    async with bus:
        events = [
            Event(topic="a.b", data={"i": 1}),
            Event(topic="c.d", data={"i": 2}),
        ]
        ids = await bus.publish_many(events)
        assert len(ids) == 2
        assert len(transport.published_events) == 2


@pytest.mark.asyncio
async def test_consumer_group_decorator(
    transport: InMemoryTransport,
) -> None:
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    @bus.consumer_group(
        ["orders.created"],
        "test-group",
        config=ConsumerGroupConfig(concurrency=1, block_ms=100),
    )
    async def handler(event: Event) -> None:
        received.append(event)

    async with bus:
        await bus.publish("orders.created", data={"order_id": "1"})
        await asyncio.sleep(0.3)

    assert len(received) == 1
    assert received[0].data["order_id"] == "1"


@pytest.mark.asyncio
async def test_create_consumer_group_programmatic(
    transport: InMemoryTransport,
) -> None:
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.create_consumer_group(
        topics=["test.topic"],
        group_name="test-cg",
        handler=handler,
        config=ConsumerGroupConfig(concurrency=1, block_ms=100),
    )

    async with bus:
        await bus.publish("test.topic", data={"x": 1})
        await asyncio.sleep(0.3)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_health_check(bus: EventBus) -> None:
    async with bus:
        health = await bus.health_check()
        assert health["healthy"] is True
        assert health["transport"] is True
        assert health["started"] is True
        assert health["pubsub_listener_running"] is False
        assert health["pubsub_listener_error"] is None


@pytest.mark.asyncio
async def test_health_check_before_start(bus: EventBus) -> None:
    health = await bus.health_check()
    assert health["healthy"] is False
    assert health["started"] is False


@pytest.mark.asyncio
async def test_middleware(
    transport: InMemoryTransport,
) -> None:
    bus = EventBus(transport, EventBusConfig())
    publish_log: list[str] = []

    class LoggingMiddleware:
        async def before_publish(self, event: Event) -> Event:
            publish_log.append(f"before:{event.topic}")
            return event

        async def after_publish(self, event: Event, stream_id: str) -> None:
            publish_log.append(f"after:{event.topic}:{stream_id}")

    bus.use(LoggingMiddleware())

    async with bus:
        await bus.publish("test.mw", data={})

    assert publish_log == ["before:test.mw", "after:test.mw:1-0"]


# ---------------------------------------------------------------------------
# Shared listener / push-to-subscriber tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_listener_dispatches_to_subscriber(
    transport: InMemoryTransport,
) -> None:
    """Bus pushes pub/sub events to registered subscribers."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        received.append(event)

    async with bus:
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].topic == "orders.created"


@pytest.mark.asyncio
async def test_shared_listener_pattern_filtering(
    transport: InMemoryTransport,
) -> None:
    """Subscriber only receives events matching its patterns."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        received.append(event)

    async with bus:
        await bus.publish("payments.created", data={})
        await bus.publish("orders.updated", data={"id": "2"})
        await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].topic == "orders.updated"


@pytest.mark.asyncio
async def test_shared_listener_multiple_subscribers(
    transport: InMemoryTransport,
) -> None:
    """Multiple subscribers all receive the same event."""
    bus = EventBus(transport, EventBusConfig())
    received_a: list[Event] = []
    received_b: list[Event] = []

    @bus.subscribe("orders.*")
    async def handler_a(event: Event) -> None:
        received_a.append(event)

    @bus.subscribe("orders.*")
    async def handler_b(event: Event) -> None:
        received_b.append(event)

    async with bus:
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)

    assert len(received_a) == 1
    assert len(received_b) == 1


@pytest.mark.asyncio
async def test_shared_listener_error_isolation(
    transport: InMemoryTransport,
) -> None:
    """One subscriber raising doesn't block others."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    @bus.subscribe("orders.*")
    async def bad_handler(event: Event) -> None:
        raise RuntimeError("boom")

    @bus.subscribe("orders.*")
    async def good_handler(event: Event) -> None:
        received.append(event)

    async with bus:
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_dynamic_add_remove_subscriber(
    transport: InMemoryTransport,
) -> None:
    """Subscribers added/removed at runtime receive/stop receiving events."""
    from event_bus.adapters.base import CallbackSubscriber

    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    sub = CallbackSubscriber(patterns=["orders.*"], handler=handler)

    async with bus:
        # Add dynamically
        bus.add_subscriber(sub)
        await asyncio.sleep(0.05)

        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)
        assert len(received) == 1

        # Remove dynamically
        bus.remove_subscriber(sub)
        await asyncio.sleep(0.05)

        await bus.publish("orders.created", data={"id": "2"})
        await asyncio.sleep(0.1)
        assert len(received) == 1  # no new events
        assert bus._listener_task is None or bus._listener_task.done()


@pytest.mark.asyncio
async def test_custom_subscriber_only_needs_patterns_protocol(
    transport: InMemoryTransport,
) -> None:
    """Custom subscribers don't need a concrete accepts() method."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    class CustomSubscriber:
        def __init__(self) -> None:
            self.patterns = [TopicPattern("orders.*")]

        async def on_event(self, envelope: EventEnvelope) -> None:
            received.append(envelope.event)

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    bus.add_subscriber(CustomSubscriber())

    async with bus:
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_pubsub_listener_recovers_after_transport_error() -> None:
    """The shared listener retries after a transient Pub/Sub failure."""

    class FlakyPubSubTransport(InMemoryTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_subscribe = True

        async def subscribe_pubsub(
            self, patterns: list[str], ready: asyncio.Event | None = None
        ) -> AsyncIterator[EventEnvelope]:
            if self.fail_next_subscribe:
                self.fail_next_subscribe = False
                raise RuntimeError("pubsub boom")
            async for envelope in super().subscribe_pubsub(patterns, ready):
                yield envelope

    transport = FlakyPubSubTransport()
    bus = EventBus(transport, EventBusConfig())
    received = asyncio.Event()

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        received.set()

    async with bus:
        await asyncio.sleep(0.2)
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.wait_for(received.wait(), timeout=1.0)
        health = await bus.health_check()
        assert health["pubsub_listener_running"] is True
        assert health["pubsub_listener_error"] is None


@pytest.mark.asyncio
async def test_health_check_fails_when_pubsub_listener_dead(
    transport: InMemoryTransport,
) -> None:
    """Health is false when subscribers require Pub/Sub but no listener runs."""
    bus = EventBus(transport, EventBusConfig())

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        pass

    async with bus:
        assert bus._listener_task is not None
        bus._listener_task.cancel()
        await asyncio.sleep(0)
        health = await bus.health_check()
        assert health["healthy"] is False
        assert health["pubsub_listener_running"] is False


# ---------------------------------------------------------------------------
# Batch handler detection tests
# ---------------------------------------------------------------------------


def test_is_batch_handler_single_event() -> None:
    """Single Event param is not detected as batch."""

    async def handler(event: Event) -> None: ...

    assert _is_batch_handler(handler) is False


def test_is_batch_handler_list_event() -> None:
    """list[Event] param is detected as batch."""

    async def handler(events: list[Event]) -> None: ...

    assert _is_batch_handler(handler) is True


def test_is_batch_handler_untyped() -> None:
    """Untyped param falls back to single mode."""

    async def handler(events) -> None: ...

    assert _is_batch_handler(handler) is False


def test_is_batch_handler_list_no_args() -> None:
    """Bare list (no type arg) is not detected as batch."""

    async def handler(events: list) -> None: ...

    assert _is_batch_handler(handler) is False


# ---------------------------------------------------------------------------
# Batch consumer group end-to-end tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_group_batch_mode(
    transport: InMemoryTransport,
) -> None:
    """Batch handler receives list[Event] with all events from XREADGROUP."""
    bus = EventBus(transport, EventBusConfig())
    received_batches: list[list[Event]] = []

    @bus.consumer_group(
        ["orders.created"],
        "batch-group",
        config=ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=100),
    )
    async def handler(events: list[Event]) -> None:
        received_batches.append(events)

    async with bus:
        # Publish 3 events before the consumer reads
        await bus.publish("orders.created", data={"order_id": "1"})
        await bus.publish("orders.created", data={"order_id": "2"})
        await bus.publish("orders.created", data={"order_id": "3"})
        await asyncio.sleep(0.5)

    # All 3 should arrive in a single batch call
    assert len(received_batches) == 1
    assert len(received_batches[0]) == 3
    order_ids = [e.data["order_id"] for e in received_batches[0]]
    assert order_ids == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_consumer_group_single_mode_unchanged(
    transport: InMemoryTransport,
) -> None:
    """Single-event handler still receives one Event at a time."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    @bus.consumer_group(
        ["orders.created"],
        "single-group",
        config=ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=100),
    )
    async def handler(event: Event) -> None:
        received.append(event)

    async with bus:
        await bus.publish("orders.created", data={"order_id": "1"})
        await bus.publish("orders.created", data={"order_id": "2"})
        await asyncio.sleep(0.5)

    assert len(received) == 2
    assert received[0].data["order_id"] == "1"
    assert received[1].data["order_id"] == "2"


@pytest.mark.asyncio
async def test_consumer_group_batch_programmatic(
    transport: InMemoryTransport,
) -> None:
    """Batch mode works with create_consumer_group (non-decorator)."""
    bus = EventBus(transport, EventBusConfig())
    received_batches: list[list[Event]] = []

    async def handler(events: list[Event]) -> None:
        received_batches.append(events)

    bus.create_consumer_group(
        topics=["test.batch"],
        group_name="batch-cg",
        handler=handler,
        config=ConsumerGroupConfig(concurrency=1, batch_size=10, block_ms=100),
    )

    async with bus:
        await bus.publish("test.batch", data={"x": 1})
        await bus.publish("test.batch", data={"x": 2})
        await asyncio.sleep(0.5)

    assert len(received_batches) == 1
    assert len(received_batches[0]) == 2


# ---------------------------------------------------------------------------
# Bus edge-case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_with_all_metadata(bus: EventBus, transport: InMemoryTransport) -> None:
    """event_id, source, correlation_id, causation_id, headers all set."""
    async with bus:
        await bus.publish(
            topic="test.meta",
            data={"x": 1},
            event_id="custom-id",
            source="test-source",
            correlation_id="corr-1",
            causation_id="cause-1",
            headers={"x-trace": "abc"},
        )

        event = transport.published_events[0]
        assert event.metadata.event_id == "custom-id"
        assert event.metadata.source == "test-source"
        assert event.metadata.correlation_id == "corr-1"
        assert event.metadata.causation_id == "cause-1"
        assert event.metadata.headers == {"x-trace": "abc"}


@pytest.mark.asyncio
async def test_middleware_transforms_event(
    transport: InMemoryTransport,
) -> None:
    """before_publish can modify the event."""
    bus = EventBus(transport, EventBusConfig())

    class TagMiddleware:
        async def before_publish(self, event: Event) -> Event:
            from event_bus.core.event import EventMetadata

            new_meta = EventMetadata(
                event_id=event.metadata.event_id,
                timestamp=event.metadata.timestamp,
                source="injected-source",
                correlation_id=event.metadata.correlation_id,
                causation_id=event.metadata.causation_id,
                content_type=event.metadata.content_type,
                retry_count=event.metadata.retry_count,
                headers=event.metadata.headers,
            )
            return Event(topic=event.topic, data=event.data, metadata=new_meta)

        async def after_publish(self, event: Event, stream_id: str) -> None:
            pass

    bus.use(TagMiddleware())

    async with bus:
        await bus.publish("test.mw", data={"x": 1})
        assert transport.published_events[0].metadata.source == "injected-source"


@pytest.mark.asyncio
async def test_middleware_on_publish_many(
    transport: InMemoryTransport,
) -> None:
    """Middleware applies to every event in batch."""
    bus = EventBus(transport, EventBusConfig())
    seen: list[str] = []

    class TrackMiddleware:
        async def before_publish(self, event: Event) -> Event:
            seen.append(f"before:{event.topic}")
            return event

        async def after_publish(self, event: Event, stream_id: str) -> None:
            seen.append(f"after:{event.topic}:{stream_id}")

    bus.use(TrackMiddleware())

    async with bus:
        events = [
            Event(topic="a", data={}),
            Event(topic="b", data={}),
        ]
        await bus.publish_many(events)
        assert seen == [
            "before:a",
            "before:b",
            "after:a:1-0",
            "after:b:2-0",
        ]


@pytest.mark.asyncio
async def test_subscribe_direct_handler(
    transport: InMemoryTransport,
) -> None:
    """bus.subscribe('topic', handler=fn) works."""
    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("orders.*", handler=handler)

    async with bus:
        await bus.publish("orders.created", data={"id": "1"})
        await asyncio.sleep(0.1)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_add_subscriber_while_running(
    transport: InMemoryTransport,
) -> None:
    """Subscriber added post-start gets background-started."""
    from event_bus.adapters.base import CallbackSubscriber

    bus = EventBus(transport, EventBusConfig())
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    async with bus:
        sub = CallbackSubscriber(patterns=["test.*"], handler=handler)
        bus.add_subscriber(sub)
        await asyncio.sleep(0.05)

        await bus.publish("test.topic", data={"x": 1})
        await asyncio.sleep(0.1)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_remove_subscriber_not_in_list(
    transport: InMemoryTransport,
) -> None:
    """No error when removing unknown subscriber."""
    from event_bus.adapters.base import CallbackSubscriber

    bus = EventBus(transport, EventBusConfig())

    async def handler(event: Event) -> None:
        pass

    sub = CallbackSubscriber(patterns=["**"], handler=handler)
    # Should not raise
    bus.remove_subscriber(sub)


@pytest.mark.asyncio
async def test_health_check_counts(
    transport: InMemoryTransport,
) -> None:
    """Health includes subscriber/group counts."""
    bus = EventBus(transport, EventBusConfig())

    @bus.subscribe("a.*")
    async def handler_a(event: Event) -> None:
        pass

    @bus.consumer_group(["b"], "g", config=ConsumerGroupConfig(block_ms=100))
    async def handler_b(event: Event) -> None:
        pass

    async with bus:
        health = await bus.health_check()
        assert health["subscribers"] == 1
        assert health["consumer_groups"] == 1
        assert health["pubsub_listener_running"] is True


@pytest.mark.asyncio
async def test_stop_timeout(transport: InMemoryTransport) -> None:
    """Shutdown timeout doesn't prevent transport disconnect."""
    bus = EventBus(transport, EventBusConfig(shutdown_timeout=0.01))

    async with bus:
        pass

    # After stop, transport should be disconnected
    assert not await transport.is_healthy()


@pytest.mark.asyncio
async def test_context_manager_calls_start_stop(
    transport: InMemoryTransport,
) -> None:
    """async with bus: calls start then stop."""
    bus = EventBus(transport, EventBusConfig())

    assert bus._started is False
    async with bus:
        assert bus._started is True
        assert await transport.is_healthy()
    assert bus._started is False


@pytest.mark.asyncio
async def test_publish_metrics_emitted(
    transport: InMemoryTransport,
) -> None:
    """events.published metric incremented with correct tags."""
    from tests.helpers import FakeMetrics

    metrics = FakeMetrics()
    bus = EventBus(transport, EventBusConfig(), metrics=metrics)

    async with bus:
        await bus.publish("orders.created", data={"x": 1})

    published = metrics.find("increment", "events.published")
    assert len(published) == 1
    assert published[0][3] == {"topic": "orders.created"}


@pytest.mark.asyncio
async def test_publish_metrics_not_emitted_on_transport_failure() -> None:
    """Failed stream publishes are not counted as published events."""
    from tests.helpers import FakeMetrics

    class FailingTransport(InMemoryTransport):
        async def publish(self, event: Event) -> str:
            raise RuntimeError("stream down")

    metrics = FakeMetrics()
    bus = EventBus(FailingTransport(), EventBusConfig(), metrics=metrics)

    async with bus:
        with pytest.raises(RuntimeError, match="stream down"):
            await bus.publish("orders.created", data={"x": 1})

    assert metrics.find("increment", "events.published") == []
