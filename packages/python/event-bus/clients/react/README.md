# @event-bus/react

Type-safe React hook for subscribing to the event bus via SSE. Handles connection lifecycle, automatic reconnection with exponential backoff, and typed per-topic event handlers.

## Install

```bash
npm install @event-bus/react
```

Peer dependency: `react >= 18.0.0`

## Quick Start

```tsx
import { useEventBus } from "@event-bus/react";

// 1. Define your event types once
interface MyEvents {
  "orders.created": { order_id: string; total: number; customer: string };
  "orders.updated": { order_id: string; status: string };
  "orders.cancelled": { order_id: string; reason: string };
}

// 2. Use the hook
function OrderDashboard() {
  const { status } = useEventBus<MyEvents>({
    url: "/events/sse",
    topics: ["orders.*"],
    handlers: {
      "orders.created": (data) => {
        // data is typed as { order_id: string; total: number; customer: string }
        console.log("New order:", data.order_id, data.total);
      },
      "orders.updated": (data) => {
        // data is typed as { order_id: string; status: string }
        console.log("Order updated:", data.order_id, data.status);
      },
    },
  });

  return <div>Connection: {status}</div>;
}
```

That's it. The hook connects to your SSE endpoint, subscribes to the topic patterns, and calls the right handler with fully typed data for each event.

## API

### `useEventBus<TMap>(options): UseEventBusReturn`

#### Type Parameter

| Param | Description |
|---|---|
| `TMap` | An interface mapping topic names to their data payload types. Keys are exact topic strings, values are the `data` object shape. |

#### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `url` | `string` | *required* | SSE endpoint path or URL. Topics are appended as `?topics=...` |
| `topics` | `string[]` | *required* | Topic patterns to subscribe to (`*`, `**` supported) |
| `handlers` | `{ [topic]: handler }` | `undefined` | Typed per-topic event handlers |
| `onEvent` | `(topic, data, info) => void` | `undefined` | Catch-all handler for every event |
| `onConnect` | `() => void` | `undefined` | Called when the connection opens |
| `onDisconnect` | `() => void` | `undefined` | Called when the connection is lost |
| `onError` | `(error: Error) => void` | `undefined` | Called on connection or parse errors |
| `enabled` | `boolean` | `true` | Set to `false` to disconnect without unmounting |
| `withCredentials` | `boolean` | `false` | Send cookies with the request |
| `headers` | `Record<string, string>` | `undefined` | Custom HTTP headers (e.g. Authorization) |
| `maxReconnectAttempts` | `number` | `Infinity` | Stop trying after this many reconnects |
| `reconnectDelay` | `number` | `1000` | Initial reconnect delay in ms |
| `maxReconnectDelay` | `number` | `30000` | Maximum reconnect delay in ms |

#### Return Value

| Field | Type | Description |
|---|---|---|
| `status` | `ConnectionStatus` | `"connecting"`, `"connected"`, `"reconnecting"`, or `"disconnected"` |
| `lastEvent` | `object \| null` | The most recently received event (`{ topic, data, info }`) |
| `disconnect()` | `() => void` | Manually close the connection (no auto-reconnect) |
| `reconnect()` | `() => void` | Manually reconnect (resets attempt counter) |

## Topic Patterns

The `topics` array controls which events the server sends. The server-side event bus supports glob patterns:

| Pattern | Matches | Does Not Match |
|---|---|---|
| `orders.created` | `orders.created` | `orders.updated` |
| `orders.*` | `orders.created`, `orders.updated` | `orders.item.added` |
| `orders.**` | `orders.created`, `orders.item.added` | `payments.created` |
| `**` | everything | |

## Usage Patterns

### Per-Topic Typed Handlers

The primary pattern. Define an `EventMap` interface and get full type safety:

