from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from event_bus.config import EventBusConfig
from event_bus.consumer.group import ConsumerGroup, ConsumerGroupConfig
from event_bus.core.event import Event, EventMetadata
from event_bus.core.topic import validate_topic
from event_bus.interfaces.middleware import Middleware
from event_bus.interfaces.subscriber import EventHandler, Subscriber
from event_bus.interfaces.transport import IdempotencyStore, Transport
from event_bus.observability.metrics import MetricsCollector, NoOpMetrics
from event_bus.resilience.backpressure import BackpressureController

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[None]) -> None:
    """Callback attached to fire-and-forget tasks so exceptions aren't lost."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task %s failed: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


class EventBus:
    """
    The central orchestrator.

    Provides the public API for publishing events, registering subscribers,
    creating consumer groups, and managing lifecycle.

    Usage::

        transport = RedisTransport(RedisConfig())
        bus = EventBus(transport)

        @bus.consumer_group(["orders.created"], "order-processor")
        async def handle(event: Event) -> None:
              ...

        async with bus:
              await bus.publish("orders.created", {"order_id": "123"})
    """

    def __init__(
        self,
        transport: Transport,
        config: EventBusConfig | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._transport = transport
        self._config = config or EventBusConfig()
        self._metrics: MetricsCollector = metrics or NoOpMetrics()
        self._middlewares: list[Middleware] = []
        self._subscribers: list[Subscriber] = []
        self._consumer_groups: list[ConsumerGroup] = []
        self._backpressure = BackpressureController(
            max_pending=self._config.max_pending_publishes,
        )
        self._listener_task: asyncio.Task[None] | None = None
        self._pubsub_listener_error: str | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._idempotency: IdempotencyStore | None = None
        # Set by the shared listener once its subscription is confirmed.
        self._listener_ready: asyncio.Event | None = None
        self._started = False
        logger.debug(
            "EventBus created (max_pending_publishes=%d, shutdown_timeout=%.1fs)",
            self._config.max_pending_publishes,
            self._config.shutdown_timeout,
        )

    @property
    def transport(self) -> Transport:
        """The underlying transport (for adapter subscribers)."""
        return self._transport

    def _spawn_background_task(self, coro: Any, *, name: str = "") -> asyncio.Task[None]:
        """Create a tracked background task that logs exceptions."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return task

    async def _start_subscriber_and_listener(
        self,
        subscriber: Subscriber,
    ) -> None:
        await subscriber.start()
        self._ensure_shared_listener()

    def _subscriber_accepts(self, subscriber: Subscriber, topic: str) -> bool:
        return any(pattern.matches(topic) for pattern in subscriber.patterns)

    def _listener_is_running(self) -> bool:
        return self._listener_task is not None and not self._listener_task.done()

    def _ensure_shared_listener(self) -> None:
        if not self._started or not self._subscribers or self._listener_is_running():
            return
        self._pubsub_listener_error = None
        self._listener_ready = asyncio.Event()
        self._listener_task = asyncio.create_task(
            self._shared_listener(self._listener_ready),
            name="eventbus-shared-listener",
        )
        self._listener_task.add_done_callback(self._on_listener_done)
        logger.debug("Shared pub/sub listener task created")

    def _on_listener_done(self, task: asyncio.Task[None]) -> None:
        if self._listener_task is task:
            self._listener_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._pubsub_listener_error = str(exc)
            logger.error(
                "Shared pub/sub listener failed: %s",
                exc,
                exc_info=exc,
            )

    def _stop_shared_listener(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()

    async def _stop_shared_listener_and_wait(self) -> None:
        task = self._listener_task
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self._listener_task is task:
            self._listener_task = None
        logger.debug("Shared listener stopped")

    # -- Shared listener --

    async def _shared_listener(self, ready: asyncio.Event) -> None:
        """
        Continuously listens for events from pub/sub transport and distributes
        them to subscribed listeners. If any subscriber raises an exception,
        the error is logged without halting the listener. ``ready`` is set
        once the first subscription is confirmed. Pub/sub is at-most-once:
        events published while the listener reconnects are missed.
        """
        logger.debug("Shared pub/sub listener starting")
        backoff = 0.1
        while self._started and self._subscribers:
            try:
                async for envelope in self._transport.subscribe_pubsub(["**"], ready):
                    if not self._started or not self._subscribers:
                        break
                    self._pubsub_listener_error = None
                    backoff = 0.1
                    logger.debug(
                        "Shared listener received event %s on topic '%s'",
                        envelope.event.event_id,
                        envelope.event.topic,
                    )
                    subscribers = list(self._subscribers)  # snapshot to avoid mutation
                    matched = 0
                    for sub in subscribers:
                        if self._subscriber_accepts(sub, envelope.event.topic):
                            matched += 1
                            try:
                                await sub.on_event(envelope)
                            except Exception as e:
                                logger.error(
                                    "Error pushing event %s to subscriber: %s",
                                    envelope.event.event_id,
                                    e,
                                    exc_info=True,
                                )
                    logger.debug(
                        "Event %s dispatched to %d/%d subscribers",
                        envelope.event.event_id,
                        matched,
                        len(subscribers),
                    )
                if self._started and self._subscribers:
                    self._pubsub_listener_error = "pub/sub listener ended"
                    logger.warning(
                        "Shared pub/sub listener ended; retrying in %.1fs",
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
            except asyncio.CancelledError:
                logger.debug("Shared pub/sub listener cancelled")
                raise
            except Exception as e:
                self._pubsub_listener_error = str(e)
                logger.error(
                    "Shared pub/sub listener error; retrying in %.1fs: %s",
                    backoff,
                    e,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
        logger.debug("Shared pub/sub listener exiting")

    # -- Lifecycle --

    async def start(self) -> None:
        logger.debug("EventBus starting...")
        await self._transport.connect()
        try:
            if self._config.enable_idempotency:
                self._idempotency = self._transport.idempotency_store(
                    self._config.idempotency_ttl, self._config.idempotency_lease
                )
            for sub in self._subscribers:
                logger.debug("Starting subscriber: %s", sub)
                await sub.start()
            for cg in self._consumer_groups:
                logger.debug("Starting consumer group: %s", cg)
                cg.idempotency = self._idempotency
                await cg.start()
            self._started = True
            self._ensure_shared_listener()
            await self._wait_for_subscription()
        except Exception:
            # If any startup step fails, tear down what we already started
            logger.error("EventBus start failed, cleaning up", exc_info=True)
            await self.stop()
            raise
        logger.info(
            "EventBus started with %d subscribers, %d consumer groups",
            len(self._subscribers),
            len(self._consumer_groups),
        )

    async def _wait_for_subscription(self) -> None:
        """Wait until the shared listener's subscription is confirmed, so
        events published after start() reach the subscribers."""
        ready = self._listener_ready
        if ready is None or not self._listener_is_running():
            return
        try:
            await asyncio.wait_for(ready.wait(), timeout=self._config.subscribe_timeout)
        except TimeoutError:
            logger.warning(
                "The pub/sub subscription wasn't confirmed within %.1fs; "
                "events published until it is are missed by subscribers",
                self._config.subscribe_timeout,
            )

    # A timeout parameter rather than asyncio.timeout(): it bounds only the
    # consumer groups' and subscribers' shutdown, and the transport is
    # disconnected even when they overrun it.
    async def stop(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """
        Stop the EventBus gracefully. Cancels the shared listener,
        stops all consumer groups and subscribers, then disconnects
        the transport. If shutdown tasks exceed the timeout, the
        transport is still disconnected.
        """
        timeout = timeout or self._config.shutdown_timeout
        logger.info("EventBus shutting down (timeout=%.1fs)...", timeout)

        # Cancel the shared listener first
        await self._stop_shared_listener_and_wait()

        # Cancel any fire-and-forget background tasks
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Stop consumer groups (they let in-flight messages finish, up to the
        # timeout) and subscribers
        shutdown_tasks: list[Any] = []
        for cg in self._consumer_groups:
            shutdown_tasks.append(cg.stop(timeout=timeout))
        for sub in self._subscribers:
            shutdown_tasks.append(sub.stop())
        if shutdown_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*shutdown_tasks, return_exceptions=True),
                    timeout=timeout,
                )
                logger.debug("All shutdown tasks completed within timeout")
            except TimeoutError:
                logger.warning(
                    "Shutdown timed out after %.1fs, some tasks may not have stopped cleanly",
                    timeout,
                )

        # Always disconnect the transport, even if shutdown timed out
        await self._transport.disconnect()
        self._started = False
        logger.info("EventBus stopped.")

    # -- Publishing --

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        source: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        headers: dict[str, str] | None = None,
        event_id: str | None = None,
    ) -> str:
        """
        Publish an event with the given topic and data.

        Middleware hooks are invoked before and after publishing.
        Backpressure is applied to prevent overwhelming the transport.
        """
        metadata_kwargs: dict[str, Any] = {
            "source": source,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "headers": headers or {},
        }
        if event_id is not None:
            metadata_kwargs["event_id"] = event_id

        metadata = EventMetadata(**metadata_kwargs)
        event = Event(topic=topic, data=data, metadata=metadata)

        logger.debug(
            "Publishing event %s to topic '%s'",
            event.event_id,
            topic,
        )

        for mw in self._middlewares:
            event = await mw.before_publish(event)
            logger.debug(
                "Middleware %s.before_publish completed for event %s",
                type(mw).__name__,
                event.event_id,
            )

        validate_topic(event.topic)
        async with self._backpressure:
            stream_id = await self._transport.publish(event)
            self._count_published(event)

        logger.debug(
            "Event %s published to stream (stream_id=%s)",
            event.event_id,
            stream_id,
        )

        for mw in self._middlewares:
            await mw.after_publish(event, stream_id)
            logger.debug(
                "Middleware %s.after_publish completed for event %s",
                type(mw).__name__,
                event.event_id,
            )

        return stream_id

    async def publish_event(self, event: Event) -> str:
        """Publish a pre-constructed Event object."""
        logger.debug(
            "Publishing pre-constructed event %s to topic '%s'",
            event.event_id,
            event.topic,
        )
        for mw in self._middlewares:
            event = await mw.before_publish(event)
        validate_topic(event.topic)
        async with self._backpressure:
            stream_id = await self._transport.publish(event)
            self._count_published(event)
        for mw in self._middlewares:
            await mw.after_publish(event, stream_id)
        logger.debug(
            "Event %s published (stream_id=%s)",
            event.event_id,
            stream_id,
        )
        return stream_id

    async def publish_many(self, events: list[Event]) -> list[str]:
        """
        Publish multiple events. Middleware hooks are applied to each event.
        All events are sent in a single pipeline for efficiency.
        """
        logger.debug("Publishing batch of %d events", len(events))

        # Apply before_publish middleware to each event
        processed_events: list[Event] = []
        for event in events:
            for mw in self._middlewares:
                event = await mw.before_publish(event)
            processed_events.append(event)

        for event in processed_events:
            validate_topic(event.topic)
        async with self._backpressure:
            stream_ids = await self._transport.publish_many(processed_events)
            for event in processed_events:
                self._count_published(event)

        # Apply after_publish middleware to each event
        for event, stream_id in zip(processed_events, stream_ids, strict=False):
            for mw in self._middlewares:
                await mw.after_publish(event, stream_id)

        logger.debug(
            "Batch of %d events published (stream_ids=%s)",
            len(stream_ids),
            stream_ids,
        )
        return stream_ids

    def _count_published(self, event: Event) -> None:
        self._metrics.increment(
            "events.published", tags={"topic": self._config.metric_topic(event.topic)}
        )

    # -- Subscribing --

    def subscribe(
        self,
        *patterns: str,
        handler: EventHandler | None = None,
    ) -> Callable[..., Any]:
        """
        Subscribe a handler to the given event topic patterns.
        Can be used as a decorator or called directly with a handler.
        """
        from event_bus.adapters.base import CallbackSubscriber

        def decorator(fn: EventHandler) -> EventHandler:
            sub = CallbackSubscriber(
                patterns=list(patterns),
                handler=fn,
            )
            self._subscribers.append(sub)
            logger.debug(
                "Registered callback subscriber for patterns %s",
                list(patterns),
            )
            if self._started:
                self._spawn_background_task(
                    self._start_subscriber_and_listener(sub),
                    name=f"subscriber-start-{id(sub)}",
                )
            return fn

        if handler is not None:
            decorator(handler)
            return handler
        return decorator

    def add_subscriber(self, subscriber: Subscriber) -> None:
        """Add a subscriber (SSE, WebSocket, or custom)."""
        self._subscribers.append(subscriber)
        logger.debug("Added subscriber: %s", subscriber)
        if self._started:
            self._spawn_background_task(
                self._start_subscriber_and_listener(subscriber),
                name=f"subscriber-start-{id(subscriber)}",
            )

    def remove_subscriber(self, subscriber: Subscriber) -> None:
        """
        Remove a subscriber so the shared listener stops pushing events to it.
        The caller is responsible for calling ``await subscriber.stop()``.
        """
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)
            logger.debug("Removed subscriber: %s", subscriber)
            if not self._subscribers:
                self._stop_shared_listener()
        else:
            logger.debug("Attempted to remove subscriber not in list: %s", subscriber)

    # -- Consumer Groups --

    def consumer_group(
        self,
        topics: list[str],
        group_name: str,
        config: ConsumerGroupConfig | None = None,
    ) -> Callable[..., Any]:
        """
        Decorator to register a consumer group handler.

        The handler mode is detected automatically from type hints:

        - ``async def handle(event: Event)`` -- single mode, called once per event
        - ``async def handle(events: list[Event])`` -- batch mode, called with the
          topic's XREADGROUP batch

        ::

            @bus.consumer_group(["orders.created"], "order-processor")
            async def handle_order(event: Event) -> None:
                  ...

            @bus.consumer_group(["orders.created"], "order-batch-processor")
            async def handle_batch(events: list[Event]) -> None:
                  await db.insert_many([e.data for e in events])
        """

        def decorator(fn: EventHandler) -> EventHandler:
            cg = ConsumerGroup(
                transport=self._transport,
                topics=topics,
                group_name=group_name,
                handler=fn,
                config=config or ConsumerGroupConfig(),
                metrics=self._metrics,
                idempotency=self._idempotency,
                metric_topic=self._config.metric_topic,
            )
            self._consumer_groups.append(cg)
            logger.debug(
                "Registered consumer group '%s' for topics %s",
                group_name,
                topics,
            )
            if self._started:
                self._spawn_background_task(
                    cg.start(),
                    name=f"cg-start-{group_name}",
                )
            return fn

        return decorator

    def create_consumer_group(
        self,
        topics: list[str],
        group_name: str,
        handler: Any,  # EventHandler or BatchEventHandler (auto-detected)
        config: ConsumerGroupConfig | None = None,
    ) -> ConsumerGroup:
        """Imperatively create and register a consumer group.

        Handler mode is auto-detected from type hints.  See
        :meth:`consumer_group` for details.
        """
        cg = ConsumerGroup(
            transport=self._transport,
            topics=topics,
            group_name=group_name,
            handler=handler,
            config=config or ConsumerGroupConfig(),
            metrics=self._metrics,
            idempotency=self._idempotency,
            metric_topic=self._config.metric_topic,
        )
        self._consumer_groups.append(cg)
        logger.debug(
            "Created consumer group '%s' for topics %s",
            group_name,
            topics,
        )
        if self._started:
            self._spawn_background_task(
                cg.start(),
                name=f"cg-start-{group_name}",
            )
        return cg

    # -- Dead letters --

    async def replay_dead_letters(self, group_name: str, count: int = 100) -> tuple[int, int]:
        """
        Handle a consumer group's dead letters again, in that group only.
        Each that succeeds leaves its DLQ.

        :return: How many succeeded, and how many failed or couldn't be replayed.
        :raises KeyError: No consumer group of that name is registered.
        """
        for cg in self._consumer_groups:
            if cg.group_name == group_name:
                return await cg.replay_dead_letters(count)
        raise KeyError(f"No consumer group named {group_name!r}")

    # -- Middleware --

    def use(self, middleware: Middleware) -> None:
        """Add a middleware to the middleware stack."""
        self._middlewares.append(middleware)
        logger.debug("Added middleware: %s", type(middleware).__name__)

    # -- Health --

    async def health_check(self) -> dict[str, Any]:
        """Check health of the bus and its transport."""
        transport_healthy = await self._transport.is_healthy()
        pubsub_required = bool(self._subscribers)
        pubsub_listener_running = self._listener_is_running()
        health = {
            "healthy": (
                transport_healthy
                and self._started
                and (not pubsub_required or pubsub_listener_running)
            ),
            "transport": transport_healthy,
            "started": self._started,
            "subscribers": len(self._subscribers),
            "consumer_groups": len(self._consumer_groups),
            "pubsub_listener_running": pubsub_listener_running,
            "pubsub_listener_error": self._pubsub_listener_error,
        }
        logger.debug("Health check result: %s", health)
        return health

    # -- Context manager --

    async def __aenter__(self) -> EventBus:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()
