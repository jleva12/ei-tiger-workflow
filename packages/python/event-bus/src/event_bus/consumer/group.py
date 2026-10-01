from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import typing
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from event_bus.consumer.retry import ExponentialBackoff, RetryPolicy
from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.interfaces.transport import (
    UNDECODABLE_HEADER,
    IdempotencyStore,
    NoGroupError,
    Transport,
)

logger = logging.getLogger(__name__)

# How long stop() lets workers finish their current message by default.
DEFAULT_STOP_TIMEOUT = 30.0
# Attempts to acknowledge a processed message before leaving it to redelivery.
ACK_ATTEMPTS = 3
# Messages identified by (topic, stream ID).
MessageKey = tuple[str, str]


def _is_batch_handler(handler: Any) -> bool:
    """Detect whether a handler expects list[Event] instead of Event.

    Inspects the type annotation of the first parameter.  Returns True
    when the annotation is ``list[Event]`` (or ``typing.List[Event]``).
    Falls back to False for untyped parameters so existing handlers
    continue to work as single-event processors.
    """
    try:
        hints = typing.get_type_hints(handler)
    except Exception:
        return False

    sig = inspect.signature(handler)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    if not params:
        return False

    hint = hints.get(params[0].name)
    if hint is None:
        return False

    origin = typing.get_origin(hint)
    if origin is list:
        args = typing.get_args(hint)
        if args and args[0] is Event:
            return True
    return False


@dataclass
class ConsumerGroupConfig:
    """
    Configuration class for consumer group settings.

    Delivery is at-least-once. A worker reads a batch and keeps a lease on
    every message it hasn't finished (renewed every ``heartbeat_interval``).
    A message whose lease lapses for ``claim_idle_ms`` -- its consumer died
    -- is taken over by another consumer's reclaimer. A failed message is
    retried after ``retry_policy.delay(attempt)`` (capped at
    ``claim_idle_ms``) without holding up the worker, and dead-lettered once
    it has been retried ``max_retries`` times.

    :ivar consumer_name: A unique identifier for the consumer (process).
    :ivar concurrency: Number of concurrent worker tasks.
    :ivar batch_size: Messages per XREADGROUP call.
    :ivar block_ms: XREADGROUP block timeout in milliseconds; keep it below
        the transport's blocking socket timeout.
    :ivar retry_policy: Strategy for computing retry delays.
    :ivar max_retries: Maximum retries before dead-lettering.
    :ivar claim_idle_ms: The lease: how long a message may go unrenewed
        before another consumer takes it over. Also the longest retry delay.
    :ivar claim_interval: Seconds between reclaim sweeps (retries and dead
        consumers' messages are picked up this often).
    :ivar heartbeat_interval: Seconds between lease renewals of in-flight
        messages; by default a third of the lease.
    :ivar start_id: Where a newly created group starts: ``"0"`` (the whole
        retained stream) or ``"$"`` (only events published from now on).
    :ivar trim_interval: Seconds between trims of what every group has
        consumed (None disables).
    :ivar idle_consumer_ms: Remove the group's consumers idle this long with
        nothing pending, e.g. dead processes' names (None disables).
    """

    consumer_name: str = field(default_factory=lambda: f"{os.getpid()}-{uuid.uuid4().hex[:8]}")
    concurrency: int = 1
    batch_size: int = 10
    block_ms: int = 2000
    retry_policy: RetryPolicy = field(default_factory=ExponentialBackoff)
    max_retries: int = 5
    claim_idle_ms: int = 60_000
    claim_interval: float = 1.0
    heartbeat_interval: float | None = None
    start_id: str = "0"
    trim_interval: float | None = 60.0
    idle_consumer_ms: int | None = 3_600_000

    def __post_init__(self) -> None:
        if self.concurrency < 1 or self.batch_size < 1:
            raise ValueError("concurrency and batch_size must be at least 1")
        if self.max_retries < 0:
            raise ValueError("max_retries can't be negative")
        if self.claim_idle_ms <= 0 or self.claim_interval <= 0:
            raise ValueError("claim_idle_ms and claim_interval must be positive")
        if (
            self.heartbeat_interval is not None
            and self.heartbeat_interval * 1000 >= self.claim_idle_ms
        ):
            raise ValueError("heartbeat_interval must be shorter than the lease (claim_idle_ms)")

    @property
    def lease_renewal_interval(self) -> float:
        if self.heartbeat_interval is not None:
            return self.heartbeat_interval
        return self.claim_idle_ms / 3000

    def retry_delay_ms(self, attempt: int) -> int:
        """The wait before retrying, capped at the lease (a message can't be
        held back longer than that)."""
        return max(0, min(int(self.retry_policy.delay(attempt) * 1000), self.claim_idle_ms))


