# Subscription Patterns & Methods

A complete guide to every way you can subscribe to events, which topic patterns are supported, and when to use each approach.

---

## Table of Contents

- [Topic Naming](#topic-naming)
- [Pattern Matching](#pattern-matching)
  - [Exact Match](#exact-match)
  - [Single-Level Wildcard (*)](#single-level-wildcard-)
  - [Multi-Level Wildcard (**)](#multi-level-wildcard-)
  - [Combined Patterns](#combined-patterns)
  - [Full Pattern Reference](#full-pattern-reference)
- [Subscription Methods](#subscription-methods)
  - [1. @bus.subscribe() — Pub/Sub Callback](#1-bussubscribe--pubsub-callback)
  - [2. @bus.consumer_group() — Durable Stream Processing](#2-busconsumer_group--durable-stream-processing)
  - [3. bus.create_consumer_group() — Programmatic Registration](#3-buscreate_consumer_group--programmatic-registration)
  - [4. SSESubscriber — Server-Sent Events to Browser](#4-ssesubscriber--server-sent-events-to-browser)
  - [5. WebSocketSubscriber — WebSocket Push to Browser](#5-websocketsubscriber--websocket-push-to-browser)
  - [6. bus.add_subscriber() — Custom Subscribers](#6-busadd_subscriber--custom-subscribers)
- [Batch vs Single Handlers](#batch-vs-single-handlers)
- [Scoped Topics](#scoped-topics)
  - [User-Scoped Events](#user-scoped-events)
  - [Transaction-Scoped Events](#transaction-scoped-events)
  - [Tenant-Scoped Events (Multi-Tenancy)](#tenant-scoped-events-multi-tenancy)
  - [Region or Environment Scoping](#region-or-environment-scoping)
- [Dynamic Subscriptions](#dynamic-subscriptions)
- [Multiple Patterns Per Subscriber](#multiple-patterns-per-subscriber)
- [Pattern Matching Limitations](#pattern-matching-limitations)
- [Choosing the Right Subscription Method](#choosing-the-right-subscription-method)

---

## Topic Naming

Topics are **dot-separated hierarchical strings**. They follow a `domain.action` or `domain.entity.action` convention:

```
orders.created
orders.updated
orders.item.added
payments.refund.completed
notifications.email.sent
inventory.warehouse.us-east.low-stock
```

There is no enforced schema — topics are arbitrary strings. The dots create a hierarchy that the pattern matching system uses for filtering.

**Naming best practices:**
- Use lowercase with dots as separators
- Start with the domain/service name (`orders`, `payments`, `inventory`)
- End with the action or state (`created`, `updated`, `completed`, `failed`)
- Keep segments short and descriptive
- Use consistent naming across your system

---

## Pattern Matching

The event bus uses glob-style patterns compiled to regex. Patterns are checked when events arrive at a subscriber — only matching events are delivered.

### Exact Match

A pattern with no wildcards matches only that exact topic.

```python
"orders.created"
```

| Topic | Match? |
|---|---|
| `orders.created` | Yes |
| `orders.updated` | No |
| `orders.created.v2` | No |

### Single-Level Wildcard (`*`)

A `*` matches **exactly one** segment (anything between dots).

```python
"orders.*"
```

| Topic | Match? |
|---|---|
| `orders.created` | Yes |
| `orders.updated` | Yes |
| `orders.cancelled` | Yes |
| `orders.item.added` | No (two segments after `orders`) |
| `payments.created` | No (different first segment) |

`*` can appear in any position:

```python
"*.created"  # anything.created

"orders.*.done"  # orders.{anything}.done
```

| Pattern | Matches | Does Not Match |
|---|---|---|
| `*.created` | `orders.created`, `payments.created` | `orders.item.created` |
| `orders.*.done` | `orders.payment.done`, `orders.shipping.done` | `orders.done`, `orders.a.b.done` |

### Multi-Level Wildcard (`**`)

A `**` matches **one or more** segments at any depth.

```python
"orders.**"
```

| Topic | Match? |
|---|---|
| `orders.created` | Yes (one segment) |
| `orders.item.added` | Yes (two segments) |
| `orders.item.price.updated` | Yes (three segments) |
| `payments.created` | No (different root) |

A standalone `**` matches **any topic**:

```python
"**"
```

| Topic | Match? |
|---|---|
| `orders.created` | Yes |
| `payments.refund.completed` | Yes |
| `a.b.c.d.e` | Yes |

### Combined Patterns

Wildcards can be mixed with literal segments:

```python
"app.*.completed"  # app.{one-segment}.completed

"app.**.completed"  # app.{one-or-more-segments}.completed
```

| Pattern | Matches | Does Not Match |
|---|---|---|
| `app.*.completed` | `app.orders.completed`, `app.payments.completed` | `app.orders.items.completed` |
| `app.**.completed` | `app.orders.completed`, `app.orders.items.completed` | `app.completed` (** needs at least one segment) |

### Full Pattern Reference

| Pattern | Regex | Matches | Does Not Match |
|---|---|---|---|
| `orders.created` | `^orders\.created$` | `orders.created` | everything else |
| `orders.*` | `^orders\.[^.]+$` | `orders.created`, `orders.X` | `orders.a.b` |
| `orders.**` | `^orders\.[^.]+(?:\.[^.]+)*$` | `orders.created`, `orders.a.b.c` | `payments.created` |
| `*.*` | `^[^.]+\.[^.]+$` | `orders.created`, `a.b` | `a.b.c` |
| `**` | `^[^.]+(?:\.[^.]+)*$` | anything with at least one segment | *(matches everything)* |
| `*.*.done` | `^[^.]+\.[^.]+\.done$` | `orders.payment.done` | `orders.done` |

---

## Subscription Methods

### 1. `@bus.subscribe()` — Pub/Sub Callback

**Transport**: Redis Pub/Sub (fire-and-forget)
**Delivery**: Broadcast to ALL instances
**Pattern support**: Full wildcard support (`*`, `**`)

```python
@bus.subscribe("orders.*")
async def on_order(event: Event) -> None:
    print(f"{event.topic}: {event.data}")
```

Multiple patterns:

```python
@bus.subscribe("orders.*", "payments.*", "inventory.**")
async def on_event(event: Event) -> None:
    print(f"{event.topic}: {event.data}")
```

Direct registration (non-decorator):

```python
async def handler(event: Event) -> None:
    print(event.data)


bus.subscribe("orders.*", handler=handler)
```

**Behavior:**
- Receives events via Redis Pub/Sub — sub-millisecond latency
- Every instance running this code receives every matching event (broadcast)
- Internal bounded queue (default 256) with drop-on-full protection
- Background drain loop calls your handler
- No ACK, no retry, no durability

**Use for:** Logging, metrics, cache invalidation, triggering non-critical side effects.

**Do NOT use for:** Anything that must run exactly once (emails, payments, DB writes). Use a consumer group instead.

---

### 2. `@bus.consumer_group()` — Durable Stream Processing

**Transport**: Redis Streams (durable, at-least-once)
**Delivery**: One message to one consumer within the group
**Pattern support**: Exact topics only (no wildcards)

```python
@bus.consumer_group(
    topics=["orders.created", "orders.updated"],
    group_name="order-processor",
)
async def process_order(event: Event) -> None:
    await save_to_database(event.data)
```

With config:

```python
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.consumer.retry import ExponentialBackoff


@bus.consumer_group(
    topics=["orders.created"],
    group_name="order-processor",
    config=ConsumerGroupConfig(
        concurrency=3,  # 3 parallel workers
        batch_size=20,  # 20 messages per XREADGROUP
        max_retries=5,  # then dead-letter
        retry_policy=ExponentialBackoff(base_delay=2.0, max_delay=120.0),
    ),
)
async def process(event: Event) -> None: ...
```

**Behavior:**
- Reads from Redis Streams via `XREADGROUP`
- Each message delivered to exactly one consumer in the group
- Automatic ACK on success
- Exponential backoff retry on failure
- Dead letter queue after max retries
- Reclaimer task picks up messages from crashed consumers
- Across 25 pods/workers, each message is processed exactly once

**Use for:** Order processing, payments, email sending, DB writes — anything that must not be lost.

**Topics are exact strings, not patterns.** `topics=["orders.*"]` creates a literal stream called `eventbus:stream:orders.*` — it does not match `orders.created`. List each topic explicitly:

```python
# Correct
@bus.consumer_group(topics=["orders.created", "orders.updated"], ...)

# Wrong — creates a literal stream named "orders.*"
@bus.consumer_group(topics=["orders.*"], ...)
```

---

### 3. `bus.create_consumer_group()` — Programmatic Registration

Same as `@bus.consumer_group()` but without the decorator. Required when creating the bus inside a lifespan (post-fork safe for multi-worker deployments):

```python
async def process_order(event: Event) -> None:
    await save_to_database(event.data)


cg = bus.create_consumer_group(
    topics=["orders.created"],
    group_name="order-processor",
    handler=process_order,
    config=ConsumerGroupConfig(concurrency=2),
)
```

Returns the `ConsumerGroup` instance for manual control:

```python
await cg.start()
await cg.stop()
await cg.start()  # restartable
```

---

### 4. `SSESubscriber` — Server-Sent Events to Browser

**Transport**: Redis Pub/Sub
**Delivery**: Broadcast (each SSE connection gets all matching events)
**Pattern support**: Full wildcard support (`*`, `**`)

```python
from event_bus.adapters.sse import SSESubscriber

subscriber = SSESubscriber(
    patterns=["orders.*"],
    max_queue_size=256,
    heartbeat_interval=15.0,
)
bus.add_subscriber(subscriber)
await subscriber.start()

async for chunk in subscriber.events():
    await response.write(chunk.encode())

await subscriber.stop()
bus.remove_subscriber(subscriber)
```

SSE output format:

```
id: 3f2a1b4c-...
event: orders.created
data: {"order_id": "ord-123", "total": 99.99}

: heartbeat

id: 8e7d6c5b-...
event: orders.updated
data: {"order_id": "ord-123", "status": "shipped"}
```

The `event:` field is the topic name, so browser clients can listen for specific topics:

```javascript
const source = new EventSource('/events/sse?topics=orders.*');
source.addEventListener('orders.created', (e) => {
    console.log(JSON.parse(e.data));
});
```

**Use for:** Real-time dashboards, live feeds, notification popups in the browser.

---

### 5. `WebSocketSubscriber` — WebSocket Push to Browser

**Transport**: Redis Pub/Sub
**Delivery**: Broadcast (each WS connection gets all matching events)
**Pattern support**: Full wildcard support (`*`, `**`)

```python
from event_bus.adapters.websocket import WebSocketSubscriber

subscriber = WebSocketSubscriber(
    patterns=["orders.*"],
    ws=websocket,  # any object with send_text(str)
    max_queue_size=512,
    ping_interval=30.0,
)
bus.add_subscriber(subscriber)
await subscriber.start()
# ... connection open ...
await subscriber.stop()
bus.remove_subscriber(subscriber)
```

JSON output format:

```json
{
    "type": "event",
    "topic": "orders.created",
    "data": {"order_id": "ord-123"},
    "metadata": {
        "event_id": "3f2a1b4c-...",
        "timestamp": 1708617600.0,
        "source": "order-service",
        "correlation_id": "req-abc"
    }
}
```

Custom message format:

```python
import json


def dashboard_format(envelope):
    return json.dumps(
        {
            "id": envelope.event.event_id,
            "type": envelope.event.topic.split(".")[-1],
            "payload": envelope.event.data,
        }
    )


subscriber = WebSocketSubscriber(
    patterns=["orders.*"],
    ws=websocket,
    format_message=dashboard_format,
)
```

**Use for:** Interactive UIs, chat, collaborative editing — anything needing bidirectional communication or custom framing.

---

### 6. `bus.add_subscriber()` — Custom Subscribers

Build your own subscriber by implementing the `Subscriber` protocol or extending `BaseSubscriber`:

```python
from event_bus.interfaces.subscriber import BaseSubscriber
from event_bus.core.event import EventEnvelope


class MetricsSubscriber(BaseSubscriber):
    """Push events to Prometheus/StatsD."""

    def __init__(self, patterns: list[str], metrics_client):
        super().__init__(patterns)
        self._metrics = metrics_client

    async def on_event(self, envelope: EventEnvelope) -> None:
        self._metrics.increment(
            "events.received",
            tags={"topic": envelope.event.topic},
        )


# Register it
sub = MetricsSubscriber(["**"], statsd_client)
bus.add_subscriber(sub)
```

`BaseSubscriber` gives you:
- `self._patterns` — compiled `TopicPattern` list
- `self.accepts(topic)` — returns True if topic matches any pattern
- `self._running` — set by `start()`/`stop()`

The shared listener matches against `sub.patterns` before `sub.on_event()`, so custom subscribers only need to expose the `patterns`, `on_event()`, `start()`, and `stop()` protocol. `BaseSubscriber.accepts()` is still available as a convenience helper.

---

## Batch vs Single Handlers

Consumer groups automatically detect your handler mode from the **type hint** on the first parameter:

### Single mode — `event: Event`

```python
@bus.consumer_group(["orders.created"], "processor")
async def handle(event: Event) -> None:
    await db.insert(event.data)
```

Called once per event. Each event is ACKed individually.

### Batch mode — `events: list[Event]`

```python
@bus.consumer_group(
    ["orders.created"],
    "batch-processor",
    config=ConsumerGroupConfig(batch_size=100),
)
async def handle(events: list[Event]) -> None:
    await db.insert_many([e.data for e in events])
```

Called once per topic batch returned by `XREADGROUP`. All events ACKed together on success. On failure, each event is evaluated individually for retry/DLQ using Redis Stream delivery counts; retried events come back to the handler as a list too.

The `batch_size` config controls the maximum number of events per batch (default 10). The actual batch size depends on how many messages are available in the stream.

**No configuration flag needed** — just change the type hint.

---

## Scoped Topics

Use hierarchical topics to scope events to specific entities. This is the primary pattern for per-user, per-transaction, or per-tenant event streams.

### User-Scoped Events

```python
# Publishing
await bus.publish(f"order.{user_id}", {"status": "shipped", "order_id": "ord-123"})
await bus.publish(f"notification.{user_id}", {"message": "Your order shipped!"})

# Subscribing to one user's events (SSE endpoint)
subscriber = SSESubscriber([f"order.{user_id}"])

# Subscribing to ALL users' order events (admin dashboard)
subscriber = SSESubscriber(["order.*"])

# Consumer group for all user orders (backend processing)
bus.create_consumer_group(
    topics=[f"order.{user_id}"],  # exact topic per user
    group_name="user-order-processor",
    handler=process_user_order,
)
```

### Transaction-Scoped Events

```python
# Track a specific transaction lifecycle
txn_id = "txn_abc123"

await bus.publish(f"payment.{txn_id}.initiated", {"amount": 99.99})
await bus.publish(f"payment.{txn_id}.authorized", {"auth_code": "XYZ"})
await bus.publish(f"payment.{txn_id}.completed", {"receipt": "R-001"})

# Client subscribes to all events for this transaction
subscriber = SSESubscriber([f"payment.{txn_id}.*"])

# Or all phases of all transactions (monitoring)
subscriber = SSESubscriber(["payment.**"])
```

### Tenant-Scoped Events (Multi-Tenancy)

```python
# Namespace events by tenant
tenant = "acme-corp"

await bus.publish(f"{tenant}.orders.created", {"order_id": "ord-1"})
await bus.publish(f"{tenant}.payments.received", {"amount": 500})

# All events for this tenant
subscriber = SSESubscriber([f"{tenant}.**"])

# All orders across all tenants (admin)
subscriber = SSESubscriber(["*.orders.*"])

# Consumer group per tenant
bus.create_consumer_group(
    topics=[f"{tenant}.orders.created"],
    group_name=f"{tenant}-order-processor",
    handler=process_order,
)
```

### Region or Environment Scoping

```python
await bus.publish("us-east.inventory.low-stock", {"sku": "WIDGET-A"})
await bus.publish("eu-west.inventory.low-stock", {"sku": "WIDGET-B"})

# All inventory events across all regions
subscriber = SSESubscriber(["*.inventory.*"])

# Everything in us-east
subscriber = SSESubscriber(["us-east.**"])
```

---

## Dynamic Subscriptions

Subscribers can be added and removed while the bus is running:

```python
async with bus:
    # Add a subscriber dynamically
    sub = SSESubscriber(["orders.*"])
    bus.add_subscriber(sub)
    await sub.start()

    # ... later, when client disconnects ...
    bus.remove_subscriber(sub)
    await sub.stop()
```

Consumer groups can also be registered after startup:

```python
async with bus:
    # Register a new consumer group at runtime
    cg = bus.create_consumer_group(
        topics=["orders.created"],
        group_name="late-processor",
        handler=my_handler,
    )
    # Automatically started via background task

    # ... later ...
    await cg.stop()
```

### WebSocket Topic Switching

WebSocket clients can change their subscriptions without reconnecting:

```python
# Client sends: {"action": "update_topics", "topics": ["payments.*"]}

if msg.get("action") == "update_topics":
    await subscriber.stop()
    bus.remove_subscriber(subscriber)
    subscriber = WebSocketSubscriber(
        patterns=msg["topics"],
        ws=websocket,
    )
    bus.add_subscriber(subscriber)
    await subscriber.start()
```

---

## Multiple Patterns Per Subscriber

Every subscription method accepts a **list** of patterns. An event matches if it matches **any** pattern in the list:

```python
# Decorator — multiple positional args
@bus.subscribe("orders.*", "payments.*", "inventory.**")
async def handler(event: Event) -> None: ...


# SSE — list of patterns
subscriber = SSESubscriber(["orders.*", "payments.*"])

# WebSocket — list of patterns
subscriber = WebSocketSubscriber(["orders.*", "payments.*"], ws=websocket)


# Consumer group — list of exact topics
@bus.consumer_group(
    topics=["orders.created", "orders.updated", "orders.cancelled"],
    group_name="order-processor",
)
async def handler(event: Event) -> None: ...
```

---

## Pattern Matching Limitations

### Consumer groups do NOT support wildcards

Consumer groups read from **exact Redis Stream keys**. `topics=["orders.*"]` creates a literal stream named `eventbus:stream:orders.*`, not a pattern.

```python
# These work — exact topic names
@bus.consumer_group(topics=["orders.created", "orders.updated"], ...)

# This does NOT work as you'd expect
@bus.consumer_group(topics=["orders.*"], ...)  # literal stream name
```

If you need wildcard consumption with durability, list all topics explicitly or use a flat topic with data filtering:

```python
# Option 1: list all topics
@bus.consumer_group(
    topics=["orders.created", "orders.updated", "orders.cancelled"],
    group_name="order-processor",
)
async def handle(event: Event) -> None: ...


# Option 2: flat topic, filter in handler
@bus.consumer_group(topics=["orders"], group_name="order-processor")
async def handle(event: Event) -> None:
    action = event.data.get("action")
    if action in ("created", "updated"):
        await process(event)
```

### `**` matches one or more segments, not zero

`orders.**` does **not** match `orders` (zero segments after the dot). It matches `orders.created` (one segment), `orders.a.b` (two segments), etc.

| Pattern | Does NOT Match |
|---|---|
| `orders.**` | `orders` (no segments after) |
| `**` | *(matches everything with at least one dot-separated segment)* |

### Patterns are matched at consumption time, not publish time

When you publish, the event goes to **all** streams and channels regardless of who is subscribed. Pattern filtering happens when the shared listener distributes events to subscribers. This means:

- Publishing `orders.created` always writes to `eventbus:stream:orders.created` and `eventbus:channel:orders.created`
- A subscriber with pattern `payments.*` simply ignores it
- There is no way to "publish to a pattern" — you always publish to an exact topic

---

## Choosing the Right Subscription Method

| Method | Transport | Wildcards | Delivery | Durability | Use Case |
|---|---|---|---|---|---|
| `@bus.subscribe()` | Pub/Sub | Yes | Broadcast (all instances) | None | Logging, metrics, cache |
| `@bus.consumer_group()` | Streams | No (exact) | One per group | At-least-once + DLQ | Orders, payments, emails |
| `bus.create_consumer_group()` | Streams | No (exact) | One per group | At-least-once + DLQ | Same, but post-fork safe |
| `SSESubscriber` | Pub/Sub | Yes | Per-connection | None | Browser dashboards |
| `WebSocketSubscriber` | Pub/Sub | Yes | Per-connection | None | Interactive UIs |
| `bus.add_subscriber()` | Pub/Sub | Yes | Per-instance | None | Custom integrations |

**Decision shortcuts:**

- Must not lose the event? → **Consumer group**
- Pushing to a browser? → **SSE** or **WebSocket**
- Logging/monitoring? → **`@bus.subscribe()`**
- Need wildcards with durability? → Not directly supported. Use a consumer group with explicit topics, or use `@bus.subscribe()` that writes to a database for persistence
- Multiple services need the same event? → Multiple consumer groups with different `group_name` values
- Load-balance across workers? → One consumer group with `concurrency > 1`
