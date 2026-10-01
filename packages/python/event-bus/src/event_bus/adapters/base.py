from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterable, Sequence
from typing import Any

from event_bus.core.errors import InvalidTopicError
from event_bus.core.event import Event, EventEnvelope
from event_bus.core.topic import TopicPattern, validate_pattern
from event_bus.interfaces.subscriber import BaseSubscriber, EventHandler

logger = logging.getLogger(__name__)

#: Default seconds stop() lets in-flight work finish before cancelling it.
DEFAULT_STOP_TIMEOUT = 5.0
#: Default maximum number of client-supplied patterns per SSE/WebSocket subscriber.
DEFAULT_MAX_PATTERNS = 32
# Seconds stop() waits for a cancelled task to finish before abandoning it, so
# a handler that swallows CancelledError cannot make stop() hang.
_CANCEL_TIMEOUT = 1.0


class _Signal(enum.Enum):
    """Control items put on an adapter queue next to the events."""

    STOP = "stop"


QueueItem = EventEnvelope | _Signal


def validate_subscriber_patterns(
    patterns: Sequence[str | TopicPattern],
    *,
    max_patterns: int = DEFAULT_MAX_PATTERNS,
) -> list[TopicPattern]:
    """Validate client-supplied topic patterns and return them as TopicPatterns.

    :raises InvalidTopicError: if *patterns* is a bare string, has more than
        *max_patterns* entries, or any entry fails :func:`validate_pattern`.
    """
    if isinstance(patterns, str | bytes):
        raise InvalidTopicError("patterns must be a list of topic patterns, not a single string")
    if len(patterns) > max_patterns:
        raise InvalidTopicError(
            f"too many topic patterns ({len(patterns)}); the maximum is {max_patterns}"
        )
    validated: list[TopicPattern] = []
    for pattern in patterns:
        if isinstance(pattern, TopicPattern):
            validate_pattern(pattern.pattern)
            validated.append(pattern)
        else:
            validated.append(TopicPattern(validate_pattern(pattern)))
    return validated