class ConsumerGroup:
    """
    A consumer group that reads from Redis Streams, processes events with the
    provided handler, acknowledges on success, and retries or dead-letters on
    failure.
    """

    def __init__(
        self,
        transport: Transport,
        topics: list[str],
        group_name: str,
        handler: Any,  # EventHandler or BatchEventHandler
        config: ConsumerGroupConfig,
        metrics: Any = None,
        idempotency: IdempotencyStore | None = None,
        metric_topic: Callable[[str], str] | None = None,
    ) -> None:
        self._transport = transport
        self._topics = topics
        self._group_name = group_name
        self._handler = handler
        self._batch_mode = _is_batch_handler(handler)
        self._config = config
        self._metrics = metrics
        self.idempotency = idempotency
        self._metric_topic = metric_topic or (lambda topic: topic)
        self._workers: list[asyncio.Task[None]] = []
        self._reclaimer_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        logger.debug(
            "ConsumerGroup '%s' handler mode: %s",
            group_name,
            "batch" if self._batch_mode else "single",
        )

    @property
    def group_name(self) -> str:
        return self._group_name

    @property
    def topics(self) -> list[str]:
        return list(self._topics)

    def _worker_names(self) -> list[str]:
        return [f"{self._config.consumer_name}-{i}" for i in range(self._config.concurrency)]

    def _reclaimer_name(self) -> str:
        return f"{self._config.consumer_name}-reclaimer"

    # -- Lifecycle --

    async def start(self) -> None:
        """Create the consumer groups in Redis and launch worker + reclaimer tasks."""
        await self._ensure_groups()
        self._stopping = asyncio.Event()
        for name in self._worker_names():
            self._workers.append(
                asyncio.create_task(
                    self._worker_loop(name), name=f"cg-worker-{self._group_name}-{name}"
                )
            )
        self._reclaimer_task = asyncio.create_task(
            self._reclaim_loop(), name=f"cg-reclaimer-{self._group_name}"
        )
        logger.info(
            "ConsumerGroup '%s' started with %d workers for topics %s",
            self._group_name,
            self._config.concurrency,
            self._topics,
        )

    # A timeout parameter rather than asyncio.timeout(): it bounds how long
    # in-flight messages get to finish before they're cancelled, not the call.
    async def stop(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """
        Stop gracefully: workers finish the message in hand and hand the rest
        of their batch back (claimable at once by other consumers); anything
        still running after ``timeout`` seconds is cancelled, its messages
        left to be redelivered.
        """
        self._stopping.set()
        tasks = [*self._workers, *([self._reclaimer_task] if self._reclaimer_task else [])]
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=DEFAULT_STOP_TIMEOUT if timeout is None else timeout
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._reclaimer_task = None
        logger.info("ConsumerGroup '%s' stopped.", self._group_name)

    async def _ensure_groups(self) -> None:
        for topic in self._topics:
            await self._transport.create_consumer_group(
                topic, self._group_name, self._config.start_id
            )

    async def _recreate_groups(self, error: Exception) -> None:
        logger.warning(
            "Consumer group '%s' is missing (%s); recreating it", self._group_name, error
        )
        try:
            await self._ensure_groups()
        except Exception:
            logger.error("Couldn't recreate consumer group '%s'", self._group_name, exc_info=True)
            await self._sleep(1.0)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, waking early when the group stops."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # -- Reading --

    async def _worker_loop(self, consumer_name: str) -> None:
        """Continuously read and process messages from the transport."""
        while not self._stopping.is_set():
            try:
                envelopes = await self._transport.read_group_many(
                    topics=self._topics,
                    group_name=self._group_name,
                    consumer_name=consumer_name,
                    count=self._config.batch_size,
                    block_ms=self._config.block_ms,
                )
            except NoGroupError as e:
                await self._recreate_groups(e)
                continue
            except Exception as e:
                logger.error("Worker '%s' error: %s", consumer_name, e, exc_info=True)
                await self._sleep(1.0)
                continue
            if envelopes:
                await self._process(envelopes, consumer_name)

    async def _reclaim_loop(self) -> None:
        """Take over messages whose lease lapsed (dead consumers' messages,
        and failed messages whose retry is due), and keep the streams tidy."""
        consumer = self._reclaimer_name()
        loop = asyncio.get_running_loop()
        last_trim = last_prune = loop.time()
        prune_every = (
            max(60.0, self._config.idle_consumer_ms / 10_000)
            if self._config.idle_consumer_ms
            else None
        )
        while not self._stopping.is_set():
            await self._sleep(self._config.claim_interval)
            if self._stopping.is_set():
                break
            for topic in self._topics:
                try:
                    claimed = await self._transport.claim_idle(
                        topic,
                        self._group_name,
                        consumer,
                        self._config.claim_idle_ms,
                        count=self._config.batch_size,
                    )
                except NoGroupError as e:
                    await self._recreate_groups(e)
                    continue
                except Exception as e:
                    logger.error("Reclaimer error on '%s': %s", topic, e, exc_info=True)
                    continue
                if claimed:
                    logger.debug(
                        "Reclaimer took over %d message(s) on '%s' for group '%s'",
                        len(claimed),
                        topic,
                        self._group_name,
                    )
                    await self._process(claimed, consumer)
            now = loop.time()
            if self._config.trim_interval and now - last_trim >= self._config.trim_interval:
                last_trim = now
                await self._housekeep(self._trim)
            if prune_every and now - last_prune >= prune_every:
                last_prune = now
                await self._housekeep(self._prune_consumers)

    async def _housekeep(self, job: Callable[[str], Any]) -> None:
        for topic in self._topics:
            try:
                await job(topic)
            except Exception as e:
                logger.warning("Housekeeping on '%s' failed: %s", topic, e)

    async def _trim(self, topic: str) -> None:
        removed = await self._transport.trim_consumed(topic)
        if removed:
            logger.debug("Trimmed %d consumed entries from '%s'", removed, topic)

    async def _prune_consumers(self, topic: str) -> None:
        if self._config.idle_consumer_ms is None:
            return
        await self._transport.remove_idle_consumers(
            topic,
            self._group_name,
            self._config.idle_consumer_ms,
            keep={*self._worker_names(), self._reclaimer_name()},
        )

    # -- Processing --

    async def _process(self, envelopes: list[EventEnvelope], consumer_name: str) -> None:
        """
        Handle a batch the consumer holds, renewing the lease on every message
        not yet settled. On stop, hands the unstarted rest back at once.
        """
        unsettled: dict[MessageKey, EventEnvelope] = {
            (env.event.topic, env.stream_id): env for env in envelopes
        }

        def settled(*items: EventEnvelope) -> None:
            for env in items:
                unsettled.pop((env.event.topic, env.stream_id), None)

        heartbeat = asyncio.create_task(
            self._renew_leases(unsettled, consumer_name),
            name=f"cg-heartbeat-{self._group_name}-{consumer_name}",
        )
        try:
            if self._batch_mode:
                by_topic: dict[str, list[EventEnvelope]] = defaultdict(list)
                for env in envelopes:
                    by_topic[env.event.topic].append(env)
                for topic, topic_envelopes in by_topic.items():
                    if self._stopping.is_set():
                        break
                    await self._handle_batch(topic, topic_envelopes, consumer_name, settled)
            else:
                for env in envelopes:
                    if self._stopping.is_set():
                        break
                    await self._handle_one(env, consumer_name, settled)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            if unsettled:
                await self._hand_back(list(unsettled.values()), consumer_name)

    async def _renew_leases(
        self, unsettled: dict[MessageKey, EventEnvelope], consumer_name: str
    ) -> None:
        interval = self._config.lease_renewal_interval
        while True:
            await asyncio.sleep(interval)
            by_topic: dict[str, list[str]] = defaultdict(list)
            for topic, stream_id in list(unsettled):
                by_topic[topic].append(stream_id)
            for topic, ids in by_topic.items():
                try:
                    await self._transport.touch(topic, self._group_name, consumer_name, ids, 0)
                except Exception as e:
                    logger.warning("Couldn't renew the lease on %d message(s): %s", len(ids), e)

    async def _hand_back(self, envelopes: list[EventEnvelope], consumer_name: str) -> None:
        """Make unprocessed messages claimable by other consumers now."""
        by_topic: dict[str, list[str]] = defaultdict(list)
        for env in envelopes:
            by_topic[env.event.topic].append(env.stream_id)
        for topic, ids in by_topic.items():
            try:
                async with asyncio.timeout(5.0):
                    await self._transport.touch(
                        topic, self._group_name, consumer_name, ids, self._config.claim_idle_ms
                    )
            except Exception as e:
                logger.warning(
                    "Couldn't hand %d message(s) back (redelivered after the lease): %s",
                    len(ids),
                    e,
                )

    async def _begin(
        self, env: EventEnvelope
    ) -> tuple[typing.Literal["new", "done", "busy"], str | None]:
        if self.idempotency is None:
            return "new", None
        return await self.idempotency.begin(self._group_name, env.event.event_id)

    async def _finish(self, env: EventEnvelope, token: str | None, ok: bool) -> None:
        if self.idempotency is None or token is None:
            return
        try:
            if ok:
                await self.idempotency.complete(self._group_name, env.event.event_id, token)
            else:
                await self.idempotency.release(self._group_name, env.event.event_id, token)
        except Exception as e:
            logger.warning("Idempotency update for event %s failed: %s", env.event.event_id, e)

    async def _handle_one(
        self,
        envelope: EventEnvelope,
        consumer_name: str,
        settled: Callable[..., None],
    ) -> None:
        """Process a single event: call handler, ack, or retry/DLQ on failure."""
        topic = envelope.event.topic
        envelope, retry_count = self._with_effective_retry(envelope)
        try:
            status, token = await self._begin(envelope)
        except Exception as e:
            settled(envelope)
            await self._on_failure(topic, consumer_name, [(envelope, retry_count)], e)
            return
        if status == "done":
            logger.debug("Event %s already processed by '%s'", envelope.event.event_id, topic)
            self._count("events.duplicate", topic)
            await self._ack(topic, [envelope])
            settled(envelope)
            return
        if status == "busy":
            # Another consumer is processing the same event: look again later.
            settled(envelope)
            await self._release(topic, consumer_name, [(envelope, 0)])
            return
        try:
            await self._handler(envelope.event)
        except Exception as e:
            settled(envelope)
            await self._finish(envelope, token, ok=False)
            await self._on_failure(topic, consumer_name, [(envelope, retry_count)], e)
            return
        # The lease holds until the ack is through (or given up), so the
        # event isn't reclaimed while the ack is being retried.
        await self._finish(envelope, token, ok=True)
        await self._ack(topic, [envelope])
        settled(envelope)
        self._count("events.processed", topic)

    async def _handle_batch(
        self,
        topic: str,
        envelopes: list[EventEnvelope],
        consumer_name: str,
        settled: Callable[..., None],
    ) -> None:
        """Process a batch of one topic's events with the batch handler; ack
        all on success, else retry or dead-letter each by its own count."""
        items = [self._with_effective_retry(env) for env in envelopes]
        tokens: dict[str, str | None] = {}
        todo: list[tuple[EventEnvelope, int]] = []
        for env, retry_count in items:
            try:
                status, token = await self._begin(env)
            except Exception as e:
                settled(env)
                await self._on_failure(topic, consumer_name, [(env, retry_count)], e)
                continue
            if status == "done":
                self._count("events.duplicate", topic)
                await self._ack(topic, [env])
                settled(env)
            elif status == "busy":
                settled(env)
                await self._release(topic, consumer_name, [(env, 0)])
            else:
                tokens[env.stream_id] = token
                todo.append((env, retry_count))
        if not todo:
            return
        batch = [env for env, _ in todo]
        try:
            await self._handler([env.event for env in batch])
        except Exception as e:
            settled(*batch)
            for env in batch:
                await self._finish(env, tokens.get(env.stream_id), ok=False)
            await self._on_failure(topic, consumer_name, todo, e)
            return
        for env in batch:
            await self._finish(env, tokens.get(env.stream_id), ok=True)
        await self._ack(topic, batch)
        settled(*batch)
        self._count("events.processed", topic, batch_size=str(len(batch)))

    async def _ack(self, topic: str, envelopes: list[EventEnvelope]) -> None:
        """Acknowledge processed messages. A failure here isn't the handler's:
        the messages stay pending and are redelivered (at-least-once)."""
        ids = [env.stream_id for env in envelopes]
        for attempt in range(ACK_ATTEMPTS):
            try:
                await self._transport.ack_many(topic, self._group_name, ids)
                return
            except Exception as e:
                if attempt == ACK_ATTEMPTS - 1:
                    logger.error(
                        "Couldn't acknowledge %d processed message(s) on '%s'; they'll be "
                        "redelivered: %s",
                        len(ids),
                        topic,
                        e,
                    )
                    self._count("events.ack_failed", topic)
                    return
                await asyncio.sleep(0.1 * (attempt + 1))

    async def _on_failure(
        self,
        topic: str,
        consumer_name: str,
        items: list[tuple[EventEnvelope, int]],
        error: Exception,
    ) -> None:
        """Dead-letter the messages out of retries; schedule the rest's retry."""
        first = items[0][0].event.event_id
        logger.error(
            "Handler failed for %s on '%s' (group '%s', retry %d/%d): %s",
            f"event {first}" if len(items) == 1 else f"{len(items)} events",
            topic,
            self._group_name,
            max(rc for _, rc in items),
            self._config.max_retries,
            error,
            exc_info=error,
        )
        self._count(
            "events.failed",
            topic,
            **({"batch_size": str(len(items))} if self._batch_mode else {}),
        )
        reason = f"{type(error).__name__}: {error}"
        retryable: list[tuple[EventEnvelope, int]] = []
        for env, retry_count in items:
            if retry_count < self._config.max_retries:
                retryable.append((env, retry_count))
                continue
            try:
                await self._transport.dead_letter(env, reason, self._group_name)
                self._count("events.dead_lettered", topic)
            except Exception as e:
                # Still pending: retried (and dead-lettered) after the lease.
                logger.error("Couldn't dead-letter event %s: %s", env.event.event_id, e)
        if retryable:
            await self._release(topic, consumer_name, retryable)

    async def _release(
        self, topic: str, consumer_name: str, items: list[tuple[EventEnvelope, int]]
    ) -> None:
        """Schedule each message's retry after its delay: set its idle time so
        the reclaimer takes it over once the delay has passed."""
        by_idle: dict[int, list[str]] = defaultdict(list)
        for env, retry_count in items:
            delay_ms = self._config.retry_delay_ms(retry_count)
            by_idle[self._config.claim_idle_ms - delay_ms].append(env.stream_id)
        for idle_ms, ids in by_idle.items():
            try:
                await self._transport.touch(topic, self._group_name, consumer_name, ids, idle_ms)
            except Exception as e:
                logger.warning(
                    "Couldn't schedule the retry of %d message(s) (retried after the lease): %s",
                    len(ids),
                    e,
                )

    # -- Dead letters --

    async def replay_dead_letters(self, count: int = 100) -> tuple[int, int]:
        """
        Handle this group's dead letters again, in this group only (groups
        that processed the events aren't involved). Each that succeeds leaves
        the DLQ; each that fails stays.

        :return: How many succeeded, and how many failed or couldn't be replayed.
        """
        letters = await self._transport.read_dlq(count, group_name=self._group_name)
        replayed = failed = 0
        for letter in letters:
            if letter.event.metadata.headers.get(UNDECODABLE_HEADER):
                failed += 1  # nothing to hand a handler
                continue
            try:
                if self._batch_mode:
                    await self._handler([letter.event])
                else:
                    await self._handler(letter.event)
            except Exception as e:
                failed += 1
                logger.warning("Replaying dead letter %s failed: %s", letter.stream_id, e)
                continue
            await self._transport.delete_dlq(letter.stream_id, group_name=self._group_name)
            replayed += 1
        return replayed, failed

    # -- Helpers --

    def _count(self, name: str, topic: str, **tags: str) -> None:
        if self._metrics:
            self._metrics.increment(
                name,
                tags={"topic": self._metric_topic(topic), "group": self._group_name, **tags},
            )

    @staticmethod
    def _with_effective_retry(
        envelope: EventEnvelope,
    ) -> tuple[EventEnvelope, int]:
        """Return an envelope whose event metadata reflects delivery attempts."""
        delivery_retry_count = max(envelope.delivery_count - 1, 0)
        retry_count = max(
            envelope.event.metadata.retry_count,
            delivery_retry_count,
        )
        if retry_count == envelope.event.metadata.retry_count:
            return envelope, retry_count

        metadata = EventMetadata(
            event_id=envelope.event.metadata.event_id,
            timestamp=envelope.event.metadata.timestamp,
            source=envelope.event.metadata.source,
            correlation_id=envelope.event.metadata.correlation_id,
            causation_id=envelope.event.metadata.causation_id,
            content_type=envelope.event.metadata.content_type,
            retry_count=retry_count,
            headers=dict(envelope.event.metadata.headers),
        )
        event = Event(
            topic=envelope.event.topic,
            data=envelope.event.data,
            metadata=metadata,
        )
        return replace(envelope, event=event), retry_count