```tsx
interface Events {
  "orders.created": { order_id: string; total: number };
  "orders.updated": { order_id: string; status: string };
  "payments.completed": { payment_id: string; amount: number };
}

function App() {
  const { status } = useEventBus<Events>({
    url: "/events/sse",
    topics: ["orders.*", "payments.*"],
    handlers: {
      "orders.created": (data) => {
        // TypeScript knows: data.order_id is string, data.total is number
        addToOrderList(data);
      },
      "orders.updated": (data) => {
        // TypeScript knows: data.status is string
        updateOrderStatus(data.order_id, data.status);
      },
      "payments.completed": (data) => {
        // TypeScript knows: data.payment_id is string
        showPaymentConfirmation(data);
      },
    },
  });
}
```

### Catch-All Handler

For logging, debugging, or forwarding all events to state management:

```tsx
const { status } = useEventBus<Events>({
  url: "/events/sse",
  topics: ["**"],
  onEvent: (topic, data, info) => {
    console.log(`[${topic}]`, data);
    // dispatch to Redux, Zustand, etc.
    store.dispatch({ type: "EVENT_RECEIVED", topic, data });
  },
});
```

### Both Typed and Catch-All

Typed handlers fire first, then the catch-all. Both receive the same event:

```tsx
const { status } = useEventBus<Events>({
  url: "/events/sse",
  topics: ["orders.*"],
  handlers: {
    "orders.created": (data) => addOrder(data),
  },
  onEvent: (topic, data) => {
    // Runs for ALL events, including orders.created
    analytics.track("event_received", { topic });
  },
});
```

### User-Scoped Subscription

Subscribe to events for a specific user, transaction, or entity:

```tsx
function UserOrders({ userId }: { userId: string }) {
  const { status, lastEvent } = useEventBus<Events>({
    url: "/events/sse",
    topics: [`order.${userId}`],
    handlers: {
      [`order.${userId}`]: (data) => {
        setOrders((prev) => [...prev, data]);
      },
    },
  });

  return <div>Listening for {userId}'s orders ({status})</div>;
}
```

### Connection State UI

```tsx
function ConnectionBadge() {
  const { status, reconnect, disconnect } = useEventBus<Events>({
    url: "/events/sse",
    topics: ["**"],
    onEvent: (topic, data) => handleEvent(topic, data),
  });

  return (
    <div>
      <span
        style={{
          color:
            status === "connected"
              ? "green"
              : status === "reconnecting"
                ? "orange"
                : "red",
        }}
      >
        {status}
      </span>
      {status === "disconnected" && (
        <button onClick={reconnect}>Reconnect</button>
      )}
      {status === "connected" && (
        <button onClick={disconnect}>Disconnect</button>
      )}
    </div>
  );
}
```

### Conditional Connection

Use `enabled` to control when the connection is active:

```tsx
function ProtectedDashboard() {
  const { user, isAuthenticated } = useAuth();

  const { status } = useEventBus<Events>({
    url: "/events/sse",
    topics: ["orders.*"],
    enabled: isAuthenticated,  // only connect when logged in
    headers: useMemo(
      () => ({ Authorization: `Bearer ${user?.token}` }),
      [user?.token],
    ),
    handlers: {
      "orders.created": (data) => addOrder(data),
    },
  });
}
```

### With React State

```tsx
function LiveOrderList() {
  const [orders, setOrders] = useState<Order[]>([]);

  useEventBus<Events>({
    url: "/events/sse",
    topics: ["orders.*"],
    handlers: {
      "orders.created": (data) => {
        setOrders((prev) => [...prev, { ...data, status: "new" }]);
      },
      "orders.updated": (data) => {
        setOrders((prev) =>
          prev.map((o) =>
            o.order_id === data.order_id ? { ...o, status: data.status } : o,
          ),
        );
      },
    },
  });

  return (
    <ul>
      {orders.map((o) => (
        <li key={o.order_id}>
          {o.order_id} — {o.status}
        </li>
      ))}
    </ul>
  );
}
```

### Last Event Shorthand

`lastEvent` gives you the most recent event without maintaining your own state:

