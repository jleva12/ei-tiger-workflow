import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest

from event_bus.adapters import base as base_module
from event_bus.adapters.base import CallbackSubscriber, FormatCache
from event_bus.adapters.sse import SSESubscriber
from event_bus.adapters.websocket import WebSocketSubscriber
from event_bus.bus import EventBus
from event_bus.config import EventBusConfig
from event_bus.core.errors import InvalidTopicError
from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.topic import TopicPattern
from event_bus.testing.fake_transport import InMemoryTransport


def _make_envelope(
    topic: str = "orders.created", data: dict | None = None, event_id: str = "evt-1"
) -> EventEnvelope:
    event = Event(
        topic=topic,
        data=data or {"order_id": "42"},
        metadata=EventMetadata(event_id=event_id),
    )
    return EventEnvelope(event=event, stream_id="1-0", stream_name=topic)


class FakeWebSocket:
    """Minimal WebSocketSink for testing."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class NonConcurrentWebSocket(FakeWebSocket):
    """Fake sink that records concurrent send attempts."""

    def __init__(self) -> None:
        super().__init__()
        self._sending = False
        self.concurrent_send_detected = False

    async def send_text(self, data: str) -> None:
        if self._sending:
            self.concurrent_send_detected = True
            raise RuntimeError("concurrent send")
        self._sending = True
        try:
            await asyncio.sleep(0.03)
            self.sent.append(data)
        finally:
            self._sending = False


# ---------------------------------------------------------------------------
# Callback adapter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_subscriber_on_event() -> None:
    """Push an event directly and verify the handler is called."""
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    sub = CallbackSubscriber(["orders.*"], handler=handler)
    await sub.start()

    await sub.on_event(_make_envelope())
    await asyncio.sleep(0.05)  # let drain loop process

    await sub.stop()
    assert len(received) == 1
    assert received[0].topic == "orders.created"


@pytest.mark.asyncio
async def test_callback_subscriber_does_not_block_on_slow_handler() -> None:
    """on_event() returns immediately even if the handler is slow."""
    sub = CallbackSubscriber(
        ["**"],
        handler=lambda e: asyncio.sleep(10),  # very slow
        stop_timeout=0.05,
    )
    await sub.start()

    # on_event should return instantly (just enqueues)
    await asyncio.wait_for(sub.on_event(_make_envelope()), timeout=0.1)

    await sub.stop()


@pytest.mark.asyncio
async def test_callback_subscriber_start_idempotent() -> None:
    sub = CallbackSubscriber(["**"], handler=lambda e: asyncio.sleep(0))
    await sub.start()
    task1 = sub._drain_task
    await sub.start()
    assert sub._drain_task is task1
    await sub.stop()


# ---------------------------------------------------------------------------
# SSE adapter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_subscriber_on_event() -> None:
    """Push an event directly and read it from the SSE stream."""
    sub = SSESubscriber(
        ["orders.*"],
        max_queue_size=16,
        heartbeat_interval=999.0,
    )
    await sub.start()

    envelope = _make_envelope()
    await sub.on_event(envelope)

    chunks: list[str] = []
    async for chunk in sub.events():
        chunks.append(chunk)
        if len(chunks) >= 2:
            break

    await sub.stop()

    # First chunk is the initial ": connected" comment
    assert chunks[0] == ": connected\n\n"
    # Second chunk is the actual event
    assert "id: evt-1" in chunks[1]
    assert "event: orders.created" in chunks[1]
    assert '"order_id"' in chunks[1]


@pytest.mark.asyncio
async def test_sse_format() -> None:
    """Verify SSE format includes all required fields."""
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()

    envelope = _make_envelope(topic="test.topic", data={"key": "value"}, event_id="e-99")
    await sub.on_event(envelope)

    chunks: list[str] = []
    async for chunk in sub.events():
        chunks.append(chunk)
        if len(chunks) >= 2:
            break

    await sub.stop()

    # Skip the initial ": connected" comment (chunks[0])
    assert chunks[1] == 'id: e-99\nevent: test.topic\ndata: {"key":"value"}\n\n'


@pytest.mark.asyncio
async def test_sse_start_idempotent() -> None:
    """Calling start() twice is a no-op: the run (and its stop event) is kept."""
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    stop_event = sub._stop_event
    await sub.start()  # second call should be no-op
    assert sub._stop_event is stop_event
    assert sub._tasks == []  # no background tasks: heartbeats come from events()
    await sub.stop()


# ---------------------------------------------------------------------------
# WebSocket adapter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_subscriber_on_event() -> None:
    """Push an event directly and verify it's sent via the WebSocket."""
    ws = FakeWebSocket()
    sub = WebSocketSubscriber(
        ["orders.*"],
        ws=ws,
        max_queue_size=16,
    )
    await sub.start()

    envelope = _make_envelope(event_id="ws-evt-1", data={"order_id": "99"})
    await sub.on_event(envelope)

    # Give the send loop time to process
    await asyncio.sleep(0.1)

    await sub.stop()

    assert len(ws.sent) >= 1
    msg = json.loads(ws.sent[0])
    assert msg["type"] == "event"
    assert msg["topic"] == "orders.created"
    assert msg["data"]["order_id"] == "99"
    assert msg["metadata"]["event_id"] == "ws-evt-1"


