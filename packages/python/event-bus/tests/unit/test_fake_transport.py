"""Tests for event_bus/testing/fake_transport.py — InMemoryTransport."""

import asyncio
from datetime import UTC, datetime

import pytest

from event_bus.core.errors import EventBusConnectionError
from event_bus.core.event import Event
from event_bus.interfaces.transport import NoGroupError
from event_bus.testing.fake_transport import InMemoryTransport


@pytest.fixture
def transport() -> InMemoryTransport:
    return InMemoryTransport()


class TestConnectDisconnect:
    async def test_connect_disconnect(self, transport: InMemoryTransport) -> None:
        """_connected flag toggles, is_healthy() matches."""
        assert not await transport.is_healthy()
        await transport.connect()
        assert await transport.is_healthy()
        await transport.disconnect()
        assert not await transport.is_healthy()

    async def test_disconnect_keeps_state_and_reset_clears_it(
        self, transport: InMemoryTransport
    ) -> None:
        """Like Redis, a reconnect finds the streams, groups and pending
        messages; reset() forgets them."""
        await transport.connect()
        await transport.publish(Event(topic="t", data={"k": "v"}))
        await transport.create_consumer_group("t", "g")
        [envelope] = await transport.read_group("t", "g", "c", count=1, block_ms=0)
        await transport.disconnect()

        await transport.connect()
        pending = await transport.get_pending("t", "g", min_idle_ms=0)
        assert [p["message_id"] for p in pending] == [envelope.stream_id]

        transport.reset()
        assert transport.published_events == []
        assert transport.stream_length("t") == 0

    async def test_use_after_disconnect_is_refused(self, transport: InMemoryTransport) -> None:
        await transport.connect()
        await transport.disconnect()
        with pytest.raises(EventBusConnectionError):
            await transport.publish(Event(topic="t", data={}))


class TestPublish:
    async def test_publish_stores_event(self, transport: InMemoryTransport) -> None:
        """published_events and _streams populate correctly."""
        await transport.connect()
        event = Event(topic="orders.created", data={"id": "1"})
        stream_id = await transport.publish(event)
        assert stream_id == "1-0"
        assert len(transport.published_events) == 1
        assert transport.published_events[0] is event
        assert transport.stream_length("orders.created") == 1

    async def test_consumers_get_what_redis_would_give_them(
        self, transport: InMemoryTransport
    ) -> None:
        """Data round-trips through the serializer, and envelopes carry the
        Redis stream key."""
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        data = {"when": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC), "tags": ["a"]}
        await transport.publish(Event(topic="t", data=data))
        [envelope] = await transport.read_group("t", "g", "c", block_ms=0)
        assert envelope.event.data == {"when": "2024-01-02T03:04:05+00:00", "tags": ["a"]}
        assert envelope.event.data is not data
        assert envelope.stream_name == "eventbus:stream:t"

    async def test_publish_returns_incrementing_ids(self, transport: InMemoryTransport) -> None:
        """Stream IDs are '1-0', '2-0', etc."""
        await transport.connect()
        id1 = await transport.publish(Event(topic="t", data={}))
        id2 = await transport.publish(Event(topic="t", data={}))
        id3 = await transport.publish(Event(topic="t", data={}))
        assert id1 == "1-0"
        assert id2 == "2-0"
        assert id3 == "3-0"

    async def test_publish_many(self, transport: InMemoryTransport) -> None:
        """Batch publish stores all events."""
        await transport.connect()
        events = [Event(topic="t", data={"i": i}) for i in range(3)]
        ids = await transport.publish_many(events)
        assert len(ids) == 3
        assert len(transport.published_events) == 3


