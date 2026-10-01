from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Sequence

from event_bus.adapters.base import (
    DEFAULT_MAX_PATTERNS,
    BufferedSubscriber,
    FormatCache,
    QueueItem,
    validate_subscriber_patterns,
)
from event_bus.core.event import EventEnvelope
from event_bus.core.serialization import json_dumps
from event_bus.core.topic import TopicPattern

logger = logging.getLogger(__name__)

# SSE line terminators (the spec's CRLF, CR and LF -- nothing else).
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

_CONNECTED = ": connected\n\n"
_HEARTBEAT = ": heartbeat\n\n"

# Default-formatted chunks of recent events, shared by all SSE subscribers.
_FORMAT_CACHE = FormatCache()


def _check_field(name: str, value: str) -> None:
    # A CR or LF would end the field and let the rest of the value inject
    # fields of its own (e.g. a forged "event:" line). Browsers also ignore an
    # id that contains NUL.
    if "\r" in value or "\n" in value or (name == "id" and "\0" in value):
        raise ValueError(f"SSE {name} field must not contain CR, LF or NUL: {value[:80]!r}")


def format_sse(data: str, *, event: str | None = None, event_id: str | None = None) -> str:
    """Build one Server-Sent Events message.

    Every line of *data* gets its own ``data:`` prefix, so multi-line payloads
    arrive intact. ``event`` and ``id`` must be single-line.

    :raises ValueError: if *event* or *event_id* contains CR or LF (or
        *event_id* contains NUL).
    """
    lines: list[str] = []
    if event_id is not None:
        _check_field("id", event_id)
        lines.append(f"id: {event_id}")
    if event is not None:
        _check_field("event", event)
        lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in _LINE_BREAK.split(data))
    return "\n".join(lines) + "\n\n"


def _format_envelope(envelope: EventEnvelope) -> str | None:
    """Default SSE chunk for an event, or None (logged) if it cannot be sent."""
    event = envelope.event
    try:
        data = json_dumps(event.data)
    except Exception:  # TypeError/ValueError from json_dumps, or anything unexpected
        logger.error(
            "Cannot encode event %s (topic=%r) for SSE; skipping it",
            event.event_id,
            event.topic,
            exc_info=True,
        )
        return None
    try:
        return format_sse(data, event=event.topic, event_id=event.event_id)
    except ValueError as exc:
        logger.warning("Dropping event with an unsafe SSE field: %s", exc)
        return None


class SSESubscriber(BufferedSubscriber):
    """
    Adapter that converts pushed events into an SSE-compatible
    async iterator of formatted strings.

    Framework-agnostic: the caller is responsible for writing these
    strings to the HTTP response.

    Patterns usually come from the client, so they are validated with
    :func:`~event_bus.core.topic.validate_pattern` and capped at
    *max_patterns*; invalid input raises
    :class:`~event_bus.core.errors.InvalidTopicError` (a ``ValueError``).

    Usage::

        subscriber = SSESubscriber(["orders.*"])
        bus.add_subscriber(subscriber)
        await subscriber.start()
        async for chunk in subscriber.events():
              await response.write(chunk.encode())
        await subscriber.stop()
        bus.remove_subscriber(subscriber)
    """

    def __init__(
        self,
        patterns: Sequence[str | TopicPattern],
        *,
        max_queue_size: int = 256,
        heartbeat_interval: float = 15.0,
        max_patterns: int = DEFAULT_MAX_PATTERNS,
    ) -> None:
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be > 0")
        super().__init__(
            validate_subscriber_patterns(patterns, max_patterns=max_patterns),
            max_queue_size=max_queue_size,
        )
        self._heartbeat_interval = heartbeat_interval
        logger.debug(
            "SSESubscriber created (patterns=%s, max_queue_size=%d, heartbeat_interval=%.1fs)",
            patterns,
            max_queue_size,
            heartbeat_interval,
        )

    async def on_event(self, envelope: EventEnvelope) -> None:
        self._enqueue(envelope)

    async def events(
        self,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[str]:
        """
        Yields SSE-formatted strings.

        Each event::

            id: <event_id>
            event: <topic>
            data: {"key":"value"}

        Heartbeats, sent after ``heartbeat_interval`` seconds without an event::

            : heartbeat

        The generator ends promptly once :meth:`stop` is called or *cancel*
        is set. Events whose ID or topic contains CR/LF, or whose data cannot
        be encoded as JSON, are skipped with a log message.

        Args:
            cancel: Optional event that, when set, causes the generator
                to exit. Used for graceful server shutdown — the caller
                sets this event so SSE connections drain before the
                lifespan shuts down.
        """
        # Yield an initial comment immediately so the response headers
        # and first bytes flush through any proxy (e.g. Vite, nginx).
        # Without this, the client may not see the 200 OK until the
        # first heartbeat or event arrives (up to heartbeat_interval).
        yield _CONNECTED

        stop_event = self._stop_event  # this run's; start() makes a new one
        while self._running and not stop_event.is_set():
            if cancel is not None and cancel.is_set():
                logger.debug("SSE events() cancelled via external event")
                return
            try:
                item: QueueItem | None = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                item = await self._wait_for_item(stop_event, cancel)
                if item is None:
                    if stop_event.is_set() or (cancel is not None and cancel.is_set()):
                        return
                    logger.debug("SSE emitting heartbeat")
                    yield _HEARTBEAT
                    continue
            if not isinstance(item, EventEnvelope):
                return  # stop signal
            chunk = self._format_sse(item)
            if chunk is not None:
                logger.debug(
                    "SSE emitting event %s (topic=%s)",
                    item.event.event_id,
                    item.event.topic,
                )
                yield chunk

    async def _wait_for_item(
        self,
        stop_event: asyncio.Event,
        cancel: asyncio.Event | None,
    ) -> QueueItem | None:
        """Wait for the next queue item, a stop/cancel, or the heartbeat timeout.

        Returns None on stop, cancel or timeout. No task outlives the call,
        and an item that was dequeued is always returned, never lost.
        """
        getter = asyncio.create_task(self._queue.get())
        waiters = [asyncio.create_task(stop_event.wait())]
        if cancel is not None:
            waiters.append(asyncio.create_task(cancel.wait()))
        try:
            await asyncio.wait(
                [getter, *waiters],
                timeout=self._heartbeat_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # No await in here: an item the getter already took is returned below.
            for waiter in waiters:
                waiter.cancel()
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        return None

    def _format_sse(self, envelope: EventEnvelope) -> str | None:
        """The event's SSE chunk, or None if it must be skipped (logged once)."""
        return _FORMAT_CACHE.get_or_format(envelope.event, lambda: _format_envelope(envelope))