@pytest.mark.asyncio
async def test_websocket_start_idempotent() -> None:
    """Calling start() twice should not create duplicate tasks."""
    ws = FakeWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws)
    await sub.start()
    send1 = sub._send_task
    ping1 = sub._ping_task
    await sub.start()  # second call should be no-op
    assert sub._send_task is send1
    assert sub._ping_task is ping1
    await sub.stop()


@pytest.mark.asyncio
async def test_websocket_queue_full_drops_event() -> None:
    """When the queue is full, events should be dropped without error."""
    ws = FakeWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws, max_queue_size=1)
    await sub.start()

    # Fill the queue (send loop will drain, but we flood faster)
    e1 = _make_envelope(event_id="e1")
    e2 = _make_envelope(event_id="e2")
    e3 = _make_envelope(event_id="e3")
    await sub.on_event(e1)
    # These may or may not be dropped depending on timing — no error should occur
    await sub.on_event(e2)
    await sub.on_event(e3)

    await asyncio.sleep(0.1)
    await sub.stop()
    # At least one message should have been sent
    assert len(ws.sent) >= 1


@pytest.mark.asyncio
async def test_websocket_serializes_event_and_ping_sends() -> None:
    """Event sends and ping sends should not call send_text concurrently."""
    ws = NonConcurrentWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws, ping_interval=0.01)
    await sub.start()

    await sub.on_event(_make_envelope(event_id="event-send"))
    await asyncio.sleep(0.12)
    await sub.stop()

    assert ws.concurrent_send_detected is False
    assert len(ws.sent) >= 1


# ---------------------------------------------------------------------------
# Additional callback adapter edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_queue_full_drops() -> None:
    """Event dropped when queue is full (no error)."""
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.event_id)

    sub = CallbackSubscriber(["**"], handler=handler, max_queue_size=1)
    # Don't start the drain loop — so the queue never drains
    # Enqueue events directly
    await sub.on_event(_make_envelope(event_id="e1"))  # fills queue (size=1)
    await sub.on_event(_make_envelope(event_id="e2"))  # dropped — no error raised

    # Verify no crash occurred (the queue is full, but on_event didn't raise)
    assert sub._queue.qsize() == 1


@pytest.mark.asyncio
async def test_callback_handler_exception_continues() -> None:
    """Handler error doesn't break drain loop."""
    call_count = 0

    async def bad_handler(event: Event) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")

    sub = CallbackSubscriber(["**"], handler=bad_handler)
    await sub.start()

    await sub.on_event(_make_envelope(event_id="e1"))
    await asyncio.sleep(0.05)
    await sub.on_event(_make_envelope(event_id="e2"))
    await asyncio.sleep(0.05)

    await sub.stop()
    assert call_count == 2


# ---------------------------------------------------------------------------
# Additional SSE adapter edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_heartbeat_emitted() -> None:
    """Heartbeat appears after configured interval."""
    sub = SSESubscriber(["**"], heartbeat_interval=0.05)
    await sub.start()

    chunks: list[str] = []
    async for chunk in sub.events():
        chunks.append(chunk)
        # Wait for initial connected + at least one heartbeat
        if any(": heartbeat" in c for c in chunks):
            break

    await sub.stop()
    assert any(": heartbeat" in c for c in chunks)


