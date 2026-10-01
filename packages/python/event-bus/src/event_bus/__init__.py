"""
Production-grade, framework-agnostic event bus backed by Redis.

Quick start::

    from event_bus import EventBus, Event, EventMetadata
    from event_bus.transport.redis.transport import RedisTransport
    from event_bus.config import RedisConfig

    transport = RedisTransport(RedisConfig(url="redis://localhost:6379/0"))
    bus = EventBus(transport)

    async with bus:
        await bus.publish("orders.created", {"order_id": "123"})
"""

from event_bus.bus import EventBus
from event_bus.config import EventBusConfig, RedisConfig
from event_bus.core.errors import (
    BackpressureError,
    CircuitOpenError,
    ConsumerError,
    DuplicateEventError,
    EventBusConnectionError,
    EventBusError,
    InvalidTopicError,
    PublishError,
    SerializationError,
)

# Deprecated alias of EventBusConnectionError: still importable as
# ``from event_bus import ConnectionError`` but deliberately left out of
# __all__, so ``from event_bus import *`` does not shadow the builtin.
from event_bus.core.errors import ConnectionError as ConnectionError  # noqa: A004
from event_bus.core.event import Event, EventEnvelope, EventMetadata
from event_bus.core.serialization import JsonSerializer, MsgpackSerializer
from event_bus.core.topic import TopicPattern, validate_pattern, validate_topic

__all__ = [
    "EventBus",
    "EventBusConfig",
    "RedisConfig",
    "Event",
    "EventEnvelope",
    "EventMetadata",
    "TopicPattern",
    "validate_topic",
    "validate_pattern",
    "JsonSerializer",
    "MsgpackSerializer",
    "EventBusError",
    "PublishError",
    "EventBusConnectionError",
    "InvalidTopicError",
    "ConsumerError",
    "CircuitOpenError",
    "BackpressureError",
    "DuplicateEventError",
    "SerializationError",
]
