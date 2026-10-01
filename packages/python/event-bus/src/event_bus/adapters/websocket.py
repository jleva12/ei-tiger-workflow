from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from event_bus.adapters.base import (
    DEFAULT_MAX_PATTERNS,
    DEFAULT_STOP_TIMEOUT,
    BufferedSubscriber,
    FormatCache,
    validate_subscriber_patterns,
)
from event_bus.core.event import EventEnvelope
from event_bus.core.serialization import json_dumps
from event_bus.core.topic import TopicPattern

logger = logging.getLogger(__name__)

_PING_MESSAGE = json_dumps({"type": "ping"})

# Default-formatted messages of recent events, shared by all WebSocket subscribers.
_FORMAT_CACHE = FormatCache()


class WebSocketSink(Protocol):
    """
    Protocol for a WebSocket connection.

    Framework authors implement this to bridge their WS library.
    Compatible with starlette, aiohttp, websockets, etc.
    """

    async def send_text(self, data: str) -> None: ...


class WebSocketSubscriber(BufferedSubscriber):
    """
    Adapter that forwards pushed events to a WebSocket connection.

    Framework-agnostic: accepts any object implementing :class:`WebSocketSink`.

    - Patterns usually come from the client, so they are validated with
      :func:`~event_bus.core.topic.validate_pattern` and capped at
      *max_patterns*; invalid input raises
      :class:`~event_bus.core.errors.InvalidTopicError` (a ``ValueError``).
    - An event the formatter cannot encode is logged and skipped; the stream
      carries on.
    - A failed or timed-out send (``send_timeout`` seconds) means the
      connection is gone: the subscriber stops sending events and pings and
      sets :attr:`closed`, which the owner can await to tear the connection
      down. :attr:`closed` is also set by :meth:`stop`.

    Usage::

        ws_sub = WebSocketSubscriber(
            patterns=["orders.*"],
            ws=websocket,
        )
        bus.add_subscriber(ws_sub)
        await ws_sub.start()
        # ... connection open (or: await ws_sub.closed.wait()) ...
        await ws_sub.stop()
        bus.remove_subscriber(ws_sub)
    """

    def __init__(
        self,
        patterns: Sequence[str | TopicPattern],
        ws: WebSocketSink,
        *,
        max_queue_size: int = 512,
        format_message: Callable[[EventEnvelope], str] | None = None,
        ping_interval: float = 30.0,
        send_timeout: float = 10.0,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        max_patterns: int = DEFAULT_MAX_PATTERNS,
    ) -> None:
        if ping_interval <= 0:
            raise ValueError("ping_interval must be > 0")
        if send_timeout <= 0:
            raise ValueError("send_timeout must be > 0")
        super().__init__(
            validate_subscriber_patterns(patterns, max_patterns=max_patterns),
            max_queue_size=max_queue_size,
            stop_timeout=stop_timeout,
        )
        self._ws = ws
        self._format_message = format_message
        self._ping_interval = ping_interval
        self._send_timeout = send_timeout
        self._send_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        #: Set when the connection failed (send error or timeout) or stop() was called.
        self.closed = asyncio.Event()
        logger.debug(
            "WebSocketSubscriber created (patterns=%s, max_queue_size=%d, ping_interval=%.1fs)",
            patterns,
            max_queue_size,
            ping_interval,
        )

    async def on_event(self, envelope: EventEnvelope) -> None:
        if self.closed.is_set():
            return
        self._enqueue(envelope)

    async def start(self) -> None:
        if self._running:
            return
        self.closed.clear()
        await super().start()

    async def stop(self) -> None:
        self.closed.set()
        await super().stop()

    def _start_tasks(self) -> list[asyncio.Task[None]]:
        self._send_task = self._create_task(self._send_loop(), name=f"ws-send-{id(self):x}")
        self._ping_task = self._create_task(self._ping_loop(), name=f"ws-ping-{id(self):x}")
        return [self._send_task, self._ping_task]

    async def _send(self, message: str) -> None:
        async with self._send_lock:
            await asyncio.wait_for(self._ws.send_text(message), timeout=self._send_timeout)

    def _mark_closed(self, exc: BaseException) -> None:
        """The connection failed: stop sending, wake the owner, cancel the other loop."""
        if isinstance(exc, TimeoutError):
            reason = f"send timed out after {self._send_timeout:.1f}s"
        else:
            reason = f"{type(exc).__name__}: {exc}"
        logger.warning("WebSocket send failed (%s); closing subscriber", reason)
        self._running = False
        self._accepting = False
        self._stop_event.set()
        self.closed.set()
        current = asyncio.current_task()
        for task in (self._send_task, self._ping_task):
            if task is not None and task is not current and not task.done():
                task.cancel()

    def _render(self, envelope: EventEnvelope) -> str | None:
        """The message to send for *envelope*, or None to skip it (logged)."""
        if self._format_message is None:
            return _FORMAT_CACHE.get_or_format(
                envelope.event, lambda: _format_default_or_none(envelope)
            )
        try:
            return self._format_message(envelope)
        except Exception:
            logger.error(
                "format_message failed for event %s; skipping it",
                envelope.event.event_id,
                exc_info=True,
            )
            return None

    async def _send_loop(self) -> None:
        """Drain queue and send to WebSocket."""
        queue = self._queue
        try:
            while self._running:
                item = await queue.get()
                if not isinstance(item, EventEnvelope):
                    logger.debug("WS send loop received stop signal")
                    return
                message = self._render(item)
                if message is None:
                    continue
                try:
                    await self._send(message)
                except Exception as exc:  # includes TimeoutError from send_timeout
                    self._mark_closed(exc)
                    return
                logger.debug(
                    "WS sent event %s (topic=%s)",
                    item.event.event_id,
                    item.event.topic,
                )
        except asyncio.CancelledError:
            logger.debug("WS send loop cancelled")
            raise

    async def _ping_loop(self) -> None:
        """Send periodic ping messages to keep the connection alive."""
        stop_event = self._stop_event
        try:
            while self._running:
                try:
                    async with asyncio.timeout(self._ping_interval):
                        await stop_event.wait()
                except TimeoutError:
                    pass  # interval elapsed without a stop: time to ping
                else:
                    return  # stopped
                if not self._running:
                    return
                try:
                    await self._send(_PING_MESSAGE)
                except Exception as exc:
                    self._mark_closed(exc)
                    return
                logger.debug("WS ping sent")
        except asyncio.CancelledError:
            logger.debug("WS ping loop cancelled")
            raise

    @staticmethod
    def _default_format(envelope: EventEnvelope) -> str:
        return json_dumps(
            {
                "type": "event",
                "topic": envelope.event.topic,
                "data": envelope.event.data,
                "metadata": {
                    "event_id": envelope.event.metadata.event_id,
                    "timestamp": envelope.event.metadata.timestamp,
                    "source": envelope.event.metadata.source,
                    "correlation_id": envelope.event.metadata.correlation_id,
                },
            }
        )


def _format_default_or_none(envelope: EventEnvelope) -> str | None:
    try:
        return WebSocketSubscriber._default_format(envelope)
    except Exception:  # TypeError/ValueError from json_dumps, or anything unexpected
        logger.error(
            "Cannot encode event %s (topic=%r) for WebSocket; skipping it",
            envelope.event.event_id,
            envelope.event.topic,
            exc_info=True,
        )
        return None