@pytest.mark.asyncio
async def test_sse_cancel_event_stops_generator() -> None:
    """External asyncio.Event breaks the generator."""
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()

    cancel = asyncio.Event()
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in sub.events(cancel=cancel):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    cancel.set()
    await asyncio.wait_for(task, timeout=2.0)
    await sub.stop()

    # Should have gotten at least the ": connected" chunk
    assert len(chunks) >= 1
    assert chunks[0] == ": connected\n\n"


# ---------------------------------------------------------------------------
# Additional WebSocket adapter edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_custom_formatter() -> None:
    """Custom format function is used."""
    ws = FakeWebSocket()

    def custom_format(envelope: EventEnvelope) -> str:
        return f"CUSTOM:{envelope.event.topic}"

    sub = WebSocketSubscriber(["**"], ws=ws, format_message=custom_format)
    await sub.start()

    await sub.on_event(_make_envelope(topic="test.custom"))
    await asyncio.sleep(0.1)
    await sub.stop()

    assert any("CUSTOM:test.custom" in msg for msg in ws.sent)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _wait_until(predicate: Callable[[], bool], within: float = 1.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Callback adapter lifecycle: stop() never hangs, restart keeps delivering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_stop_with_full_queue_returns_promptly() -> None:
    """Regression: stop() blocked forever on queue.put(sentinel) when the queue was full."""
    started = asyncio.Event()

    async def slow(event: Event) -> None:
        started.set()
        await asyncio.sleep(0.2)

    sub = CallbackSubscriber(["**"], handler=slow, max_queue_size=2)
    await sub.start()
    await sub.on_event(_make_envelope(event_id="e0"))
    await started.wait()  # e0 is in flight
    for i in range(1, 5):
        await sub.on_event(_make_envelope(event_id=f"e{i}"))  # e1, e2 queued; e3, e4 dropped
    assert sub._queue.full()

    t0 = time.monotonic()
    await asyncio.wait_for(sub.stop(), timeout=1.0)
    assert time.monotonic() - t0 < 1.0
    # e3 and e4 were dropped (queue full), e1 and e2 discarded by stop()
    assert sub.dropped_count == 4
    assert sub._queue.empty()


@pytest.mark.asyncio
async def test_callback_stop_lets_in_flight_handler_finish() -> None:
    started = asyncio.Event()
    finished: list[str] = []

    async def handler(event: Event) -> None:
        started.set()
        await asyncio.sleep(0.1)
        finished.append(event.event_id)

    sub = CallbackSubscriber(["**"], handler=handler, stop_timeout=2.0)
    await sub.start()
    await sub.on_event(_make_envelope(event_id="in-flight"))
    await started.wait()
    await sub.stop()

    assert finished == ["in-flight"]
    assert sub._drain_task is not None
    assert sub._drain_task.done() and not sub._drain_task.cancelled()


@pytest.mark.asyncio
async def test_callback_stop_cancels_handler_after_grace_period() -> None:
    started = asyncio.Event()
    cancelled = False

    async def stuck(event: Event) -> None:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled = True
            raise

    sub = CallbackSubscriber(["**"], handler=stuck, stop_timeout=0.1)
    await sub.start()
    await sub.on_event(_make_envelope())
    await started.wait()

    t0 = time.monotonic()
    await sub.stop()
    assert time.monotonic() - t0 < 1.0
    assert cancelled
    assert sub._drain_task is not None and sub._drain_task.cancelled()


@pytest.mark.asyncio
async def test_callback_stop_gives_up_on_handler_that_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(base_module, "_CANCEL_TIMEOUT", 0.1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn(event: Event) -> None:
        started.set()
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):  # badly behaved handler
                await asyncio.sleep(0.01)

    sub = CallbackSubscriber(["**"], handler=stubborn, stop_timeout=0.1)
    await sub.start()
    await sub.on_event(_make_envelope())
    await started.wait()
    task = sub._drain_task
    assert task is not None

    await asyncio.wait_for(sub.stop(), timeout=1.0)  # returns despite the stuck task
    assert not task.done()

    release.set()
    await asyncio.wait({task}, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_callback_stop_is_idempotent_and_reentrant() -> None:
    sub = CallbackSubscriber(["**"], handler=lambda e: asyncio.sleep(0))
    await sub.stop()  # never started: no-op
    await sub.start()
    await asyncio.wait_for(asyncio.gather(sub.stop(), sub.stop()), timeout=1.0)
    await asyncio.wait_for(sub.stop(), timeout=1.0)
    assert not sub._running


@pytest.mark.asyncio
async def test_callback_stop_start_stop_start_keeps_delivering() -> None:
    """Regression: a leftover shutdown sentinel made the restarted drain loop exit at once."""
    received: list[str] = []

    async def handler(event: Event) -> None:
        await asyncio.sleep(0.01)
        received.append(event.event_id)

    sub = CallbackSubscriber(["**"], handler=handler, stop_timeout=1.0)
    for run in range(3):
        await sub.start()
        # A backlog at stop() time is what used to leave the sentinel behind.
        for i in range(3):
            await sub.on_event(_make_envelope(event_id=f"backlog-{run}-{i}"))
        await sub.stop()

        await sub.start()
        await sub.on_event(_make_envelope(event_id=f"after-restart-{run}"))
        await _wait_until(lambda run=run: f"after-restart-{run}" in received)
        await sub.stop()

    assert [e for e in received if e.startswith("after-restart")] == [
        "after-restart-0",
        "after-restart-1",
        "after-restart-2",
    ]


@pytest.mark.asyncio
async def test_callback_ignores_events_while_stopped_and_buffers_before_start() -> None:
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.event_id)

    sub = CallbackSubscriber(["**"], handler=handler)
    await sub.on_event(_make_envelope(event_id="before-start"))  # buffered
    await sub.start()
    await _wait_until(lambda: received == ["before-start"])
    await sub.stop()

    await sub.on_event(_make_envelope(event_id="while-stopped"))  # ignored
    assert sub._queue.empty()
    await sub.start()
    await sub.on_event(_make_envelope(event_id="after-restart"))
    await _wait_until(lambda: received == ["before-start", "after-restart"])
    await sub.stop()


@pytest.mark.asyncio
async def test_callback_stop_cancelled_externally_cancels_handler_and_can_be_retried() -> None:
    started = asyncio.Event()

    async def stuck(event: Event) -> None:
        started.set()
        await asyncio.sleep(3600)

    sub = CallbackSubscriber(["**"], handler=stuck, stop_timeout=5.0)
    await sub.start()
    await sub.on_event(_make_envelope())
    await started.wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sub.stop(), timeout=0.05)  # e.g. the bus's shutdown timeout
    task = sub._drain_task
    assert task is not None
    await asyncio.wait({task}, timeout=1.0)
    assert task.cancelled()  # not left running behind the caller's back

    await asyncio.wait_for(sub.stop(), timeout=1.0)  # a retry completes
    await sub.start()  # and the subscriber restarts cleanly
    assert sub._drain_task is not task
    await sub.stop()