class TestPubSub:
    async def test_pubsub_receives_events(self, transport: InMemoryTransport) -> None:
        """Published events flow to subscribe_pubsub iterator."""
        await transport.connect()
        received = []

        async def listener() -> None:
            async for envelope in transport.subscribe_pubsub(["orders.*"]):
                received.append(envelope)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(listener())
        await asyncio.sleep(0.01)

        await transport.publish(Event(topic="orders.created", data={"id": "1"}))
        await transport.publish(Event(topic="orders.updated", data={"id": "2"}))
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 2
        assert received[0].event.topic == "orders.created"
        assert received[1].event.topic == "orders.updated"

    async def test_pubsub_pattern_filtering(self, transport: InMemoryTransport) -> None:
        """Non-matching topics are filtered out."""
        await transport.connect()
        received = []

        async def listener() -> None:
            async for envelope in transport.subscribe_pubsub(["orders.*"]):
                received.append(envelope)
                break

        task = asyncio.create_task(listener())
        await asyncio.sleep(0.01)

        # This should NOT match "orders.*"
        await transport.publish(Event(topic="payments.created", data={}))
        # This should match
        await transport.publish(Event(topic="orders.created", data={}))

        await asyncio.wait_for(task, timeout=2.0)
        assert len(received) == 1
        assert received[0].event.topic == "orders.created"

    async def test_pubsub_disconnect_exits_iterator(self, transport: InMemoryTransport) -> None:
        """subscribe_pubsub exits cleanly on disconnect."""
        await transport.connect()
        received = []

        async def listener() -> None:
            async for envelope in transport.subscribe_pubsub(["**"]):
                received.append(envelope)

        task = asyncio.create_task(listener())
        await asyncio.sleep(0.01)

        await transport.disconnect()
        await asyncio.wait_for(task, timeout=2.0)
        # Should exit without error


class TestConsumerGroup:
    async def test_reading_a_group_that_doesnt_exist_raises_nogroup(
        self, transport: InMemoryTransport
    ) -> None:
        await transport.connect()
        await transport.publish(Event(topic="t", data={}))
        with pytest.raises(NoGroupError):
            await transport.read_group("t", "g", "c", block_ms=0)

    async def test_start_id_is_honoured(self, transport: InMemoryTransport) -> None:
        """'0' reads the retained history; '$' only what's published after."""
        await transport.connect()
        await transport.publish(Event(topic="t", data={"n": 1}))
        await transport.create_consumer_group("t", "from-start", start_id="0")
        await transport.create_consumer_group("t", "from-now", start_id="$")
        await transport.publish(Event(topic="t", data={"n": 2}))
        early = await transport.read_group("t", "from-start", "c", block_ms=0)
        late = await transport.read_group("t", "from-now", "c", block_ms=0)
        assert [e.event.data["n"] for e in early] == [1, 2]
        assert [e.event.data["n"] for e in late] == [2]

    async def test_a_blocking_read_wakes_when_an_event_arrives(
        self, transport: InMemoryTransport
    ) -> None:
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        reader = asyncio.create_task(transport.read_group("t", "g", "c", block_ms=5000))
        await asyncio.sleep(0.05)
        await transport.publish(Event(topic="t", data={}))
        messages = await asyncio.wait_for(reader, timeout=1.0)
        assert len(messages) == 1

    async def test_read_group_returns_messages(self, transport: InMemoryTransport) -> None:
        """read_group returns published events."""
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await transport.publish(Event(topic="t", data={"x": 1}))
        await transport.publish(Event(topic="t", data={"x": 2}))

        messages = await transport.read_group("t", "g", "c", count=10, block_ms=0)
        assert len(messages) == 2
        assert messages[0].delivery_count == 1
        assert messages[0].event.data == {"x": 1}
        assert messages[1].event.data == {"x": 2}

    async def test_read_group_advances_cursor(self, transport: InMemoryTransport) -> None:
        """Second read returns new messages only."""
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await transport.publish(Event(topic="t", data={"x": 1}))

        msg1 = await transport.read_group("t", "g", "c", count=10, block_ms=0)
        assert len(msg1) == 1

        await transport.publish(Event(topic="t", data={"x": 2}))
        msg2 = await transport.read_group("t", "g", "c", count=10, block_ms=0)
        assert len(msg2) == 1
        assert msg2[0].event.data == {"x": 2}

    async def test_read_group_many_reads_all_topics_once(
        self, transport: InMemoryTransport
    ) -> None:
        """read_group_many returns available messages across topics."""
        await transport.connect()
        await transport.create_consumer_group("a", "g")
        await transport.create_consumer_group("b", "g")
        await transport.publish(Event(topic="b", data={"src": "b"}))

        messages = await transport.read_group_many(["a", "b"], "g", "c", count=10, block_ms=0)

        assert len(messages) == 1
        assert messages[0].event.topic == "b"
        assert messages[0].delivery_count == 1

    async def test_pending_claim_increments_delivery_count(
        self, transport: InMemoryTransport
    ) -> None:
        """Pending entries can be reclaimed with an incremented delivery count."""
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await transport.publish(Event(topic="t", data={"x": 1}))

        messages = await transport.read_group("t", "g", "c1", count=1, block_ms=0)
        assert messages[0].delivery_count == 1

        pending = await transport.get_pending("t", "g", min_idle_ms=0)
        assert pending[0]["message_id"] == messages[0].stream_id
        assert pending[0]["times_delivered"] == 1

        claimed = await transport.claim_message(
            "t", "g", "c2", messages[0].stream_id, min_idle_ms=0
        )
        assert claimed is not None
        assert claimed.delivery_count == 2

        await transport.ack("t", "g", messages[0].stream_id)
        assert await transport.get_pending("t", "g", min_idle_ms=0) == []


