# Event Bus

A production-grade, framework-agnostic Python event bus backed by Redis. Designed for both **server-to-server communication** between application components and **real-time push to browser clients** via SSE and WebSocket adapters.

Built on a hybrid **Redis Streams + Pub/Sub** architecture: Streams provide durable, ordered, at-least-once delivery for backend processors, while Pub/Sub provides sub-millisecond fan-out for real-time UI updates.

---

## Table of Contents

- [Architecture](#architecture)
  - [High-Level Overview](#high-level-overview)
  - [Publish Flow (Dual-Write)](#publish-flow-dual-write)
  - [Server-Side Consumer Groups](#server-side-consumer-groups)
  - [Real-Time Browser Push](#real-time-browser-push)
  - [Failure Handling](#failure-handling)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Guide](#usage-guide)
  - [1. Publishing Events](#1-publishing-events)
  - [2. Server-Side: Consumer Groups](#2-server-side-consumer-groups)
  - [3. Server-Side: Lightweight Subscribers](#3-server-side-lightweight-subscribers)
  - [4. Frontend: SSE Adapter](#4-frontend-sse-adapter)
  - [5. Frontend: WebSocket Adapter](#5-frontend-websocket-adapter)
  - [6. Frontend: JavaScript Client Examples](#6-frontend-javascript-client-examples)
  - [7. Middleware](#7-middleware)
  - [8. Health Checks](#8-health-checks)
- [Configuration Reference](#configuration-reference)
- [Topic Patterns](#topic-patterns)
- [Serialization](#serialization)
- [Resilience Features](#resilience-features)
  - [Circuit Breaker](#circuit-breaker)
  - [Backpressure](#backpressure)
  - [Dead Letter Queue](#dead-letter-queue)
  - [Retry with Backoff](#retry-with-backoff)
  - [Slow Consumer Protection](#slow-consumer-protection)
- [Connection Lifecycle & Cleanup](#connection-lifecycle--cleanup)
  - [Graceful Shutdown](#graceful-shutdown)
  - [Startup Failure Safety](#startup-failure-safety)
  - [Background Task Tracking](#background-task-tracking)
  - [Resource Cleanup on Disconnect](#resource-cleanup-on-disconnect)
- [Debug Logging](#debug-logging)
- [Testing](#testing)
- [Full End-to-End Example](#full-end-to-end-example)
  - [Bundled examples](#bundled-examples)
- [Package Structure](#package-structure)

---

## Architecture

### High-Level Overview

![High-Level Overview](docs/diagrams/high-level-overview.mmd)

### Publish Flow (Dual-Write)

Every call to `bus.publish()` writes to **both** Redis Streams and Pub/Sub in sequence:

![Publish Flow](docs/diagrams/publish-flow.mmd)

**Why dual-write?**
- **Streams** are the source of truth -- durable, ordered, replayable, with consumer group semantics
- **Pub/Sub** provides instant push -- no polling delay, but fire-and-forget (no persistence)
- If Pub/Sub fails, the event is still safe in the Stream. Consumer groups always get it. Browser clients may miss the instant notification but can catch up via other mechanisms.

### Server-Side Consumer Groups

Consumer groups use Redis Streams for reliable, at-least-once delivery:

![Consumer Groups](docs/diagrams/consumer-groups.mmd)

### Real-Time Browser Push

Browser clients connect via SSE or WebSocket. The adapters subscribe to Redis Pub/Sub and forward events:

![Browser Push](docs/diagrams/browser-push.mmd)

### Failure Handling

![Circuit Breaker States](docs/diagrams/circuit-breaker-states.mmd)

---

## Installation

The package is a uv project in this monorepo, `packages/python/event-bus`: the
distribution `event-bus`, whose module `event_bus` lives under `src/event_bus/`.
Its only runtime dependency is `redis`. An application depends on it with a
path source in its `pyproject.toml`:

```toml
[project]
dependencies = ["event-bus"]

[tool.uv.sources]
event-bus = { path = "../../packages/python/event-bus", editable = true }
```

For msgpack serialization (optional, more compact), depend on
`event-bus[msgpack]` instead.

For development, uv manages the package's own environment (`.venv`, locked in
`uv.lock`); run its tools with `uv run` in the package directory:

```bash
cd packages/python/event-bus
uv sync                # the package, redis and the dev group (ruff, mypy, pytest, msgpack)
uv run pytest

# From the repository root
make event-bus-check   # ruff check, ruff format --check, mypy src and pytest
make event-bus-fmt     # ruff check --fix and ruff format
```

For a local Redis, start the monorepo's shared one from the repository root. It
listens on `redis://127.0.0.1:16389/0` by default (`FORGE_REDIS_URL` in
`.env.common`); the bus keeps its keys under its `key_prefix` (`eventbus:` by
default), apart from the other users of that database:

```bash
make shared-deps
```

---

## Quick Start

```python
import asyncio
from event_bus import EventBus, Event
from event_bus.transport.redis.transport import RedisTransport
from event_bus.config import RedisConfig


async def main():
    # The monorepo's shared Redis (make shared-deps)
    transport = RedisTransport(RedisConfig(url="redis://127.0.0.1:16389/0"))

    async with EventBus(transport) as bus:
        # Publish an event
        await bus.publish(
            "orders.created",
            {"order_id": "ord-123", "total": 99.99},
            source="order-service",
        )


asyncio.run(main())
```

The `async with` block connects to Redis on enter and performs a full graceful shutdown on exit -- all consumer groups are stopped, all subscriber drain loops are cancelled, background tasks are awaited, and Redis connections are closed.

---

## Usage Guide

### 1. Publishing Events

#### Simple publish

```python
stream_id = await bus.publish(
    topic="orders.created",
    data={
        "order_id": "ord-12345",
        "customer_id": "cust-99",
        "total": 149.99,
        "items": [{"sku": "WIDGET-A", "qty": 3}],
    },
    source="order-service",  # originating service
    correlation_id="req-abc-123",  # trace through the system
)
print(f"Published: {stream_id}")
```

#### Publish a pre-built Event

```python
from event_bus import Event, EventMetadata

event = Event(
    topic="inventory.reserved",
    data={"sku": "WIDGET-A", "qty": 3, "warehouse": "US-EAST"},
    metadata=EventMetadata(
        source="inventory-service",
        correlation_id="req-abc-123",
        causation_id="ord-12345",  # the event that caused this one
        headers={"priority": "high"},
    ),
)
stream_id = await bus.publish_event(event)
```

#### Batch publish (pipelined)

Batch publish sends all events in a single Redis pipeline for efficiency. Middleware hooks (`before_publish` / `after_publish`) are applied to every event in the batch:

```python
events = [
    Event(topic="notifications.email", data={"to": "alice@example.com", "template": "welcome"}),
    Event(topic="notifications.sms", data={"to": "+1234567890", "body": "Your order shipped"}),
    Event(topic="analytics.track", data={"event": "order_completed", "user_id": "cust-99"}),
]
stream_ids = await bus.publish_many(events)
```

### 2. Server-Side: Consumer Groups

Consumer groups provide **at-least-once delivery**, **parallel processing**, **automatic retries with real backoff delays**, and **dead-letter queues**. Use these for any work that must not be lost.

#### Decorator style

```python
from event_bus import EventBus, Event
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.consumer.retry import ExponentialBackoff

bus = EventBus(transport)


@bus.consumer_group(
    topics=["orders.created", "orders.updated"],
    group_name="order-processor",
    config=ConsumerGroupConfig(
        concurrency=3,  # 3 parallel worker tasks
        batch_size=20,  # read 20 messages per XREADGROUP
        max_retries=5,  # then send to DLQ
        retry_policy=ExponentialBackoff(
            base_delay=2.0,  # 2s, 4s, 8s, 16s, 32s
            max_delay=120.0,
        ),
    ),
)
async def process_order(event: Event) -> None:
    """
    This handler runs for every orders.created and orders.updated event.

    - If it succeeds: the message is ACKed automatically.
    - If it raises: the worker sleeps for the retry delay (exponential backoff
      with jitter), then the message stays pending for reclaim.
    - After 5 retries: the message goes to the dead-letter queue.
    """
    order_id = event.data["order_id"]
    print(f"Processing {event.topic}: order {order_id}")
    await save_to_database(order_id, event.data)
    await notify_warehouse(order_id)


# Start the bus -- connects to Redis, creates consumer groups, starts workers
async with bus:
    await asyncio.Event().wait()  # run forever
```

#### Programmatic style

```python
async def handle_payment(event: Event) -> None:
    print(f"Payment: {event.data}")
    await process_payment(event.data["payment_id"])


cg = bus.create_consumer_group(
    topics=["payments.completed", "payments.refunded"],
    group_name="payment-processor",
    handler=handle_payment,
    config=ConsumerGroupConfig(concurrency=2),
)

async with bus:
    await asyncio.Event().wait()
```

#### Multiple consumer groups in one process

```python
bus = EventBus(transport)


@bus.consumer_group(["orders.*"], "order-service")
async def handle_orders(event: Event) -> None: ...


@bus.consumer_group(["payments.*"], "payment-service")
async def handle_payments(event: Event) -> None: ...


@bus.consumer_group(["notifications.*"], "notification-service")
async def handle_notifications(event: Event) -> None: ...


# All three consumer groups start together
async with bus:
    await asyncio.Event().wait()
```

#### Consumer group restartability

Consumer groups can be stopped and restarted cleanly. The internal stop event is reset on each `stop()` call, so calling `start()` again works correctly:

```python
cg = bus.create_consumer_group(["orders.*"], "processor", handler=my_handler)
async with bus:
    await cg.start()
    # ... later ...
    await cg.stop()
    # ... can restart ...
    await cg.start()
```

### 3. Server-Side: Lightweight Subscribers

For fire-and-forget reactions that don't need consumer group guarantees (logging, metrics, cache invalidation):

```python
@bus.subscribe("orders.*", "payments.*")
async def log_all_events(event: Event) -> None:
    """Receives events via Pub/Sub -- no durability, no ACK."""
    print(f"[LOG] {event.topic}: {event.data}")
```

Each `CallbackSubscriber` has an internal bounded queue (default: 256 items, `max_queue_size`) and a drain loop. If the handler is slow and the queue fills up, events are **dropped** rather than blocking the shared pub/sub listener. This protects all other subscribers from being starved by one slow handler. Drops are counted in `subscriber.dropped_count` and logged as a warning for the first drop, then at most once every `drop_log_interval` seconds (default 10 s) together with the number dropped since the last report, so a flood of drops cannot also flood the log.

**Stop and restart.** `stop()` never blocks on the queue: it wakes the drain loop, lets the handler call in progress finish for up to `stop_timeout` seconds (a constructor argument, default 5 s) and only then cancels it. Events still queued at that point are discarded and counted in `dropped_count`. `stop()` returns in bounded time even when the queue is full or the handler swallows cancellation, can be called any number of times (also again after a caller's timeout cancelled it -- the handler is then cancelled too, not left running), and the subscriber can be started again: `@bus.subscribe` handlers keep receiving events across `bus.stop()` / `bus.start()`. Events pushed before the first `start()` are buffered; events pushed while stopped are ignored. The adapters' loops never swallow `CancelledError`.

### 4. Frontend: SSE Adapter

The `SSESubscriber` converts Redis Pub/Sub events into SSE-formatted text chunks. It is **framework-agnostic** -- you provide the HTTP response writer.

The subscriber must be registered with the bus via `bus.add_subscriber()` and removed with `bus.remove_subscriber()` when the client disconnects. Always call `stop()` in a `finally` block:

#### With FastAPI

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from event_bus import EventBus
from event_bus.adapters.sse import SSESubscriber


@app.get("/events/stream")
async def sse_endpoint(request: Request, topics: str = "**"):
    topic_list = [t.strip() for t in topics.split(",")]

    subscriber = SSESubscriber(
        patterns=topic_list,
        max_queue_size=256,
        heartbeat_interval=15.0,  # keep-alive every 15s
    )
    bus.add_subscriber(subscriber)
    await subscriber.start()

    async def generate():
        try:
            async for chunk in subscriber.events():
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            bus.remove_subscriber(subscriber)
            await subscriber.stop()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
```

The `events()` generator waits on the queue with `heartbeat_interval` as the timeout: it yields each event as soon as it arrives, a `: heartbeat` comment only after `heartbeat_interval` seconds without an event, and ends promptly -- no polling -- when `stop()` is called or the optional `cancel` event is set. `stop()` returns immediately; events still queued are discarded and counted in `dropped_count` (queue-full drops are counted and rate-limit logged as for `CallbackSubscriber`). After `stop()`, `start()` works again and a new `events()` call streams the new run.

Each event is written as `id:`, `event:` (the topic) and `data:` lines, where the data is compact JSON encoded by the same rules as the JSON serializer (see [Serialization](#serialization)); a multi-line payload gets one `data:` line per line. An event whose ID or topic contains CR or LF is **dropped with a warning** -- otherwise a topic such as `"orders.created\nevent: admin"` could forge SSE fields for every subscriber -- and an event whose data cannot be encoded (for example `NaN`) is skipped with an error log; the stream carries on in both cases. The encoded chunk is cached per event, so N connections encode an event once. `event_bus.adapters.sse.format_sse(data, event=..., event_id=...)` builds a message by the same rules.

Topic patterns usually come straight from the client, so `SSESubscriber` and `WebSocketSubscriber` validate them with `validate_pattern()` (see [Topic Patterns](#topic-patterns)) and accept at most `max_patterns` of them (default 32). A malformed or oversized list -- or a bare string instead of a list -- raises `InvalidTopicError`, a `ValueError`, from the constructor; answer it with `400 Bad Request`:

```python
from event_bus import InvalidTopicError

try:
    subscriber = SSESubscriber(patterns=topic_list)
except InvalidTopicError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
```

#### With aiohttp

```python
from aiohttp import web


async def sse_endpoint(request: web.Request) -> web.StreamResponse:
    topics = request.query.get("topics", "*").split(",")

    response = web.StreamResponse()
    response.content_type = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    await response.prepare(request)

    subscriber = SSESubscriber(patterns=topics)
    bus.add_subscriber(subscriber)
    await subscriber.start()

    try:
        async for chunk in subscriber.events():
            await response.write(chunk.encode("utf-8"))
    finally:
        bus.remove_subscriber(subscriber)
        await subscriber.stop()

    return response
```

### 5. Frontend: WebSocket Adapter

The `WebSocketSubscriber` forwards events to any object with a `send_text(str)` method -- compatible with Starlette, aiohttp, and the `websockets` library out of the box.

#### With FastAPI

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from event_bus.adapters.websocket import WebSocketSubscriber


@app.websocket("/events/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    init_msg = await websocket.receive_json()
    topics = init_msg.get("topics", ["*"])

    subscriber = WebSocketSubscriber(
        patterns=topics,
        ws=websocket,
        max_queue_size=512,
    )
    bus.add_subscriber(subscriber)
    await subscriber.start()

    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("action") == "unsubscribe":
                break
            elif msg.get("action") == "update_topics":
                # Dynamic re-subscription
                await subscriber.stop()
                bus.remove_subscriber(subscriber)
                subscriber = WebSocketSubscriber(
                    patterns=msg["topics"],
                    ws=websocket,
                )
                bus.add_subscriber(subscriber)
                await subscriber.start()
    except WebSocketDisconnect:
        pass
    finally:
        bus.remove_subscriber(subscriber)
        await subscriber.stop()
```

If a send fails or takes longer than `send_timeout` seconds (default 10 s) -- client gone, network error, a peer that stopped reading -- the subscriber treats the connection as dead: it stops sending events **and** pings (so the client cannot look connected while nothing arrives), ignores further events so its queue does not fill up, and sets `subscriber.closed`, an `asyncio.Event` the endpoint can await to tear the connection down. An event the formatter cannot encode is instead logged with its traceback and skipped; the stream carries on.

`stop()` sets `closed` too, lets a send in progress finish (up to `stop_timeout`, default 5 s) rather than cutting a frame in half -- which matters when a new subscriber reuses the socket, as in the `update_topics` branch above -- and discards the unsent backlog (counted in `dropped_count`). It never blocks on a full queue, can be called repeatedly, and `start()` clears `closed` and works again. You must still call `stop()` and `remove_subscriber()` in a `finally` block. Client-supplied patterns are validated as described for the SSE adapter, so catch `InvalidTopicError` around the constructor.

#### Custom WebSocket message format

```python
import json
from event_bus.core.event import EventEnvelope


def custom_format(envelope: EventEnvelope) -> str:
    """Only send what the dashboard needs."""
    return json.dumps(
        {
            "id": envelope.event.event_id,
            "type": envelope.event.topic.split(".")[-1],  # "created", "updated"
            "payload": envelope.event.data,
            "ts": envelope.event.metadata.timestamp,
        }
    )


subscriber = WebSocketSubscriber(
    patterns=["orders.*"],
    ws=websocket,
    format_message=custom_format,
)
```

The default format (`{"type":"event","topic":...,"data":...,"metadata":{...}}`, compact JSON by the serializer's rules) is encoded once per event and shared by all WebSocket subscribers. A custom `format_message` bypasses that cache and runs once per subscriber; if it raises, the event is logged and skipped.

### 6. Frontend: JavaScript Client Examples

#### SSE (EventSource)

```javascript
// Connect to SSE endpoint
const evtSource = new EventSource('/events/stream?topics=orders.*,payments.*');

// Listen for specific event types (topic names become SSE event types)
evtSource.addEventListener('orders.created', (e) => {
  const order = JSON.parse(e.data);
  console.log('New order:', order.order_id, 'Total:', order.total);
  addToOrdersDashboard(order);
});

evtSource.addEventListener('orders.updated', (e) => {
  const order = JSON.parse(e.data);
  updateOrderRow(order);
});

// SSE auto-reconnects on disconnect -- built into the browser
evtSource.onerror = (e) => {
  console.warn('SSE connection lost, will auto-reconnect...');
};
```

#### WebSocket

```javascript
const ws = new WebSocket('ws://localhost:8000/events/ws');

ws.onopen = () => {
  ws.send(JSON.stringify({
    action: 'subscribe',
    topics: ['orders.*', 'inventory.*']
  }));
};

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'ping') return;  // ignore keep-alive pings

  console.log(`[${msg.topic}]`, msg.data);
  switch (msg.topic) {
    case 'orders.created':
      addToOrdersList(msg.data);
      break;
    case 'inventory.low_stock':
      showStockAlert(msg.data);
      break;
  }
};

// Reconnect on close
ws.onclose = () => {
  setTimeout(() => { /* reconnect logic */ }, 3000);
};
```

### 7. Middleware

Middlewares transform or inspect events before and after publishing. They are applied to `publish()`, `publish_event()`, and `publish_many()`:

```python
from event_bus.core.event import Event


class AuditLogMiddleware:
    """Log every published event for audit compliance."""

    async def before_publish(self, event: Event) -> Event:
        print(f"[AUDIT] Publishing: {event.topic} id={event.event_id}")
        return event

    async def after_publish(self, event: Event, stream_id: str) -> None:
        print(f"[AUDIT] Published: {event.topic} -> {stream_id}")


class CorrelationIdMiddleware:
    """Inject a correlation ID if missing."""

    def __init__(self, default_source: str):
        self.default_source = default_source

    async def before_publish(self, event: Event) -> Event:
        if not event.metadata.source:
            from event_bus import EventMetadata

            new_meta = EventMetadata(
                event_id=event.metadata.event_id,
                timestamp=event.metadata.timestamp,
                source=self.default_source,
                correlation_id=event.metadata.correlation_id,
                causation_id=event.metadata.causation_id,
                headers=event.metadata.headers,
            )
            return Event(topic=event.topic, data=event.data, metadata=new_meta)
        return event

    async def after_publish(self, event: Event, stream_id: str) -> None:
        pass


# Register middleware
bus.use(AuditLogMiddleware())
bus.use(CorrelationIdMiddleware(default_source="my-service"))
```

### 8. Health Checks

```python
@app.get("/health")
async def health():
    status = await bus.health_check()
    # Returns:
    # {
    #   "healthy": True,
    #   "transport": True,
    #   "started": True,
    #   "subscribers": 3,
    #   "consumer_groups": 2,
    #   "pubsub_listener_running": True,
    #   "pubsub_listener_error": None
    # }
    http_status = 200 if status["healthy"] else 503
    return JSONResponse(status, status_code=http_status)
```

---

## Configuration Reference

```python
from event_bus import EventBus
from event_bus.config import EventBusConfig, RedisConfig
from event_bus.transport.redis.transport import RedisTransport

transport = RedisTransport(
    RedisConfig(
        url="redis://localhost:6379/0",  # Redis connection URL (redacted in logs and repr)
        max_connections=20,  # Command pool size
        max_blocking_connections=10,  # Blocking pool (XREADGROUP workers)
        max_pubsub_connections=10,  # Dedicated Pub/Sub subscription pool
        pool_timeout=10.0,  # Seconds to wait for a free pooled connection
        key_prefix="eventbus",  # Redis key namespace
        max_stream_length=100_000,  # MAXLEN ~ per stream; None = trim by consumption only
        dlq_max_length=50_000,  # MAXLEN ~ per dead-letter stream; None = unbounded
        health_check_interval=10.0,  # Seconds between PING checks
        socket_timeout=5.0,  # Socket read/write timeout (command pool)
        socket_connect_timeout=5.0,  # Socket connect timeout
        blocking_socket_timeout=30.0,  # Read timeout for XREADGROUP connections
        pubsub_socket_timeout=30.0,  # Read timeout for the Pub/Sub connection
        serializer="json",  # "json" or "msgpack"
        circuit_breaker_failure_threshold=5,  # Connectivity failures before circuit opens
        circuit_breaker_recovery_timeout=30.0,  # Seconds before half-open
        circuit_breaker_half_open_max_calls=3,  # Concurrent probe calls in half-open
    )
)

bus = EventBus(
    transport,
    EventBusConfig(
        max_pending_publishes=1000,  # Backpressure: max in-flight publishes
        shutdown_timeout=30.0,  # Seconds to wait for graceful shutdown
        subscribe_timeout=5.0,  # start() waits this long for the Pub/Sub subscription
        enable_idempotency=False,  # Skip events a group already handled (by event_id)
        idempotency_ttl=86_400,  # Seconds a handled event_id is remembered
        idempotency_lease=300,  # Seconds an in-progress claim holds before it expires
        metrics_topic_depth=None,  # Tag metrics with the first N topic segments
    ),
)
```

The transport owns the Redis settings and the serializer. `EventBusConfig.redis`
and `EventBusConfig.serializer` are deprecated: setting them to anything but the
default emits a `DeprecationWarning`, and they are not used.

`metrics_topic_depth` bounds metric cardinality when topics carry IDs
(`orders.42.shipped`): with a depth of 1 every metric is tagged `orders`.

```python
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.consumer.retry import ExponentialBackoff

cg_config = ConsumerGroupConfig(
    consumer_name="worker-1",  # Unique consumer name (auto-generated if omitted)
    concurrency=3,  # Parallel worker tasks
    batch_size=10,  # Messages per XREADGROUP call
    block_ms=2000,  # XREADGROUP block timeout (ms)
    max_retries=5,  # Retries before DLQ
    retry_policy=ExponentialBackoff(
        base_delay=1.0,  # Initial delay (seconds)
        max_delay=60.0,  # Maximum delay cap
        multiplier=2.0,  # Delay multiplier per attempt
        jitter=0.1,  # Random jitter factor (prevents thundering herd)
    ),
    claim_idle_ms=60_000,  # Lease: a message idle this long is reclaimed
    claim_interval=1.0,  # Seconds between reclaim sweeps (also when retries run)
    heartbeat_interval=None,  # Lease renewal period; None = claim_idle_ms / 3
    start_id="0",  # Where a new group starts: "0" = whole stream, "$" = new events only
    trim_interval=60.0,  # Seconds between trimming what every group consumed
    idle_consumer_ms=3_600_000,  # Remove consumers idle this long with nothing pending
)
```

`claim_idle_ms` must be positive and `claim_interval` must be shorter than it;
`ConsumerGroupConfig` raises `ValueError` otherwise.

---

## Topic Patterns

Topics are dot-separated hierarchical names. Subscribers can use glob patterns:

| Pattern | Matches | Does Not Match |
|---|---|---|
| `orders.created` | `orders.created` | `orders.updated` |
| `orders.*` | `orders.created`, `orders.updated` | `orders.item.added` |
| `orders.**` | `orders.created`, `orders.item.added`, `orders.a.b.c` | `payments.created` |
| `**` | `orders.created`, `payments.refunded`, `a.b.c.d` | *(matches everything)* |
| `app.*.completed` | `app.orders.completed`, `app.payments.completed` | `app.orders.items.completed` |

The `**` wildcard matches **one or more** segments at any depth. A standalone `**` pattern matches any topic.

Matching is segment-wise: the topic is split on `.`, a literal segment matches the identical segment, `*` exactly one non-empty segment and `**` one or more. It costs O(pattern segments × topic segments) with no regular expressions, so no topic can make a match slow and block the event loop, and each `TopicPattern` precomputes its segments once (there is no global cache to grow). A topic that is empty or contains whitespace or control characters (CR, LF, tab, NUL, ...) never matches any pattern, including a trailing newline.

**Topic and pattern rules.** `validate_topic(topic)` accepts one or more dot-separated segments of ASCII letters, digits, `_`, `-` and `:`, at most 255 characters long -- no whitespace, no CR/LF, no empty segments, no wildcards. `validate_pattern(pattern)` applies the same rules but also allows whole-segment `*` and `**` (not partial wildcards such as `orders*`) and at most 32 segments. Both return their argument unchanged and otherwise raise `InvalidTopicError`, which is both an `EventBusError` and a `ValueError`:

```python
from event_bus import InvalidTopicError, validate_pattern, validate_topic

validate_topic("tenant-1:orders.created")  # ok
validate_pattern("orders.**")  # ok
validate_topic("orders created")  # InvalidTopicError
validate_pattern("orders*")  # InvalidTopicError
```

`TopicPattern` itself does not validate, so existing server-side patterns keep working; the SSE and WebSocket adapters validate the (client-supplied) patterns they are given and cap them at `max_patterns` per subscriber.

---

## Serialization

`JsonSerializer` (the default) and `MsgpackSerializer` encode payloads by the same explicit rules (`event_bus.core.serialization.encode_default`) instead of silently `str()`-ing unknown values:

| Python value | Encoded as |
|---|---|
| `datetime`, `date`, `time` | ISO 8601 string (`isoformat()`), e.g. `"2024-01-02T03:04:05+00:00"` |
| `UUID`, `Decimal` | string |
| `Enum` | its `.value` |
| `set`, `frozenset` | list, sorted when the items are comparable |
| `tuple` | list |
| anything else (custom objects, `bytes` in JSON, ...) | rejected with `SerializationError` |

Decoding returns the encoded form (strings and lists), not the original Python types.

- JSON is compact (`separators=(",", ":")`), and `NaN` / `Infinity` are rejected with `SerializationError` -- they are not valid JSON and browsers cannot parse them.
- Payloads are dicts: `serialize()` rejects anything else, and `deserialize()` raises `SerializationError` unless the decoded value is a dict.
- `MsgpackSerializer` imports msgpack once, when it is constructed (raising a clear `ImportError` if the `msgpack` extra is missing), and unpacks with `strict_map_key=False`, so maps with non-string keys such as `{1: "a"}` round-trip.
- The SSE and WebSocket adapters encode event data with the same rules (`event_bus.core.serialization.json_dumps`), so browsers receive exactly what the serializer would produce.

---

## Resilience Features

### Circuit Breaker

Protects against Redis outages. The circuit breaker has three states:

- **CLOSED** (normal): All calls go through. Consecutive failures are counted.
- **OPEN** (tripped): After reaching the failure threshold, all calls fail immediately with `CircuitOpenError`. No Redis traffic.
- **HALF_OPEN** (probing): After the recovery timeout elapses, at most `circuit_breaker_half_open_max_calls` probe calls run at once; the rest fail fast with `CircuitOpenError`. A successful probe closes the circuit; a failed one re-opens it.

Only **connectivity** failures count: refused or dropped connections, timeouts
and OS-level socket errors. A command Redis rejects (`WRONGTYPE`, `NOGROUP`, a
bad payload) says nothing about Redis being down and doesn't count, and neither
does waiting for a pooled connection (`pool_timeout`) -- a publish burst queues
for connections rather than tripping the breaker. Health-check pings don't touch
the breaker either; they log and set `is_healthy()`.

The state transition from OPEN to HALF_OPEN happens atomically when the recovery timeout elapses -- there is no race between checking state and acting on it.

![Circuit Breaker Flow](docs/diagrams/circuit-breaker-flow.mmd)

### Backpressure

Limits concurrent in-flight publishes via an asyncio semaphore (default: 1000). If the semaphore is exhausted, `publish()` raises `BackpressureError` after a configurable timeout (default: 10s) instead of queuing unboundedly.

```python
from event_bus import BackpressureError

try:
    await bus.publish("orders.created", data)
except BackpressureError:
    # System is overloaded, apply flow control
    return {"status": "overloaded"}, 503
```

### Dead Letter Queue

Each consumer group has its own dead-letter stream at `{key_prefix}:dlq:{group}`.
An event that fails `max_retries` times is written there (with the error as
`"ExceptionType: message"`, the original stream and ID) and acknowledged in the
same `MULTI` transaction, so it is never both pending and dead-lettered. Other
groups on the same topic are unaffected.

A stream entry that can't be decoded at all (a corrupt payload, an unknown
serializer) is dead-lettered as it came in rather than blocking its batch: its
raw fields are kept under `data["raw"]` and the `x-eventbus-undecodable` header
is set.

```python
# Inspect a group's dead letters
for envelope in await transport.read_dlq(count=50, group_name="billing"):
    print(envelope.stream_id, envelope.event.topic, envelope.event.metadata.headers)

# Once the bug is fixed, run them through that group's handler again
replayed, failed = await bus.replay_dead_letters("billing", count=100)
```

`replay_dead_letters` hands each dead letter to the named group's handler
directly; nothing is republished, so groups that already processed the event
don't see it again. Each one that succeeds is deleted from the DLQ; each one
that fails (and every undecodable entry) stays for inspection. `read_dlq()`
without a group name reads the shared stream `{key_prefix}:dlq`, which only
`send_to_dlq(envelope, error)` without a group writes to.

### Retry with Backoff

When a handler raises, the worker **doesn't sleep**: it releases the message
and moves on to the next one, so one failing event never holds up the rest of
the stream. The retry delay follows the configured policy:

- **ExponentialBackoff** (default): `base_delay * multiplier^attempt` with random jitter. Delays: 1s, 2s, 4s, 8s, 16s... capped at `max_delay`. Jitter prevents thundering herd when multiple consumers retry simultaneously.
- **FixedDelay**: Constant delay between retries.

The message stays in the stream's pending list. Releasing it sets its idle time
to `claim_idle_ms - delay`, so the reclaimer (every `claim_interval` seconds,
`XPENDING` + one batched `XCLAIM`) picks it up once the delay has passed -- on
this replica or any other. The effective delay is capped at `claim_idle_ms`,
and it is only as precise as `claim_interval`. Retry state is derived from the
stream's delivery count, so a poison message reaches the DLQ after
`max_retries` retries even across restarts. Batch handlers receive their
retried events as a list, the same as the first delivery.

**Leases.** A message a consumer has read is its lease for `claim_idle_ms`.
While a batch is being handled, a heartbeat renews the lease of every message
still in hand (every `heartbeat_interval`, default a third of the lease), so a
slow batch isn't reclaimed and handled twice by another replica. Set
`claim_idle_ms` comfortably above your slowest single event.

**Acks.** A failed `XACK` is retried three times and then counted as
`events.ack_failed`; it is not a handler failure, so the event is never retried
or dead-lettered because of it. If the ack is lost for good the event is
redelivered after the lease -- use idempotency to make that harmless.

### Idempotency

Delivery is at-least-once. With `EventBusConfig(enable_idempotency=True)` every
consumer group records, per `event_id`, that it has handled an event and skips
it when it comes again (a publisher retry, a redelivery after a lost ack):

1. Before the handler runs, the group claims the ID with `SET NX` and a lease
   (`idempotency_lease`). A second copy arriving meanwhile is released for a
   later retry, not dropped.
2. After the handler succeeds, the claim becomes `done` for `idempotency_ttl`
   seconds; copies arriving in that window are acknowledged without running.
3. If the handler fails, the claim is released (a compare-and-delete, so an
   expired lease re-taken by another consumer is never deleted), and the retry
   runs normally.

Keys are `{key_prefix}:idem:{group}:{event_id}`: each group dedupes on its own.
The `InMemoryTransport` uses an in-process store with the same behaviour.

### Stream Trimming

`XADD` trims each stream approximately to `max_stream_length`. That cap applies
whether or not groups have caught up, so the reclaimer also trims each stream
every `trim_interval` seconds with `XTRIM MINID` to the oldest entry any group
still needs (undelivered or pending). If a group's lag passes 80% of
`max_stream_length` a warning is logged: past the cap the oldest unread
entries are trimmed before that group reads them. Set `max_stream_length=None`
to trim by consumption only.

Consumers idle longer than `idle_consumer_ms` with nothing pending are removed
from the group, so replicas that come and go don't accumulate.

### Slow Consumer Protection

Every adapter (CallbackSubscriber, SSESubscriber, WebSocketSubscriber) has a **bounded internal queue** with configurable size:

| Adapter | Default Queue Size | On Full |
|---|---|---|
| CallbackSubscriber | 256 | Drops event, counts it, rate-limited warning |
| SSESubscriber | 256 | Drops event, counts it, rate-limited warning |
| WebSocketSubscriber | 512 | Drops event, counts it, rate-limited warning |

A slow consumer **never blocks the shared pub/sub listener**. Events are delivered to the queue with `put_nowait()`. If the queue is full, the event is dropped and counted in the subscriber's `dropped_count`; a warning is logged for the first drop and then at most once per `drop_log_interval` seconds (default 10 s) with the number dropped since the last report, so a flood of drops does not slow the event loop with logging. Other subscribers are unaffected.

---

## Connection Lifecycle & Cleanup

The event bus is designed to prevent connection leaks, dangling tasks, and memory growth under all conditions -- normal shutdown, errors, and partial failures.

### Graceful Shutdown

`bus.stop()` follows a strict teardown order:

1. **Cancel the shared pub/sub listener** -- stops accepting new events
2. **Cancel all tracked background tasks** -- any fire-and-forget tasks created by `add_subscriber()` or `create_consumer_group()` after startup
3. **Stop all consumer groups** -- workers stop reading, finish the event in hand and hand the rest of their batch back (idle time set to the lease, so another replica claims them at once); anything still running after the shutdown timeout is cancelled
4. **Stop all subscribers** -- each adapter wakes its consumer without blocking (even on a full queue), lets in-flight work finish for up to its `stop_timeout`, cancels what is left and discards its unsent backlog; SSE `events()` generators end and WebSocket subscribers set `closed`. Subscribers can be started again by the next `bus.start()`
5. **Disconnect the transport** -- closes Redis clients and connection pools, nulls all references

If shutdown tasks exceed the configured timeout, the transport is **still disconnected**. The `TimeoutError` is caught and logged as a warning rather than leaking the connection.

### Startup Failure Safety

If any step during `bus.start()` fails (subscriber start, consumer group creation, etc.), the bus **automatically tears down** everything that was already started:

```python
async with bus:  # if start() fails partway through,
    ...  # stop() is called to clean up
```

The transport connection, shared listener task, and any already-started subscribers/consumer groups are all properly cleaned up before the exception propagates.

### Background Task Tracking

When subscribers or consumer groups are added dynamically (after `bus.start()`), the fire-and-forget `asyncio.create_task()` calls are tracked in a set with done-callbacks:

- **Exception logging**: If the task fails, the exception is logged via a done-callback instead of being silently lost ("Task exception was never retrieved")
- **Cleanup on stop**: All tracked tasks are cancelled and awaited during `bus.stop()`
- **Self-cleaning**: Completed tasks are automatically removed from the tracking set

### Resource Cleanup on Disconnect

When `disconnect()` is called (or triggered by shutdown):

- **RedisConnectionManager**: Cancels the health check loop, closes the Redis clients (`aclose()`), and sets all references (clients, pools) to `None`. Any post-disconnect access raises `EventBusConnectionError` instead of a confusing redis error.
- **RedisTransport**: Nulls out its stream, pub/sub and DLQ managers; a late task that tries to use them gets `EventBusConnectionError`.
- **InMemoryTransport**: Sends shutdown sentinels to all pub/sub queues (non-blocking, with drain fallback if full). Streams, groups and the DLQ survive a disconnect, like Redis data does; call `reset()` to clear them between tests.
- **ConsumerGroup**: Resets the internal `_stop_event` so the group can be restarted via `start()` after being stopped.

---

## Debug Logging

Every module uses Python's standard `logging` at the `DEBUG` level. To see everything the event bus does:

```python
import logging

# Enable debug for all event bus modules
logging.getLogger("event_bus").setLevel(logging.DEBUG)

# Or use basicConfig for quick debugging
logging.basicConfig(level=logging.DEBUG)
```

With debug logging enabled, you will see:

| Area | What is logged |
|---|---|
| **Publishing** | Event ID, topic, middleware execution, backpressure semaphore acquire/release, stream write result, pub/sub fan-out count |
| **Consumer Groups** | Worker start/stop, XREADGROUP calls with parameters, message processing, ACK, retry delays, DLQ sends, reclaimer sweeps with claimed message IDs |
| **Transport** | Redis connect/disconnect, dual-write publish, XADD/XREADGROUP/XACK/XCLAIM/XPENDING commands with full parameters |
| **Connection** | Pool creation with sizes, ping results, health check loop, client close |
| **Pub/Sub** | PUBLISH with channel and subscriber count, PSUBSCRIBE patterns, received messages, control messages |
| **Adapters** | Queue enqueue with size, handler invocation, drain loop lifecycle, heartbeats, WS sends, queue-full drops |
| **Circuit Breaker** | State transitions (closed/open/half_open), probe counts, failure counts |
| **Backpressure** | Semaphore acquire/release with available counts |
| **DLQ** | Send/read/replay/delete with event IDs, groups and counts |

Example output:

```
DEBUG event_bus.bus: Publishing event 3f2a... to topic 'orders.created'
DEBUG event_bus.resilience.backpressure: Backpressure: acquiring semaphore (available=999/1000)
DEBUG event_bus.transport.redis.transport: Publishing event 3f2a... to topic 'orders.created' (dual-write)
DEBUG event_bus.transport.redis.stream: XADD eventbus:stream:orders.created (event_id=3f2a..., maxlen~100000)
DEBUG event_bus.transport.redis.stream: XADD result: eventbus:stream:orders.created -> 1708617600000-0
DEBUG event_bus.transport.redis.pubsub: PUBLISH eventbus:channel:orders.created (event_id=3f2a...)
DEBUG event_bus.transport.redis.pubsub: PUBLISH eventbus:channel:orders.created -> 2 subscribers received
DEBUG event_bus.resilience.backpressure: Backpressure: semaphore released (available=1000/1000)
DEBUG event_bus.bus: Event 3f2a... published to stream (stream_id=1708617600000-0)
```

---

## Testing

The library includes an `InMemoryTransport` for unit testing with zero Redis dependency:

```python
import pytest
from event_bus import EventBus, Event
from event_bus.config import EventBusConfig
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.testing.fake_transport import InMemoryTransport


@pytest.mark.asyncio
async def test_order_processing():
    transport = InMemoryTransport()
    bus = EventBus(transport, EventBusConfig())
    processed: list[Event] = []

    @bus.consumer_group(
        ["orders.created"],
        "test-group",
        config=ConsumerGroupConfig(concurrency=1, block_ms=100),
    )
    async def handler(event: Event):
        processed.append(event)

    async with bus:
        await bus.publish("orders.created", {"order_id": "test-1"})
        await asyncio.sleep(0.3)

    assert len(processed) == 1
    assert processed[0].data["order_id"] == "test-1"

    # Inspect what was published
    assert len(transport.published_events) == 1
    assert transport.published_events[0].topic == "orders.created"
```

The `InMemoryTransport` behaves like Redis where it matters to delivery: events
round-trip through the serializer, reading a group that doesn't exist raises
`NoGroupError`, and streams, groups and dead letters survive `disconnect()`.
Use a fresh transport per test, or call `transport.reset()` to clear it.

Run the test suite, which needs no Redis (the msgpack serializer's tests use
msgpack from the dev group):

```bash
# From the repository root: lint, format check, types and tests
make event-bus-check

# Or in packages/python/event-bus
uv run pytest
```

`tests/integration/` holds delivery tests against a real Redis (bursts, poison
entries, leases across replicas, DLQ replay, trimming, idempotency). They are
skipped unless `EVENT_BUS_TEST_REDIS_URL` is set; from the repository root,

```bash
make event-bus-test-redis
```

starts the shared Redis and runs them on database 14. Each test works under a
key prefix of its own and deletes it afterwards.

---

## Full End-to-End Example

A complete multi-service application with real-time dashboard:

```python
"""
app.py -- Order processing system with real-time dashboard

Run (FastAPI and Uvicorn are not dependencies of the package):
    make shared-deps
    uv run --with fastapi --with uvicorn uvicorn app:app --reload
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from event_bus import EventBus, Event
from event_bus.transport.redis.transport import RedisTransport
from event_bus.config import RedisConfig, EventBusConfig
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.adapters.sse import SSESubscriber
from event_bus.adapters.websocket import WebSocketSubscriber

# --- Setup ---
transport = RedisTransport(RedisConfig(url="redis://127.0.0.1:16389/0"))
bus = EventBus(transport, EventBusConfig())


# --- Consumer Groups (server-side processing) ---


@bus.consumer_group(["orders.created"], "order-processor")
async def process_order(event: Event) -> None:
    """Validate order, reserve inventory, initiate payment."""
    order = event.data
    print(f"Processing order {order['order_id']}...")
    await asyncio.sleep(0.1)

    # Publish downstream events (event-driven choreography)
    await bus.publish(
        "inventory.reserve",
        {"order_id": order["order_id"], "items": order["items"]},
        source="order-processor",
        causation_id=event.event_id,
        correlation_id=event.metadata.correlation_id,
    )
    await bus.publish(
        "orders.confirmed",
        {"order_id": order["order_id"], "status": "confirmed"},
        source="order-processor",
        causation_id=event.event_id,
        correlation_id=event.metadata.correlation_id,
    )


@bus.consumer_group(["inventory.reserve"], "inventory-service")
async def reserve_inventory(event: Event) -> None:
    """Reserve inventory for an order."""
    print(f"Reserving inventory for order {event.data['order_id']}")
    await asyncio.sleep(0.05)

    await bus.publish(
        "inventory.reserved",
        {"order_id": event.data["order_id"], "items": event.data["items"]},
        source="inventory-service",
        causation_id=event.event_id,
    )


# --- App Lifecycle ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bus.start()
    yield
    await bus.stop()


app = FastAPI(lifespan=lifespan)


# --- REST API (publishers) ---


@app.post("/orders")
async def create_order(order: dict):
    """Create an order -- publishes event for async processing."""
    stream_id = await bus.publish(
        "orders.created",
        order,
        source="api-gateway",
    )
    return {"status": "accepted", "stream_id": stream_id}


# --- SSE Endpoint (real-time dashboard) ---


@app.get("/events/stream")
async def sse_stream(request: Request, topics: str = "orders.**"):
    topic_list = [t.strip() for t in topics.split(",")]
    subscriber = SSESubscriber(patterns=topic_list)
    bus.add_subscriber(subscriber)
    await subscriber.start()

    async def generate():
        try:
            async for chunk in subscriber.events():
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            bus.remove_subscriber(subscriber)
            await subscriber.stop()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- WebSocket Endpoint (live feed) ---


@app.websocket("/events/ws")
async def ws_feed(websocket: WebSocket):
    await websocket.accept()
    init = await websocket.receive_json()
    topics = init.get("topics", ["*"])

    subscriber = WebSocketSubscriber(patterns=topics, ws=websocket)
    bus.add_subscriber(subscriber)
    await subscriber.start()

    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("action") == "update_topics":
                await subscriber.stop()
                bus.remove_subscriber(subscriber)
                subscriber = WebSocketSubscriber(patterns=msg["topics"], ws=websocket)
                bus.add_subscriber(subscriber)
                await subscriber.start()
    except WebSocketDisconnect:
        pass
    finally:
        bus.remove_subscriber(subscriber)
        await subscriber.stop()


# --- Health ---


@app.get("/health")
async def health():
    return await bus.health_check()
```

Test it:

```bash
# Start the shared Redis (from the repository root)
make shared-deps

# Start the server
uv run --with fastapi --with uvicorn uvicorn app:app --reload

# Create an order
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id": "ord-001", "items": [{"sku": "WIDGET", "qty": 2}]}'

# Watch events via SSE (in another terminal)
curl -N http://localhost:8000/events/stream?topics=orders.**,inventory.**

# Or open the WebSocket in browser devtools:
# ws = new WebSocket('ws://localhost:8000/events/ws')
# ws.onopen = () => ws.send(JSON.stringify({topics: ['orders.**']}))
# ws.onmessage = (e) => console.log(JSON.parse(e.data))
```

### Bundled examples

`examples/server.py` is a runnable version of this application: a FastAPI server
with consumer groups, SSE and WebSocket endpoints, and the demo page in
`examples/static/index.html` on http://localhost:8000. FastAPI and Uvicorn are
not dependencies of the package, not even of its dev group, so add them for the
run, in `packages/python/event-bus` with the shared Redis started:

```bash
uv run --with fastapi --with uvicorn uvicorn examples.server:app --reload
```

It connects to `FORGE_REDIS_URL`, by default the shared Redis on
`redis://127.0.0.1:16389/0`. `examples/react-dashboard` is a Vite and React
dashboard on http://localhost:3000 that proxies to that server and subscribes
with the React client in `clients/react`: build the client first
(`npm install && npm run build` in `clients/react`), then run
`npm install && npm run dev` in `examples/react-dashboard`.

---

## Package Structure

The code lives under `src/event_bus/`; the tests under `tests/`, the examples
under `examples/`, the React client under `clients/react/` and the design notes
and diagrams under `docs/`.

```
src/event_bus/
├── __init__.py              # Public API exports
├── bus.py                   # EventBus orchestrator (lifecycle, task tracking)
├── config.py                # RedisConfig, EventBusConfig
├── core/
│   ├── event.py             # Event, EventMetadata, EventEnvelope
│   ├── topic.py             # TopicPattern (glob matching: *, **)
│   ├── serialization.py     # JSON + msgpack serializers
│   └── errors.py            # Exception hierarchy
├── interfaces/
│   ├── publisher.py         # Publisher protocol
│   ├── subscriber.py        # Subscriber protocol + BaseSubscriber
│   ├── transport.py         # Transport protocol
│   └── middleware.py        # Middleware protocol
├── transport/redis/
│   ├── connection.py        # Dual connection pool + health checks + cleanup
│   ├── stream.py            # Redis Streams operations (XADD, XREADGROUP, etc.)
│   ├── pubsub.py            # Redis Pub/Sub operations
│   ├── dlq.py               # Per-group dead-letter streams
│   └── transport.py         # RedisTransport (dual-write orchestrator)
├── consumer/
│   ├── group.py             # ConsumerGroup (workers, reclaimer, retry delays)
│   └── retry.py             # ExponentialBackoff, FixedDelay
├── adapters/
│   ├── base.py              # CallbackSubscriber (bounded queue + drain loop)
│   ├── sse.py               # SSE adapter (heartbeats, sentinel-based shutdown)
│   └── websocket.py         # WebSocket adapter (send loop, ping loop)
├── resilience/
│   ├── circuit_breaker.py   # 3-state circuit breaker (no side-effect reads)
│   ├── backpressure.py      # Semaphore-gated publish
│   └── idempotency.py       # Two-phase per-group dedup (Redis + in-memory)
├── observability/
│   └── metrics.py           # MetricsCollector protocol + NoOpMetrics
└── testing/
    ├── fake_transport.py    # InMemoryTransport (Redis semantics, reset())
    └── fixtures.py          # pytest fixtures
```