@pytest.mark.asyncio
async def test_callback_handler_may_stop_its_own_subscriber() -> None:
    sub: CallbackSubscriber

    async def handler(event: Event) -> None:
        await sub.stop()

    sub = CallbackSubscriber(["**"], handler=handler, stop_timeout=5.0)
    await sub.start()
    await sub.on_event(_make_envelope())
    task = sub._drain_task
    assert task is not None
    await asyncio.wait_for(asyncio.wait({task}), timeout=1.0)
    assert not sub._running
    await asyncio.wait_for(sub.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_callback_drain_loop_does_not_swallow_cancellation() -> None:
    sub = CallbackSubscriber(["**"], handler=lambda e: asyncio.sleep(0))
    await sub.start()
    task = sub._drain_task
    assert task is not None
    await asyncio.sleep(0)  # let it block in queue.get()
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.cancelled()
    await sub.stop()


@pytest.mark.asyncio
async def test_bus_subscribe_handlers_survive_bus_restart() -> None:
    """@bus.subscribe handlers keep receiving events across bus stop()/start()."""
    bus = EventBus(InMemoryTransport(), EventBusConfig())
    received: list[str] = []

    @bus.subscribe("orders.*")
    async def handler(event: Event) -> None:
        received.append(event.data["n"])

    for run in range(3):
        async with bus:
            await bus.publish("orders.created", {"n": f"run-{run}"})
            await _wait_until(lambda run=run: f"run-{run}" in received)

    assert received == ["run-0", "run-1", "run-2"]


# ---------------------------------------------------------------------------
# Drop visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_drops_are_counted_and_logged_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sub = CallbackSubscriber(["**"], handler=lambda e: asyncio.sleep(0), max_queue_size=1)
    caplog.set_level(logging.WARNING, logger="event_bus.adapters.base")

    for i in range(1001):  # not started: the first fills the queue, the rest are dropped
        await sub.on_event(_make_envelope(event_id=f"e{i}"))

    assert sub.dropped_count == 1000
    drop_logs = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert len(drop_logs) == 1  # the first drop only; the rest fall in the rate-limit window
    assert "e1" in drop_logs[0].getMessage()

    sub.drop_log_interval = 0.0  # window elapsed: the next drop reports the running total
    await sub.on_event(_make_envelope(event_id="late"))
    drop_logs = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert len(drop_logs) == 2
    assert "1000 dropped since the last report, 1001 in total" in drop_logs[1].getMessage()


# ---------------------------------------------------------------------------
# Client-supplied pattern validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ["sse", "ws"])
@pytest.mark.parametrize(
    "patterns",
    [
        ["orders.\n*"],
        ["orders created"],
        [""],
        ["orders*"],
        ["orders..created"],
        "orders.*",  # a bare string, not a list
        [f"p{i}" for i in range(33)],  # more than max_patterns
        ["a." * 40 + "b"],  # too many segments
        [42],
    ],
)
def test_client_patterns_are_validated(factory: str, patterns: Any) -> None:
    with pytest.raises(InvalidTopicError):
        if factory == "sse":
            SSESubscriber(patterns)
        else:
            WebSocketSubscriber(patterns, ws=FakeWebSocket())


def test_client_pattern_errors_are_value_errors() -> None:
    with pytest.raises(ValueError):
        SSESubscriber(["bad topic"])


def test_valid_client_patterns_and_custom_limit() -> None:
    sub = SSESubscriber(["orders.*", "payments.**", TopicPattern("tenant-1:x.**")])
    assert [p.pattern for p in sub.patterns] == ["orders.*", "payments.**", "tenant-1:x.**"]
    WebSocketSubscriber([f"p{i}" for i in range(40)], ws=FakeWebSocket(), max_patterns=40)
    with pytest.raises(InvalidTopicError):
        SSESubscriber(["a", "b"], max_patterns=1)
    with pytest.raises(InvalidTopicError):
        SSESubscriber([TopicPattern("bad pattern")])


def test_callback_subscriber_patterns_are_not_restricted() -> None:
    """Server-side patterns are trusted and keep working unvalidated."""
    sub = CallbackSubscriber(["legacy/topic"], handler=lambda e: asyncio.sleep(0))
    assert sub.accepts("legacy/topic")


# ---------------------------------------------------------------------------
# FormatCache
# ---------------------------------------------------------------------------


def test_format_cache_reuses_payload_for_same_event_only() -> None:
    cache = FormatCache(maxsize=2)
    calls: list[str] = []

    def fmt(value: str) -> Callable[[], str]:
        def inner() -> str:
            calls.append(value)
            return value

        return inner

    e1 = Event(topic="t", data={}, metadata=EventMetadata(event_id="same"))
    e1_twin = Event(topic="t", data={"other": 1}, metadata=EventMetadata(event_id="same"))
    assert cache.get_or_format(e1, fmt("one")) == "one"
    assert cache.get_or_format(e1, fmt("unused")) == "one"
    # Same event_id, different Event object: formatted afresh, never mixed up.
    assert cache.get_or_format(e1_twin, fmt("twin")) == "twin"
    assert calls == ["one", "twin"]


def test_format_cache_is_bounded() -> None:
    cache = FormatCache(maxsize=3)
    events = [Event(topic="t", data={}) for _ in range(10)]
    for event in events:
        cache.get_or_format(event, lambda: "x")
    assert len(cache) == 3


def test_format_cache_caches_failures_and_skips_huge_payloads() -> None:
    cache = FormatCache(max_payload_chars=10)
    calls = 0

    def failing() -> str | None:
        nonlocal calls
        calls += 1
        return None

    event = Event(topic="t", data={})
    assert cache.get_or_format(event, failing) is None
    assert cache.get_or_format(event, failing) is None
    assert calls == 1

    big = Event(topic="t", data={})
    assert cache.get_or_format(big, lambda: "y" * 11) == "y" * 11
    assert len(cache) == 1  # the huge payload was not kept
