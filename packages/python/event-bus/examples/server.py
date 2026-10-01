"""
Example FastAPI server demonstrating the event bus.

FastAPI and Uvicorn are not dependencies of the package; uv adds them for the
run. From the repository root, start the shared Redis, then run the server in
packages/python/event-bus:

    make shared-deps
    uv run --with fastapi --with uvicorn uvicorn examples.server:app --reload

Then open http://localhost:8000 in your browser. The server connects to
FORGE_REDIS_URL, by default the shared Redis on redis://127.0.0.1:16389/0.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal as _signal
import string
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse

from event_bus import Event, EventBus, InvalidTopicError
from event_bus.adapters.sse import SSESubscriber
from event_bus.adapters.websocket import WebSocketSubscriber
from event_bus.config import EventBusConfig, RedisConfig
from event_bus.consumer.group import ConsumerGroupConfig
from event_bus.transport.redis.transport import RedisTransport

# ---------------------------------------------------------------------------
# Setup — bus is created per-worker inside lifespan to avoid fork issues.
# Module-level reference so route handlers can access it.
# ---------------------------------------------------------------------------

bus: EventBus | None = None
_shutdown_event = asyncio.Event()


def _get_bus() -> EventBus:
    """Return the current worker's EventBus instance."""
    assert bus is not None, "EventBus not started — lifespan has not run yet"
    return bus


# ---------------------------------------------------------------------------
# Consumer group handlers — plain async functions registered in lifespan
# ---------------------------------------------------------------------------


async def process_order(event: Event) -> None:
    """Simulate order processing and emit downstream events."""
    _bus = _get_bus()
    order = event.data
    await asyncio.sleep(0.2)  # simulate work

    # Confirm the order
    await _bus.publish(
        "orders.confirmed",
        {"order_id": order["order_id"], "status": "confirmed"},
        source="order-processor",
        causation_id=event.event_id,
    )

    # Reserve inventory
    await _bus.publish(
        "inventory.reserved",
        {"order_id": order["order_id"], "items": order.get("items", [])},
        source="order-processor",
        causation_id=event.event_id,
    )


async def send_notification(event: Event) -> None:
    """Simulate sending a notification when an order is confirmed."""
    _bus = _get_bus()
    await asyncio.sleep(0.1)
    await _bus.publish(
        "notifications.sent",
        {
            "order_id": event.data["order_id"],
            "channel": "email",
            "message": f"Order {event.data['order_id']} has been confirmed!",
        },
        source="notification-service",
        causation_id=event.event_id,
    )


# ---------------------------------------------------------------------------
# App lifecycle — transport + bus created HERE, per-worker (post-fork safe)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy libraries
for name in ("httpx", "httpcore", "google", "pymongo", "litellm"):
    logging.getLogger(name).setLevel(logging.WARNING)
# Event bus: INFO shows start/stop/errors, DEBUG shows every Redis command
logging.getLogger("event_bus").setLevel(logging.INFO)

