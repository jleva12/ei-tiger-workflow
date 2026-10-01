"""SSE adapter: field injection, encoding, heartbeats, stop/restart."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from event_bus.adapters import sse as sse_module
from event_bus.adapters.sse import SSESubscriber, format_sse
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


def _parse_sse(stream: str) -> list[dict[str, str]]:
    """Minimal spec-following SSE parser (what a browser's EventSource does)."""
    events: list[dict[str, str]] = []
    fields: dict[str, str] = {}
    data: list[str] = []
    for line in re.split(r"\r\n|\r|\n", stream):
        if line == "":
            if data:
                events.append({**fields, "data": "\n".join(data)})
            fields, data = {}, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        else:
            fields[name] = value  # the last occurrence wins, as in browsers
    return events


async def _take(sub: SSESubscriber, count: int, *, within: float = 1.0) -> list[str]:
    """The next *count* non-comment chunks from ``sub.events()``."""
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in sub.events():
            if chunk.startswith(":"):
                continue
            chunks.append(chunk)
            if len(chunks) >= count:
                return

    await asyncio.wait_for(consume(), timeout=within)
    return chunks


@pytest.fixture(autouse=True)
def _clear_format_cache() -> None:
    sse_module._FORMAT_CACHE.clear()


# ---------------------------------------------------------------------------
# Field injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic", "event_id"),
    [
        ("orders.created\nevent: admin", "evt-1"),
        ("orders.created\r\nevent: admin", "evt-1"),
        ("orders.created\revent: admin", "evt-1"),
        ("orders.created", "evt-1\nevent: admin"),
        ("orders.created", "evt-1\rdata: {}"),
        ("orders.created", "evt\x00-1"),
    ],
)
async def test_crlf_in_topic_or_id_cannot_forge_fields(
    topic: str, event_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: a topic containing '\\n' forged an 'event: admin' for every subscriber."""
    caplog.set_level(logging.WARNING, logger="event_bus.adapters.sse")
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    await sub.on_event(_envelope(topic=topic, event_id=event_id))
    await sub.on_event(_envelope(topic="orders.created", event_id="good"))

    chunks = await _take(sub, 1)
    await sub.stop()

    parsed = _parse_sse("".join(chunks))
    assert parsed == [{"id": "good", "event": "orders.created", "data": '{"order_id":"42"}'}]
    assert any("unsafe SSE field" in r.getMessage() for r in caplog.records)


def test_format_sse_splits_multiline_data() -> None:
    chunk = format_sse("line1\nline2\r\nline3\rline4", event="t.x", event_id="7")
    assert chunk == "id: 7\nevent: t.x\ndata: line1\ndata: line2\ndata: line3\ndata: line4\n\n"
    assert _parse_sse(chunk) == [{"id": "7", "event": "t.x", "data": "line1\nline2\nline3\nline4"}]


def test_format_sse_without_optional_fields() -> None:
    assert format_sse("{}") == "data: {}\n\n"


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\r\nb"])
def test_format_sse_rejects_crlf_in_event_and_id(bad: str) -> None:
    with pytest.raises(ValueError):
        format_sse("{}", event=bad)
    with pytest.raises(ValueError):
        format_sse("{}", event_id=bad)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_data_uses_the_serializer_encoding_rules() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    ts = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    await sub.on_event(_envelope(data={"ts": ts, "tags": {"b", "a"}}, event_id="e1"))
    chunks = await _take(sub, 1)
    await sub.stop()
    assert _parse_sse(chunks[0])[0]["data"] == '{"ts":"2024-01-02T03:04:05+00:00","tags":["a","b"]}'


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_data", [{"x": math.nan}, {"x": math.inf}, {"x": object()}])
async def test_unencodable_event_is_skipped_and_stream_continues(
    bad_data: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="event_bus.adapters.sse")
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    await sub.on_event(_envelope(data=bad_data, event_id="bad"))
    await sub.on_event(_envelope(event_id="good"))
    chunks = await _take(sub, 1)
    await sub.stop()

    assert [e["id"] for e in _parse_sse("".join(chunks))] == ["good"]
    assert "NaN" not in "".join(chunks)
    assert any("Cannot encode event bad" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_default_format_is_encoded_once_for_many_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_dumps = sse_module.json_dumps

    def counting_dumps(obj: Any) -> str:
        nonlocal calls
        calls += 1
        return real_dumps(obj)

    monkeypatch.setattr(sse_module, "json_dumps", counting_dumps)
    subs = [SSESubscriber(["**"], heartbeat_interval=999.0) for _ in range(5)]
    envelope = _envelope(event_id="shared")
    for sub in subs:
        await sub.start()
        await sub.on_event(envelope)  # the bus pushes the same envelope to everyone
    outputs = [(await _take(sub, 1))[0] for sub in subs]
    for sub in subs:
        await sub.stop()

    assert len(set(outputs)) == 1
    assert calls == 1


# ---------------------------------------------------------------------------
# Heartbeats and prompt exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_sent_after_idle_interval_only() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=0.1)
    await sub.start()
    chunks: list[str] = []
    t0 = time.monotonic()

    async def consume() -> None:
        async for chunk in sub.events():
            chunks.append(chunk)
            if chunk == ": heartbeat\n\n":
                return

    await asyncio.wait_for(consume(), timeout=1.0)
    elapsed = time.monotonic() - t0
    await sub.stop()

    assert chunks == [": connected\n\n", ": heartbeat\n\n"]
    assert 0.08 <= elapsed < 0.5


@pytest.mark.asyncio
async def test_events_exits_promptly_on_stop() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in sub.events():
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # blocked waiting for an event
    t0 = time.monotonic()
    await sub.stop()
    await asyncio.wait_for(task, timeout=0.5)
    assert time.monotonic() - t0 < 0.5
    assert chunks == [": connected\n\n"]


@pytest.mark.asyncio
async def test_events_exits_promptly_on_cancel_event_and_leaves_no_tasks() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    cancel = asyncio.Event()
    tasks_before = len(asyncio.all_tasks())

    async def consume() -> None:
        async for _ in sub.events(cancel=cancel):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    cancel.set()
    await asyncio.wait_for(task, timeout=0.5)
    assert time.monotonic() - t0 < 0.5
    await asyncio.sleep(0)  # let cancelled helper tasks finish
    assert len(asyncio.all_tasks()) <= tasks_before
    await sub.stop()


def test_heartbeat_interval_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SSESubscriber(["**"], heartbeat_interval=0)


# ---------------------------------------------------------------------------
# stop() / restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stop_with_full_queue_returns_immediately() -> None:
    sub = SSESubscriber(["**"], max_queue_size=2, heartbeat_interval=999.0)
    await sub.start()
    for i in range(5):
        await sub.on_event(_envelope(event_id=f"e{i}"))
    assert sub.dropped_count == 3
    await asyncio.wait_for(sub.stop(), timeout=0.5)
    assert sub.dropped_count == 5  # plus the two still queued (never streamed)
    await asyncio.wait_for(sub.stop(), timeout=0.5)  # idempotent


@pytest.mark.asyncio
async def test_sse_stop_start_stop_start_keeps_streaming() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    for run in range(3):
        await sub.start()
        await sub.on_event(_envelope(event_id=f"run-{run}"))
        chunks = await _take(sub, 1)
        assert _parse_sse(chunks[0])[0]["id"] == f"run-{run}"
        await sub.on_event(_envelope(event_id=f"left-behind-{run}"))
        await sub.stop()
        await sub.on_event(_envelope(event_id=f"while-stopped-{run}"))  # ignored


@pytest.mark.asyncio
async def test_old_events_generator_ends_after_restart() -> None:
    sub = SSESubscriber(["**"], heartbeat_interval=999.0)
    await sub.start()
    old_chunks: list[str] = []

    async def consume_old() -> None:
        async for chunk in sub.events():
            old_chunks.append(chunk)

    old = asyncio.create_task(consume_old())
    await asyncio.sleep(0.02)
    await sub.stop()
    await sub.start()
    await asyncio.wait_for(old, timeout=0.5)

    await sub.on_event(_envelope(event_id="new-run"))
    chunks = await _take(sub, 1)
    await sub.stop()
    assert _parse_sse(chunks[0])[0]["id"] == "new-run"
    assert old_chunks == [": connected\n\n"]