```tsx
function LatestOrder() {
  const { lastEvent } = useEventBus<Events>({
    url: "/events/sse",
    topics: ["orders.created"],
  });

  if (!lastEvent) return <p>Waiting for orders...</p>;

  return (
    <div>
      <p>Latest: {(lastEvent.data as Events["orders.created"]).order_id}</p>
      <small>Event ID: {lastEvent.info.eventId}</small>
    </div>
  );
}
```

## Reconnection

The hook automatically reconnects when the connection is lost:

1. Connection drops or server closes the stream
2. Status changes to `"disconnected"`, then `"reconnecting"`
3. Exponential backoff: 1s, 2s, 4s, 8s, 16s, ... up to `maxReconnectDelay` (30s)
4. Random jitter (up to 20% of delay, capped at 1s) prevents thundering herd
5. Resets to 0 on successful reconnect

```
Lost connection
  ├── Attempt 1: wait ~1.0s
  ├── Attempt 2: wait ~2.0s
  ├── Attempt 3: wait ~4.0s
  ├── Attempt 4: wait ~8.0s
  ├── Attempt 5: wait ~16.0s
  └── Attempt 6: wait ~30.0s (capped)
       └── Connected! → reset counter
```

To stop reconnecting:
- Call `disconnect()` — clears the timer, no more attempts
- Set `enabled={false}` — same effect, declarative
- Set `maxReconnectAttempts={5}` — gives up after 5 failures

To manually reconnect after `disconnect()`:
- Call `reconnect()` — resets the attempt counter and starts fresh

## Authentication

### Cookie-based auth

```tsx
useEventBus<Events>({
  url: "/events/sse",
  topics: ["orders.*"],
  withCredentials: true,  // sends cookies
  handlers: { ... },
});
```

### Bearer token auth

```tsx
const { token } = useAuth();

useEventBus<Events>({
  url: "/events/sse",
  topics: ["orders.*"],
  headers: useMemo(
    () => ({ Authorization: `Bearer ${token}` }),
    [token],
  ),
  handlers: { ... },
});
```

When the token changes, the hook reconnects automatically with the new header.

## How It Works

Under the hood, the hook uses `fetch()` + `ReadableStream` to consume the SSE stream instead of native `EventSource`. This gives us:

- **Custom headers** — `EventSource` doesn't support custom headers, only cookies
- **Catch-all event handling** — `EventSource` only dispatches to named `addEventListener` listeners; there's no way to catch all named events generically
- **Full reconnect control** — custom exponential backoff with jitter instead of the browser's basic retry

The SSE parser handles the full [SSE specification](https://html.spec.whatwg.org/multipage/server-sent-events.html):
- `id:`, `event:`, `data:`, `retry:` fields
- Multi-line `data:` (joined with newlines)
- Comment lines (`: heartbeat`) silently ignored
- All line endings (`\n`, `\r`, `\r\n`)

## Server-Side Setup

This hook expects the event bus SSE endpoint format. A minimal FastAPI example:

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from event_bus.adapters.sse import SSESubscriber


@app.get("/events/sse")
async def sse(request: Request, topics: str = "**"):
    subscriber = SSESubscriber(patterns=[t.strip() for t in topics.split(",")])
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
            "X-Accel-Buffering": "no",
        },
    )
```

The SSE stream format produced by the event bus:

```
id: 3f2a1b4c-5d6e-7f8a-9b0c-1d2e3f4a5b6c
event: orders.created
data: {"order_id":"ord-123","total":99.99}

: heartbeat

id: 8e7d6c5b-4a3f-2e1d-0c9b-8a7f6e5d4c3b
event: orders.updated
data: {"order_id":"ord-123","status":"shipped"}

```

- `id:` — the event's unique ID
- `event:` — the topic name (used to dispatch to typed handlers)
- `data:` — JSON-encoded event payload
- `: heartbeat` — keep-alive comment (ignored by the parser)