class TestDLQ:
    async def test_send_to_dlq_and_read(self, transport: InMemoryTransport) -> None:
        """DLQ stores and reads back correctly."""
        await transport.connect()
        event = Event(topic="t", data={"x": 1})
        await transport.publish(event)

        # Create the group first then read
        await transport.create_consumer_group("t", "g")
        envelope = (await transport.read_group("t", "g", "c", count=10, block_ms=0))[0]
        await transport.send_to_dlq(envelope, "handler failed")

        dlq_items = await transport.read_dlq(count=10)
        assert len(dlq_items) == 1
        assert dlq_items[0].event.data == {"x": 1}

    async def test_dead_letters_belong_to_their_group(self, transport: InMemoryTransport) -> None:
        """dead_letter acks the message and files it under the group only."""
        await transport.connect()
        await transport.create_consumer_group("t", "a")
        await transport.create_consumer_group("t", "b")
        await transport.publish(Event(topic="t", data={"x": 1}))
        [envelope] = await transport.read_group("t", "a", "c", block_ms=0)
        await transport.dead_letter(envelope, "boom", "a")

        assert await transport.get_pending("t", "a", min_idle_ms=0) == []
        [letter] = await transport.read_dlq(group_name="a")
        assert await transport.read_dlq(group_name="b") == []
        await transport.delete_dlq(letter.stream_id, group_name="a")
        assert await transport.read_dlq(group_name="a") == []


class TestLeasesAndHousekeeping:
    async def test_touch_renews_or_hands_back_a_lease(self, transport: InMemoryTransport) -> None:
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await transport.publish(Event(topic="t", data={}))
        [envelope] = await transport.read_group("t", "g", "c1", block_ms=0)

        # Renewed: not idle enough to take over.
        await transport.touch("t", "g", "c1", [envelope.stream_id], idle_ms=0)
        assert await transport.claim_idle("t", "g", "c2", min_idle_ms=1000) == []
        # Handed back: claimable at once, one more delivery.
        await transport.touch("t", "g", "c1", [envelope.stream_id], idle_ms=1000)
        [claimed] = await transport.claim_idle("t", "g", "c2", min_idle_ms=1000)
        assert claimed.delivery_count == 2

    async def test_trim_keeps_what_any_group_still_needs(
        self, transport: InMemoryTransport
    ) -> None:
        await transport.connect()
        await transport.create_consumer_group("t", "fast")
        await transport.create_consumer_group("t", "slow")
        for n in range(4):
            await transport.publish(Event(topic="t", data={"n": n}))
        fast = await transport.read_group("t", "fast", "c", block_ms=0)
        await transport.ack_many("t", "fast", [e.stream_id for e in fast])
        slow = await transport.read_group("t", "slow", "c", count=2, block_ms=0)
        await transport.ack_many("t", "slow", [slow[0].stream_id])

        # "slow" still holds its second message pending and hasn't read 3 and 4.
        assert await transport.trim_consumed("t") == 1
        assert transport.stream_length("t") == 3
        rest = await transport.read_group("t", "slow", "c", block_ms=0)
        assert [e.event.data["n"] for e in rest] == [2, 3]

    async def test_idle_consumers_without_pending_are_removed(
        self, transport: InMemoryTransport
    ) -> None:
        await transport.connect()
        await transport.create_consumer_group("t", "g")
        await transport.publish(Event(topic="t", data={}))
        await transport.read_group("t", "g", "dead-holder", block_ms=0)
        await transport.read_group("t", "g", "dead-empty", block_ms=0)
        await transport.read_group("t", "g", "me", block_ms=0)
        await asyncio.sleep(0.02)
        removed = await transport.remove_idle_consumers("t", "g", idle_ms=10, keep={"me"})
        assert removed == 1
        assert transport.consumers("t", "g") == ["dead-holder", "me"]
