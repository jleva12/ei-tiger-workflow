# Redis Queue Types: Streams vs Pub/Sub

This guide explains the two Redis messaging primitives used by the event bus, how they differ, and when to use each.

---

## Overview

The event bus uses a **dual-write architecture**. Every `bus.publish()` call writes the same event to **both** Redis primitives:

```
bus.publish("order.created", {...})
        │
        ├── 1. Redis Stream   →  eventbus:stream:order.created   (durable)
        │
        └── 2. Redis Pub/Sub  →  eventbus:channel:order.created  (real-time)
```

They serve fundamentally different purposes. You don't choose one or the other at publish time -- the choice happens at **consumption time** based on what guarantees you need.

---

## Redis Streams

### What it is

A Redis Stream is an **append-only log**. Think of it like a Kafka partition living inside Redis. Every message gets a unique, time-ordered ID (e.g. `1708617600000-0`) and stays in the stream until explicitly trimmed.

### Redis commands used

| Command | Purpose |
|---|---|
| `XADD` | Append an event to the stream |
| `XREADGROUP` | Read new messages as part of a consumer group |
| `XACK` | Acknowledge successful processing |
| `XPENDING` | List messages that were delivered but not yet ACKed |
| `XCLAIM` | Steal an unacknowledged message from a dead/slow consumer |
| `XGROUP CREATE` | Create a consumer group on a stream |

### How it maps in the event bus

```
Topic:  "order.created"
  → Redis key:  "eventbus:stream:order.created"
  → Written via:  XADD
  → Read via:    XREADGROUP (consumer groups)
```

Each unique topic becomes its own Redis Stream. Consumer groups read from a specific stream key -- they do **not** support pattern matching (no wildcards).

### Guarantees

- **Durable**: Messages persist in Redis until trimmed by `MAXLEN`
- **Ordered**: Messages are strictly ordered within a stream
- **At-least-once delivery**: Messages are tracked per consumer group. If a consumer crashes before ACKing, the message stays pending and can be reclaimed by another consumer
- **Replayable**: New consumer groups can start from `0` to replay the entire stream history
- **Parallel processing**: Multiple consumers in a group split the work -- each message goes to exactly one consumer in the group
- **Backpressure-aware**: `XREADGROUP` with `BLOCK` waits for new messages instead of busy-polling

### When to use (consumer groups)

Use consumer groups when you need **reliable processing** -- the work **must happen** and you cannot afford to lose events:

- Order processing, payment handling, inventory updates
- Sending emails, SMS, push notifications
- Database writes, ETL pipelines
- Audit logging where every event must be recorded
- Any workflow where a missed event means broken business logic

### Code path

```python
# The decorator registers a consumer group that reads from Redis Streams
@bus.consumer_group(
    topics=["order.created", "order.updated"],  # exact stream keys
    group_name="order-processor",
)
async def process_order(event: Event) -> None:
    await save_to_database(event.data)
    # If this succeeds → XACK (message removed from pending)
    # If this raises  → retry with backoff, eventually DLQ
```

### Lifecycle of a message in Streams

```
1. Publisher calls bus.publish("order.created", data)
2. XADD eventbus:stream:order.created → message ID "1708617600000-0"
3. Consumer group "order-processor" calls XREADGROUP → gets the message
4. Handler processes it successfully
5. XACK → message marked as processed
   ─── OR ───
4. Handler raises an exception
5. Message stays in PEL (Pending Entries List); the worker sets its idle
   time to claim_idle_ms - backoff delay (XCLAIM ... IDLE) and moves on
6. Once the delay has passed, the reclaimer (XPENDING → XCLAIM) takes it
   and retries it, on this replica or another
7. After max_retries failures → XADD to eventbus:dlq:order-processor and
   XACK, in one MULTI transaction
```

---

## Redis Pub/Sub

### What it is

Redis Pub/Sub is a **fire-and-forget broadcast**. A publisher sends a message to a channel, and every subscriber currently listening on that channel receives it instantly. There is **no persistence** -- if nobody is listening, the message is gone.

