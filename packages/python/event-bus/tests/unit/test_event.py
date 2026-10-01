import pytest

from event_bus.core.event import Event, EventEnvelope, EventMetadata


class TestEventMetadata:
    def test_defaults(self) -> None:
        meta = EventMetadata()
        assert meta.event_id
        assert meta.timestamp > 0
        assert meta.source == ""
        assert meta.retry_count == 0
        assert meta.headers == {}

    def test_immutable(self) -> None:
        meta = EventMetadata()
        with pytest.raises(AttributeError):
            meta.source = "changed"  # type: ignore[misc]


class TestEvent:
    def test_create(self) -> None:
        event = Event(topic="orders.created", data={"order_id": "123"})
        assert event.topic == "orders.created"
        assert event.data == {"order_id": "123"}
        assert event.event_id == event.metadata.event_id

    def test_with_retry(self) -> None:
        event = Event(topic="t", data={}, metadata=EventMetadata(retry_count=0))
        retried = event.with_retry()
        assert retried.metadata.retry_count == 1
        assert retried.metadata.event_id == event.metadata.event_id
        # Original is unchanged (frozen)
        assert event.metadata.retry_count == 0

    def test_immutable(self) -> None:
        event = Event(topic="t", data={})
        with pytest.raises(AttributeError):
            event.topic = "changed"  # type: ignore[misc]

    def test_with_retry_preserves_all_fields(self) -> None:
        """All metadata fields preserved across retry."""
        meta = EventMetadata(
            event_id="e-1",
            source="src",
            correlation_id="corr",
            causation_id="cause",
            content_type="application/json",
            retry_count=2,
            headers={"x-key": "val"},
        )
        event = Event(topic="t", data={"a": 1}, metadata=meta)
        retried = event.with_retry()

        assert retried.metadata.event_id == "e-1"
        assert retried.metadata.source == "src"
        assert retried.metadata.correlation_id == "corr"
        assert retried.metadata.causation_id == "cause"
        assert retried.metadata.content_type == "application/json"
        assert retried.metadata.retry_count == 3
        assert retried.metadata.headers == {"x-key": "val"}
        assert retried.topic == "t"
        assert retried.data == {"a": 1}

    def test_custom_headers(self) -> None:
        """Custom headers dict preserved on Event."""
        headers = {"x-trace-id": "abc", "x-source": "test"}
        event = Event(
            topic="t",
            data={},
            metadata=EventMetadata(headers=headers),
        )
        assert event.metadata.headers == headers


class TestEventMetadataUniqueness:
    def test_unique_ids(self) -> None:
        """Two EventMetadata instances have different event_ids."""
        m1 = EventMetadata()
        m2 = EventMetadata()
        assert m1.event_id != m2.event_id


class TestEventEnvelope:
    def test_create(self) -> None:
        event = Event(topic="t", data={})
        envelope = EventEnvelope(event=event, stream_id="1-0", stream_name="s")
        assert envelope.event is event
        assert envelope.stream_id == "1-0"
        assert envelope.delivery_count == 0


class TestHashing:
    def test_event_is_hashable_by_event_id(self) -> None:
        """Regression: hash(Event) raised because of the dict fields."""
        event = Event(topic="t", data={"a": 1}, metadata=EventMetadata(event_id="e-1"))
        assert hash(event) == hash("e-1")
        assert event in {event}

    def test_equal_events_have_equal_hashes(self) -> None:
        meta = EventMetadata(event_id="e-1", timestamp=1.0, headers={"h": "v"})
        a = Event(topic="t", data={"a": 1}, metadata=meta)
        b = Event(topic="t", data={"a": 1}, metadata=EventMetadata(**_meta_kwargs(meta)))
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_same_id_different_content_is_not_equal(self) -> None:
        a = Event(topic="t", data={"a": 1}, metadata=EventMetadata(event_id="e-1"))
        b = Event(topic="t", data={"a": 2}, metadata=EventMetadata(event_id="e-1"))
        assert a != b
        assert len({a, b}) == 2  # hash collision only; eq keeps them apart

    def test_metadata_and_envelope_are_hashable(self) -> None:
        meta = EventMetadata(event_id="e-1", headers={"h": "v"})
        assert hash(meta) == hash("e-1")
        envelope = EventEnvelope(event=Event(topic="t", data={}, metadata=meta), stream_id="1-0")
        assert envelope in {envelope}


class TestWithRetryCopies:
    def test_with_retry_copies_headers(self) -> None:
        """Regression: the retry shared the original's headers dict."""
        original = Event(topic="t", data={}, metadata=EventMetadata(headers={"x": "1"}))
        retried = original.with_retry()
        assert retried.metadata.headers == {"x": "1"}
        assert retried.metadata.headers is not original.metadata.headers
        retried.metadata.headers["x"] = "changed"
        assert original.metadata.headers == {"x": "1"}

    def test_with_retry_twice(self) -> None:
        event = Event(topic="t", data={}).with_retry().with_retry()
        assert event.metadata.retry_count == 2


def _meta_kwargs(meta: EventMetadata) -> dict[str, object]:
    return {f: getattr(meta, f) for f in meta.__dataclass_fields__}