class FormatCache:
    """Bounded cache of the default-formatted payload of recent events.

    The shared listener pushes the same :class:`Event` object to every matching
    subscriber, so N browser connections would otherwise encode the same event
    N times. Entries are keyed by ``event_id`` and only reused for the very
    same Event object, so two different events that share an ID never see each
    other's payload. ``None`` (a payload that could not be formatted) is cached
    too, so the failure is logged once per event rather than once per
    subscriber. Only default formatters use it; custom formatters bypass it.
    """

    def __init__(self, maxsize: int = 128, max_payload_chars: int = 1 << 20) -> None:
        self._maxsize = maxsize
        self._max_payload_chars = max_payload_chars
        self._entries: OrderedDict[str, tuple[Event, str | None]] = OrderedDict()
        self._lock = threading.Lock()  # adapters may live on several threads' loops

    def get_or_format(self, event: Event, format_event: Callable[[], str | None]) -> str | None:
        key = event.event_id
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] is event:
                return entry[1]
        payload = format_event()
        if payload is not None and len(payload) > self._max_payload_chars:
            return payload  # too big to keep around
        with self._lock:
            self._entries[key] = (event, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)
        return payload

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _log_task_failure(task: asyncio.Task[None]) -> None:
    """Done-callback: log (and so retrieve) an adapter task's unexpected exception."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Adapter task %s failed: %s", task.get_name(), exc, exc_info=exc)


async def stop_tasks(
    tasks: Iterable[asyncio.Task[None] | None],
    *,
    grace: float,
    owner: str,
) -> None:
    """Wait up to *grace* seconds for *tasks* to finish, then cancel the rest.

    Never hangs: a cancelled task that does not finish within a further
    second (it swallowed the cancellation) is logged and abandoned. The
    calling task is skipped, so a handler may stop its own subscriber. If the
    caller is itself cancelled while waiting, the tasks are cancelled too and
    the ``CancelledError`` propagates.
    """
    current = asyncio.current_task()
    pending = {t for t in tasks if t is not None and not t.done() and t is not current}
    try:
        if pending and grace > 0:
            _, pending = await asyncio.wait(pending, timeout=grace)
        if pending:
            logger.warning(
                "%s: %d task(s) still running after the %.1fs grace period; cancelling",
                owner,
                len(pending),
                grace,
            )
            for task in pending:
                task.cancel()
            _, pending = await asyncio.wait(pending, timeout=_CANCEL_TIMEOUT)
            for task in pending:
                logger.error(
                    "%s: task %s ignored cancellation; abandoning it", owner, task.get_name()
                )
    except asyncio.CancelledError:
        for task in pending:
            task.cancel()
        raise


class BufferedSubscriber(BaseSubscriber):
    """
    Base for adapters that buffer pushed events in a bounded queue.

    - ``on_event()`` never blocks: when the queue is full the event is dropped,
      :attr:`dropped_count` is incremented and a warning is logged -- the
      first drop, then at most once per :attr:`drop_log_interval` seconds.
    - Events pushed before the first ``start()`` are buffered; events pushed
      after ``stop()`` are ignored until the next ``start()``.
    - ``stop()`` wakes the consumer without blocking (a full queue means the
      consumer is not waiting on it), gives in-flight work up to
      ``stop_timeout`` seconds, cancels what is left and discards the unsent
      backlog (counted in :attr:`dropped_count`). It never hangs, can be
      called any number of times, and ``start()`` works again afterwards.

    Subclasses implement ``on_event()`` (usually just ``self._enqueue()``)
    and ``_start_tasks()``, and their consumers exit when they read
    ``_Signal.STOP`` or see ``self._running`` turn false.
    """

    #: Minimum seconds between two "queue full" warnings of one subscriber.
    drop_log_interval: float = 10.0

    def __init__(
        self,
        patterns: Sequence[str | TopicPattern],
        *,
        max_queue_size: int,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> None:
        if stop_timeout < 0:
            raise ValueError("stop_timeout must be >= 0")
        super().__init__(patterns)
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=max_queue_size)
        self._stop_timeout = stop_timeout
        self._accepting = True  # False from stop() until the next start()
        self._stop_event = asyncio.Event()  # a fresh one per run; set by stop()
        self._tasks: list[asyncio.Task[None]] = []
        self._lifecycle_lock = asyncio.Lock()
        self._dropped_count = 0
        self._dropped_at_last_log = 0
        self._last_drop_log = -math.inf

    @property
    def dropped_count(self) -> int:
        """Events dropped so far: queue full, or still queued when stopped."""
        return self._dropped_count

    @property
    def stop_timeout(self) -> float:
        return self._stop_timeout

    def _start_tasks(self) -> list[asyncio.Task[None]]:
        """Create this run's background tasks. Called by ``start()``."""
        return []

    def _create_task(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_log_task_failure)
        return task

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            if self._tasks:
                # Tasks of a previous run that ended without a completed
                # stop() (connection failure, cancelled stop): finish them
                # first so two consumers never share the queue.
                await stop_tasks(self._tasks, grace=self._stop_timeout, owner=type(self).__name__)
                self._tasks = []
                self._discard_backlog()
            self._purge_signals()
            await super().start()
            self._accepting = True
            if self._stop_event.is_set():
                # A fresh event, so a consumer still draining the previous run
                # keeps seeing its own (set) event and exits.
                self._stop_event = asyncio.Event()
            self._tasks = self._start_tasks()
            logger.debug("%s started", type(self).__name__)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            tasks = self._tasks
            if not self._running and not tasks:
                return
            logger.debug("%s stopping", type(self).__name__)
            await super().stop()
            self._accepting = False
            self._stop_event.set()
            self._signal_stop()
            try:
                await stop_tasks(tasks, grace=self._stop_timeout, owner=type(self).__name__)
            finally:
                # Keep tasks that are still winding down, so a repeated stop()
                # (e.g. after this one was cancelled) waits for them again.
                self._tasks = [t for t in tasks if not t.done()]
            self._discard_backlog()
            logger.debug("%s stopped", type(self).__name__)

    def _enqueue(self, envelope: EventEnvelope) -> None:
        if not self._accepting:
            logger.debug(
                "%s is stopped; ignoring event %s", type(self).__name__, envelope.event.event_id
            )
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._record_drop(envelope.event.event_id)
        else:
            logger.debug(
                "%s queued event %s (queue_size=%d)",
                type(self).__name__,
                envelope.event.event_id,
                self._queue.qsize(),
            )

    def _record_drop(self, event_id: str) -> None:
        self._dropped_count += 1
        now = time.monotonic()
        if now - self._last_drop_log < self.drop_log_interval:
            return
        logger.warning(
            "%s queue full (max_queue_size=%d): dropped event %s "
            "(%d dropped since the last report, %d in total)",
            type(self).__name__,
            self._queue.maxsize,
            event_id,
            self._dropped_count - self._dropped_at_last_log,
            self._dropped_count,
        )
        self._last_drop_log = now
        self._dropped_at_last_log = self._dropped_count

    def _signal_stop(self) -> None:
        """Wake a consumer blocked in ``queue.get()``; never blocks."""
        # A full queue means the consumer is not blocked in get(); it notices
        # ``_running`` is false after its current item.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_Signal.STOP)

    def _purge_signals(self) -> None:
        """Remove stale control items, keeping queued events in order."""
        items: list[QueueItem] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items:
            if isinstance(item, EventEnvelope):
                self._queue.put_nowait(item)

    def _discard_backlog(self) -> None:
        discarded = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, EventEnvelope):
                discarded += 1
        if discarded:
            self._dropped_count += discarded
            logger.warning(
                "%s stopped with %d queued event(s) undelivered; discarded them",
                type(self).__name__,
                discarded,
            )