### Redis commands used

| Command | Purpose |
|---|---|
| `PUBLISH` | Send a message to a channel |
| `PSUBSCRIBE` | Subscribe to channels matching a glob pattern |
| `PUNSUBSCRIBE` | Unsubscribe from patterns |

### How it maps in the event bus

```
Topic:  "order.created"
  → Redis channel:  "eventbus:channel:order.created"
  → Written via:    PUBLISH
  → Read via:       PSUBSCRIBE (pattern-based)
```

When realtime subscribers are registered, the shared listener in the event bus subscribes to `eventbus:channel:**` (all channels) via `PSUBSCRIBE` and then distributes events to subscribers based on their topic patterns.

### Guarantees

- **Real-time**: Sub-millisecond latency from publish to receive
- **Fan-out**: Every connected subscriber gets every matching message (broadcast, not load-balanced)
- **No persistence**: If the subscriber isn't connected when the message is published, it's lost
- **No acknowledgment**: There is no ACK, no retry, no pending list
- **Pattern matching**: Subscribers can use glob patterns (`order.*`, `order.**`)
- **Zero overhead when idle**: No polling, no sleeping -- the Redis connection is pushed to

### When to use (subscribers / adapters)

Use Pub/Sub when you need **instant notification** and can tolerate occasional missed events:

- Real-time UI updates (dashboards, live feeds, notifications)
- SSE endpoints pushing events to browsers
- WebSocket connections for live data
- Cache invalidation (best-effort is fine)
- Metrics and monitoring (missing one data point is acceptable)
- Any scenario where the consumer is ephemeral (a browser tab, a WebSocket connection)

### Code path

```python
# Lightweight subscriber -- receives via Pub/Sub, no durability
@bus.subscribe("order.*")
async def on_order(event: Event) -> None:
    print(f"Order event: {event.topic}")


# SSE adapter -- forwards Pub/Sub events to a browser
subscriber = SSESubscriber(["order.*"])
bus.add_subscriber(subscriber)

# WebSocket adapter -- forwards Pub/Sub events to a WebSocket
ws_sub = WebSocketSubscriber(["order.*"], ws=websocket)
bus.add_subscriber(ws_sub)
```

### Lifecycle of a message in Pub/Sub

```
1. Publisher calls bus.publish("order.created", data)
2. PUBLISH eventbus:channel:order.created → N subscribers notified
3. Shared listener receives via PSUBSCRIBE
4. For each registered subscriber:
   - Does subscriber pattern match "order.created"?
   - Yes → put_nowait() into subscriber's bounded queue
   - Queue full? → drop event, count it in dropped_count, log a warning at
     most every 10s (slow consumer protection)
5. Subscriber's drain loop / SSE generator / WS send loop picks it up
6. Done. No ACK, no retry, no persistence.
```

---

## Side-by-Side Comparison

| | Redis Streams | Redis Pub/Sub |
|---|---|---|
| **Persistence** | Yes -- messages stored until trimmed | No -- fire and forget |
| **Delivery guarantee** | At-least-once (with ACK + retry) | At-most-once (best effort) |
| **Consumer model** | Consumer groups (one message per consumer) | Broadcast (every subscriber gets it) |
| **Pattern matching** | No -- exact stream key only | Yes -- glob patterns (`*`, `**`) |
| **Ordering** | Strict within a stream | No ordering guarantees |
| **Backpressure** | XREADGROUP blocks until messages available | Subscribers must drain fast or lose messages |
| **Replay** | Yes -- read from any point in history | No -- only live messages |
| **Dead letter queue** | Yes -- failed messages go to DLQ | No |
| **Retry** | Yes -- exponential backoff with reclaim | No |
| **Latency** | Milliseconds (polling interval + block_ms) | Sub-millisecond (push-based) |
| **Redis key** | `eventbus:stream:{topic}` | `eventbus:channel:{topic}` |
| **Read command** | `XREADGROUP` | `PSUBSCRIBE` |
| **Typical consumers** | Backend services, workers, processors | Browser clients, dashboards, caches |