logger = logging.getLogger("example_agent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global bus
    redis_url = os.environ.get("FORGE_REDIS_URL", "redis://127.0.0.1:16389/0")
    transport = RedisTransport(RedisConfig(url=redis_url))
    bus = EventBus(transport, EventBusConfig())

    # Register consumer groups — each worker registers with a unique
    # consumer_name (pid + uuid), but the same group_name.  Redis
    # distributes messages across all consumers in the group.
    bus.create_consumer_group(
        topics=["orders.*"],
        group_name="order-processor",
        handler=process_order,
        config=ConsumerGroupConfig(concurrency=1),
    )
    bus.create_consumer_group(
        topics=["orders.confirmed"],
        group_name="notification-service",
        handler=send_notification,
        config=ConsumerGroupConfig(concurrency=1),
    )

    await bus.start()

    # -- Graceful SSE shutdown ------------------------------------------------
    # Problem: Uvicorn waits for all HTTP responses to finish before calling
    # lifespan shutdown.  SSE responses are infinite streams, so they never
    # finish → deadlock.
    #
    # Solution: Wrap Uvicorn's signal handler so we set a shutdown event
    # FIRST (causing SSE generators to exit), then forward the signal to
    # Uvicorn so it proceeds with normal shutdown.  By the time Uvicorn
    # waits for connections, the SSE streams have already closed.
    #
    # We use signal.signal() to capture the asyncio-installed handler
    # and chain onto it.  When we restore and re-deliver the signal,
    # asyncio's wakeup-fd mechanism fires Uvicorn's callback normally.
    _prev_sigint = _signal.getsignal(_signal.SIGINT)
    _prev_sigterm = _signal.getsignal(_signal.SIGTERM)

    def _on_signal(signum: int, frame: object) -> None:
        _shutdown_event.set()
        # Restore asyncio's handlers and re-deliver the signal so
        # Uvicorn's shutdown proceeds via its normal wakeup-fd path.
        _signal.signal(_signal.SIGINT, _prev_sigint)
        _signal.signal(_signal.SIGTERM, _prev_sigterm)
        os.kill(os.getpid(), signum)

    _signal.signal(_signal.SIGINT, _on_signal)
    _signal.signal(_signal.SIGTERM, _on_signal)

    yield

    # Restore original handlers in case lifespan exits without signal
    _signal.signal(_signal.SIGINT, _prev_sigint)
    _signal.signal(_signal.SIGTERM, _prev_sigterm)
    await bus.stop()
    bus = None


app = FastAPI(title="Event Bus Demo", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Serve the HTML client
# ---------------------------------------------------------------------------


_INDEX_HTML = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(await asyncio.to_thread(_INDEX_HTML.read_text))


# ---------------------------------------------------------------------------
# REST endpoints — publish events
# ---------------------------------------------------------------------------


def _random_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


@app.post("/api/orders")
async def create_order(body: dict | None = None) -> dict:
    """Create a new order. Publishes orders.created."""
    _bus = _get_bus()
    order_id = f"ord-{_random_id()}"
    order = {
        "order_id": order_id,
        "customer": body.get("customer", "demo-user") if body else "demo-user",
        "items": body.get("items", [{"sku": "WIDGET-A", "qty": 1}])
        if body
        else [{"sku": "WIDGET-A", "qty": 1}],
        "total": body.get("total", round(random.uniform(10, 500), 2))
        if body
        else round(random.uniform(10, 500), 2),
    }
    stream_id = await _bus.publish("orders.created", order, source="api")
    return {"status": "accepted", "order_id": order_id, "stream_id": stream_id}


@app.post("/api/events")
async def publish_custom_event(body: dict) -> dict:
    """Publish any custom event. Body: {"topic": "...", "data": {...}}"""
    _bus = _get_bus()
    topic = body.get("topic", "custom.event")
    data = body.get("data", {})
    try:
        stream_id = await _bus.publish(topic, data, source="api")
    except InvalidTopicError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"status": "published", "topic": topic, "stream_id": stream_id}


# ---------------------------------------------------------------------------
# SSE endpoint — browser subscribes here
# ---------------------------------------------------------------------------


@app.get("/events/sse")
async def sse_stream(request: Request, topics: str = "**") -> StreamingResponse:
    """
    SSE endpoint. Connect from browser:
        GET /events/sse?topics=orders.**,notifications.**
    """
    _bus = _get_bus()
    topic_list = [t.strip() for t in topics.split(",")]
    try:
        subscriber = SSESubscriber(
            patterns=topic_list,
            heartbeat_interval=15.0,
        )
    except InvalidTopicError as e:  # the patterns come from the client
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    _bus.add_subscriber(subscriber)
    await subscriber.start()

    async def generate() -> AsyncIterator[str]:
        try:
            async for chunk in subscriber.events(cancel=_shutdown_event):
                yield chunk
        finally:
            await subscriber.stop()
            if bus is not None:
                bus.remove_subscriber(subscriber)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# WebSocket endpoint — browser subscribes here
# ---------------------------------------------------------------------------


@app.websocket("/events/ws")
async def ws_stream(websocket: WebSocket) -> None:
    """
    WebSocket endpoint. Client sends:
        {"action": "subscribe", "topics": ["orders.**"]}
    """
    _bus = _get_bus()
    await websocket.accept()

    try:
        init = await websocket.receive_json()
        topics = init.get("topics", ["**"])
    except Exception:
        topics = ["**"]

    try:
        subscriber = WebSocketSubscriber(
            patterns=topics,
            ws=websocket,
        )
    except InvalidTopicError as e:  # the patterns come from the client
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(e)[:120])
        return
    _bus.add_subscriber(subscriber)
    await subscriber.start()

    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("action") == "update_topics":
                try:
                    replacement = WebSocketSubscriber(
                        patterns=msg.get("topics", ["**"]),
                        ws=websocket,
                    )
                except InvalidTopicError as e:
                    # Keep the current subscription and tell the client why.
                    await websocket.send_json({"type": "error", "error": str(e)})
                    continue
                await subscriber.stop()
                _bus.remove_subscriber(subscriber)
                subscriber = replacement
                _bus.add_subscriber(subscriber)
                await subscriber.start()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _bus.remove_subscriber(subscriber)
        await subscriber.stop()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return await _get_bus().health_check()