class CallbackSubscriber(BufferedSubscriber):
    """
    Invokes an async callback when the bus pushes an event.
    Used for lightweight, stateless event reactions.

    Events are buffered in an internal queue and drained by a background
    task so that a slow handler never blocks the shared listener.

    ``stop()`` lets the handler call in progress finish (up to
    ``stop_timeout`` seconds, then it is cancelled) and discards events that
    were queued but not handled yet. The subscriber can be started again.
    """

    def __init__(
        self,
        patterns: Sequence[str | TopicPattern],
        handler: EventHandler,
        *,
        max_queue_size: int = 256,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> None:
        super().__init__(patterns, max_queue_size=max_queue_size, stop_timeout=stop_timeout)
        self._handler = handler
        self._drain_task: asyncio.Task[None] | None = None
        logger.debug(
            "CallbackSubscriber created (patterns=%s, max_queue_size=%d)",
            patterns,
            max_queue_size,
        )

    async def on_event(self, envelope: EventEnvelope) -> None:
        self._enqueue(envelope)

    def _start_tasks(self) -> list[asyncio.Task[None]]:
        self._drain_task = self._create_task(
            self._drain_loop(), name=f"callback-subscriber-drain-{id(self):x}"
        )
        return [self._drain_task]

    async def _drain_loop(self) -> None:
        queue = self._queue
        try:
            while self._running:
                item = await queue.get()
                if isinstance(item, _Signal):
                    logger.debug("CallbackSubscriber drain loop received stop signal")
                    return
                await self._dispatch(item)
        except asyncio.CancelledError:
            logger.debug("CallbackSubscriber drain loop cancelled")
            raise

    async def _dispatch(self, envelope: EventEnvelope) -> None:
        event_id = envelope.event.event_id
        logger.debug("CallbackSubscriber invoking handler for event %s", event_id)
        try:
            await self._handler(envelope.event)
        except Exception as e:
            logger.error(
                "Subscriber handler error for event %s: %s",
                event_id,
                e,
                exc_info=True,
            )
        else:
            logger.debug("CallbackSubscriber handler completed for event %s", event_id)