---

## How the Dual-Write Works

When you call `bus.publish()`, the event is written to both systems in sequence:

```
bus.publish("order.created", {"order_id": "123"})
    │
    ├─ 1. XADD eventbus:stream:order.created    ← must succeed (source of truth)
    │      stream_id = "1708617600000-0"
    │
    ├─ 2. PUBLISH eventbus:channel:order.created ← best-effort (non-fatal if it fails)
    │      subscribers_notified = 3
    │
    └─ return stream_id
```

**Stream write is the primary operation.** If it fails, the whole publish fails and the circuit breaker records a failure.

**Pub/Sub is secondary.** If it fails, the error is logged as a warning but the publish still succeeds. The event is safe in the stream -- consumer groups will get it. Browser clients may miss the instant push but the data isn't lost.

This means:
- Consumer groups **always** get every event (via Streams)
- Pub/Sub subscribers **usually** get every event in real-time, but may miss events if Pub/Sub has a momentary failure
- The system is **consistent** in the durable path and **best-effort** in the real-time path

---

## Choosing the Right Consumer for Your Use Case

### "I need to process every order -- none can be missed"

**Use a consumer group** (Redis Streams):

```python
@bus.consumer_group(["order.created"], "order-processor")
async def process(event: Event) -> None:
    await charge_customer(event.data)
```

### "I want to show live order updates on a dashboard"

**Use an SSE or WebSocket subscriber** (Redis Pub/Sub):

```python
subscriber = SSESubscriber(["order.*"])
bus.add_subscriber(subscriber)
```

### "I need both -- process reliably AND update the UI in real-time"

**Use both.** The dual-write ensures both paths get the event from a single `publish()` call:

```python
# Backend -- durable processing (Streams)
@bus.consumer_group(["order.created"], "order-processor")
async def process(event: Event) -> None:
    await save_order(event.data)
    await bus.publish("order.confirmed", {"order_id": event.data["order_id"]})


# Frontend -- live updates (Pub/Sub)
@app.get("/events/orders/{user_id}")
async def order_feed(request: Request, user_id: str):
    subscriber = SSESubscriber([f"order.{user_id}"])
    bus.add_subscriber(subscriber)
    await subscriber.start()
    # ... stream to client ...
```

### "I want to fan out to multiple independent services"

**Use multiple consumer groups** with different `group_name` values on the same topic. Each group independently tracks its own position in the stream:

```python
# Service A -- both groups get every event
@bus.consumer_group(["order.created"], "billing-service")
async def bill(event): ...


# Service B
@bus.consumer_group(["order.created"], "shipping-service")
async def ship(event): ...


# Service C
@bus.consumer_group(["order.created"], "analytics-service")
async def track(event): ...
```

### "I want to load-balance processing across workers"

**Use one consumer group with higher concurrency.** Workers within the same group split the messages:

```python
@bus.consumer_group(
    ["order.created"],
    "order-processor",
    config=ConsumerGroupConfig(concurrency=5),  # 5 parallel workers
)
async def process(event): ...
```

### "I want transaction-specific subscriptions from a client"

**Use hierarchical topics with Pub/Sub subscribers.** Each client subscribes to its own scoped topic:

```python
# Publish to a user-scoped topic
await bus.publish(f"order.{user_id}", {"status": "shipped"})

# Client subscribes to only their events (via SSE endpoint)
subscriber = SSESubscriber([f"order.{user_id}"])
```

---

## Topic-to-Key Mapping

Understanding how topics become Redis keys clarifies the capabilities:

```
Topic: "order.created"

  Stream key:   eventbus:stream:order.created     ← XADD / XREADGROUP
  Channel key:  eventbus:channel:order.created    ← PUBLISH / PSUBSCRIBE

Topic: "order.user_abc123"

  Stream key:   eventbus:stream:order.user_abc123
  Channel key:  eventbus:channel:order.user_abc123
```

