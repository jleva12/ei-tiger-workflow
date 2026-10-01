"""WebSocket adapter: bad events, dead connections, stop/restart, caching."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from event_bus.adapters import websocket as ws_module
from event_bus.adapters.websocket import WebSocketSubscriber
from event_bus.core.event import Event, EventEnvelope, EventMetadata


def _envelope(
    topic: str = "orders.created",
    data: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> EventEnvelope:
    event = Event(
        topic=topic,
        data=data if data is not None else {"order_id": "42"},
        metadata=EventMetadata(event_id=event_id or str(uuid.uuid4())),
    )
    return EventEnvelope(event=event)


class RecordingWebSocket:
    def __init__(self, delay: float = 0.0) -> None:
        self.sent: list[str] = []
        self.delay = delay

    async def send_text(self, data: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(data)

    def events(self) -> list[dict[str, Any]]:
        return [m for m in map(json.loads, self.sent) if m["type"] == "event"]


class BrokenWebSocket(RecordingWebSocket):
    """Sends *ok_sends* messages, then fails like a disconnected client."""

    def __init__(self, ok_sends: int = 0) -> None:
        super().__init__()
        self.ok_sends = ok_sends
        self.attempts = 0

    async def send_text(self, data: str) -> None:
        self.attempts += 1
        if self.attempts > self.ok_sends:
            raise ConnectionResetError("client went away")
        self.sent.append(data)


class HangingWebSocket(RecordingWebSocket):
    async def send_text(self, data: str) -> None:
        await asyncio.Event().wait()  # a peer that stopped reading


async def _wait_until(predicate: Callable[[], bool], within: float = 1.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _clear_format_cache() -> None:
    ws_module._FORMAT_CACHE.clear()


# ---------------------------------------------------------------------------
# One bad event does not kill the stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_formatter_error_skips_only_that_event(caplog: pytest.LogCaptureFixture) -> None:
    ws = RecordingWebSocket()

    def fmt(envelope: EventEnvelope) -> str:
        if envelope.event.event_id == "bad":
            raise KeyError("missing field")
        return envelope.event.event_id

    sub = WebSocketSubscriber(["**"], ws=ws, format_message=fmt)
    await sub.start()
    for event_id in ("e1", "bad", "e2"):
        await sub.on_event(_envelope(event_id=event_id))
    await _wait_until(lambda: len(ws.sent) == 2)
    assert not sub.closed.is_set()
    await sub.stop()

    assert ws.sent == ["e1", "e2"]
    failures = [r for r in caplog.records if "format_message failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_data", [{"x": math.nan}, {"x": object()}])
async def test_default_formatter_skips_unencodable_event(bad_data: dict[str, Any]) -> None:
    ws = RecordingWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws)
    await sub.start()
    await sub.on_event(_envelope(data=bad_data, event_id="bad"))
    await sub.on_event(_envelope(event_id="good"))
    await _wait_until(lambda: len(ws.sent) == 1)
    await sub.stop()

    assert [m["metadata"]["event_id"] for m in ws.events()] == ["good"]
    assert "NaN" not in ws.sent[0]


@pytest.mark.asyncio
async def test_default_format_is_compact_json_with_serializer_rules() -> None:
    ws = RecordingWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws)
    await sub.start()
    await sub.on_event(_envelope(data={"ids": {3, 1, 2}}, event_id="e1"))
    await _wait_until(lambda: len(ws.sent) == 1)
    await sub.stop()

    assert ws.sent[0].startswith('{"type":"event","topic":"orders.created","data":{"ids":[1,2,3]}')
    assert ws.events()[0]["metadata"]["event_id"] == "e1"


# ---------------------------------------------------------------------------
# A dead connection is detected and reported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_marks_subscriber_closed() -> None:
    ws = BrokenWebSocket(ok_sends=1)
    sub = WebSocketSubscriber(["**"], ws=ws, ping_interval=0.02)
    await sub.start()
    await sub.on_event(_envelope(event_id="e1"))
    await sub.on_event(_envelope(event_id="e2"))  # this send fails

    await asyncio.wait_for(sub.closed.wait(), timeout=1.0)  # what an owner would await
    assert not sub._running
    assert sub._ping_task is not None and sub._send_task is not None
    await asyncio.wait({sub._ping_task, sub._send_task}, timeout=1.0)
    assert sub._ping_task.done() and sub._send_task.done()

    attempts = ws.attempts
    await asyncio.sleep(0.1)  # several ping intervals
    assert ws.attempts == attempts  # no more pings on a dead connection

    await sub.on_event(_envelope(event_id="e3"))  # returns early once closed
    assert sub._queue.empty()

    await asyncio.wait_for(sub.stop(), timeout=1.0)
    assert [m["metadata"]["event_id"] for m in ws.events()] == ["e1"]


@pytest.mark.asyncio
async def test_ping_failure_marks_subscriber_closed_and_stops_send_loop() -> None:
    ws = BrokenWebSocket(ok_sends=0)
    sub = WebSocketSubscriber(["**"], ws=ws, ping_interval=0.02)
    await sub.start()
    await asyncio.wait_for(sub.closed.wait(), timeout=1.0)
    assert sub._send_task is not None
    await asyncio.wait({sub._send_task}, timeout=1.0)
    assert sub._send_task.cancelled()
    await asyncio.wait_for(sub.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_send_timeout_marks_subscriber_closed() -> None:
    sub = WebSocketSubscriber(["**"], ws=HangingWebSocket(), send_timeout=0.05)
    await sub.start()
    t0 = time.monotonic()
    await sub.on_event(_envelope())
    await asyncio.wait_for(sub.closed.wait(), timeout=1.0)
    assert time.monotonic() - t0 < 0.5
    await asyncio.wait_for(sub.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_queue_does_not_fill_behind_a_dead_connection() -> None:
    sub = WebSocketSubscriber(["**"], ws=BrokenWebSocket(), max_queue_size=4)
    await sub.start()
    await sub.on_event(_envelope())
    await asyncio.wait_for(sub.closed.wait(), timeout=1.0)
    for _ in range(100):
        await sub.on_event(_envelope())
    assert sub._queue.qsize() == 0
    assert sub.dropped_count == 0
    await sub.stop()


# ---------------------------------------------------------------------------
# stop() / restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_stop_with_full_queue_returns_promptly() -> None:
    ws = RecordingWebSocket(delay=0.1)
    sub = WebSocketSubscriber(["**"], ws=ws, max_queue_size=2)
    await sub.start()
    for i in range(6):
        await sub.on_event(_envelope(event_id=f"e{i}"))
        await asyncio.sleep(0)
    t0 = time.monotonic()
    await asyncio.wait_for(sub.stop(), timeout=1.0)
    assert time.monotonic() - t0 < 1.0
    # The send in flight completed (not cut off mid-frame); the backlog was dropped.
    assert len(ws.sent) == 1
    assert sub.dropped_count == 5
    assert sub.closed.is_set()


@pytest.mark.asyncio
async def test_ws_stop_start_stop_start_keeps_sending() -> None:
    ws = RecordingWebSocket()
    sub = WebSocketSubscriber(["**"], ws=ws)
    for run in range(3):
        await sub.start()
        assert not sub.closed.is_set()
        await sub.on_event(_envelope(event_id=f"run-{run}"))
        await _wait_until(lambda run=run: len(ws.sent) == run + 1)
        await sub.stop()
        assert sub.closed.is_set()
    assert [m["metadata"]["event_id"] for m in ws.events()] == ["run-0", "run-1", "run-2"]


@pytest.mark.asyncio
async def test_ws_stop_wakes_ping_loop_immediately() -> None:
    sub = WebSocketSubscriber(["**"], ws=RecordingWebSocket(), ping_interval=3600)
    await sub.start()
    await asyncio.sleep(0.01)
    t0 = time.monotonic()
    await sub.stop()
    assert time.monotonic() - t0 < 0.2
    assert sub._ping_task is not None
    assert sub._ping_task.done() and not sub._ping_task.cancelled()


@pytest.mark.asyncio
async def test_ws_loops_do_not_swallow_cancellation() -> None:
    sub = WebSocketSubscriber(["**"], ws=RecordingWebSocket())
    await sub.start()
    await asyncio.sleep(0)
    tasks = {t for t in (sub._send_task, sub._ping_task) if t is not None}
    assert len(tasks) == 2
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks, timeout=1.0)
    assert all(task.cancelled() for task in tasks)
    await sub.stop()


def test_ws_timeouts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WebSocketSubscriber(["**"], ws=RecordingWebSocket(), send_timeout=0)
    with pytest.raises(ValueError):
        WebSocketSubscriber(["**"], ws=RecordingWebSocket(), ping_interval=0)


# ---------------------------------------------------------------------------
# Payload caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_format_is_encoded_once_for_many_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_dumps = ws_module.json_dumps

    def counting_dumps(obj: Any) -> str:
        nonlocal calls
        calls += 1
        return real_dumps(obj)

    monkeypatch.setattr(ws_module, "json_dumps", counting_dumps)
    sockets = [RecordingWebSocket() for _ in range(5)]
    subs = [WebSocketSubscriber(["**"], ws=ws) for ws in sockets]
    envelope = _envelope(event_id="shared")
    for sub in subs:
        await sub.start()
        await sub.on_event(envelope)
    await _wait_until(lambda: all(len(ws.sent) == 1 for ws in sockets))
    for sub in subs:
        await sub.stop()

    assert len({ws.sent[0] for ws in sockets}) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_custom_formatter_bypasses_cache() -> None:
    calls = 0

    def fmt(envelope: EventEnvelope) -> str:
        nonlocal calls
        calls += 1
        return "custom"

    sockets = [RecordingWebSocket() for _ in range(3)]
    subs = [WebSocketSubscriber(["**"], ws=ws, format_message=fmt) for ws in sockets]
    envelope = _envelope()
    for sub in subs:
        await sub.start()
        await sub.on_event(envelope)
    await _wait_until(lambda: all(ws.sent == ["custom"] for ws in sockets))
    for sub in subs:
        await sub.stop()
    assert calls == 3