The `key_prefix` (default `"eventbus"`) is configurable in `RedisConfig`. The stream and channel keys are constructed by `topic_to_stream_key()` and `topic_to_channel()` in `src/event_bus/core/topic.py`.

### Pattern matching differences

**Pub/Sub subscribers** use `PSUBSCRIBE` which supports Redis glob patterns. The event bus translates topic patterns into channel patterns:

```
Subscriber pattern: "order.*"
  → PSUBSCRIBE eventbus:channel:order.*
  → Matches: order.created, order.user_abc, order.anything
  → Does NOT match: order.item.added (that's two segments)

Subscriber pattern: "order.**"
  → PSUBSCRIBE eventbus:channel:order.**
  → Matches: order.created, order.item.added, order.a.b.c
```

**Consumer groups** use `XREADGROUP` which reads from an exact stream name:

```
Consumer group topic: "order.created"
  → XREADGROUP ... STREAMS eventbus:stream:order.created >
  → Reads from ONLY that one stream

Consumer group topic: "order.*"
  → XREADGROUP ... STREAMS eventbus:stream:order.* >
  → This is a LITERAL stream name, NOT a pattern
  → Would only match if someone published to the topic "order.*" literally
```

This is a Redis limitation, not an event bus limitation. Redis Streams do not support pattern-based reads.

---

## Connection Pools

The event bus maintains **three separate Redis connection pools** to prevent Streams and Pub/Sub from interfering with each other:

| Pool | Purpose | Socket Timeout |
|---|---|---|
| Command pool | `XADD`, `XACK`, `PUBLISH`, `XPENDING`, `XCLAIM` | `socket_timeout` (default 5s) |
| Blocking pool | `XREADGROUP` with `BLOCK` | `blocking_socket_timeout` (default 30s) |
| Pub/Sub pool | `PSUBSCRIBE` for realtime subscribers | `pubsub_socket_timeout` (default 30s) |

The blocking pool's timeout must exceed the longest `block_ms` a consumer group
uses; it only bounds how long a worker waits on a dead connection. The Pub/Sub
connection is polled with a short timeout and pinged every
`health_check_interval`, so a half-open connection is noticed instead of
hanging the listener. Pub/Sub subscriptions use their own pool so
browser/live subscribers cannot exhaust the stream worker pool.

All three are blocking pools: when every connection is in use a caller waits up
to `pool_timeout` for one instead of failing, and that wait never counts
against the circuit breaker.

---

## Dead Letter Queue

Each consumer group's DLQ is a Redis Stream at `eventbus:dlq:<group>`. When a
group's handler fails beyond `max_retries`, based on Redis Stream pending
delivery counts, the event is moved there (and acknowledged in the same
transaction); other groups reading the same topic are unaffected:

```
Normal stream:  eventbus:stream:order.created
                    │
                    │  order-processor's handler fails 5 times
                    ▼
Dead letter:    eventbus:dlq:order-processor
                    │
                    │  inspect / replay into order-processor only
                    ▼
                await bus.replay_dead_letters("order-processor")
```

Pub/Sub subscribers have no DLQ because they have no retry mechanism. If a Pub/Sub event is dropped (queue full, subscriber disconnected), it's gone.

---

## Summary Decision Tree

```
Do you need every event processed reliably?
├── YES → Use a consumer group (Redis Streams)
│         - At-least-once delivery
│         - Automatic retry + DLQ
│         - Parallel workers
│         - Survives restarts
│
└── NO → Is it for a browser/client connection?
    ├── YES → Use SSE or WebSocket subscriber (Redis Pub/Sub)
    │         - Sub-millisecond latency
    │         - Pattern matching (order.*)
    │         - Per-client scoping (order.{userId})
    │         - Automatic cleanup on disconnect
    │
    └── NO → Use a lightweight subscriber (Redis Pub/Sub)
              - Fire-and-forget callbacks
              - Good for logging, metrics, cache invalidation
              - Bounded queue protects other subscribers
```
