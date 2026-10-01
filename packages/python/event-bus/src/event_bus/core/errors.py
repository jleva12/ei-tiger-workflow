from __future__ import annotations


class EventBusError(Exception):
    """Base exception for all event bus errors."""


class EventBusConnectionError(EventBusError):
    """Cannot connect to Redis."""


# Deprecated alias kept so ``from event_bus.core.errors import ConnectionError``
# keeps working. It shadows the builtin ``ConnectionError`` wherever it is
# imported, so new code should use ``EventBusConnectionError``. It is not part
# of ``event_bus.__all__``, so ``from event_bus import *`` no longer shadows it.
ConnectionError = EventBusConnectionError  # noqa: A001


class PublishError(EventBusError):
    """Failed to publish an event."""


class SerializationError(EventBusError):
    """Failed to serialize or deserialize event data."""


class ConsumerError(EventBusError):
    """Error in consumer group processing."""


class CircuitOpenError(EventBusError):
    """Circuit breaker is open -- calls are being rejected."""


class BackpressureError(EventBusError):
    """System under backpressure -- caller should slow down."""


class DuplicateEventError(EventBusError):
    """Event with this ID has already been processed (idempotency)."""


class InvalidTopicError(EventBusError, ValueError):
    """A topic or topic pattern is malformed or exceeds the configured limits.

    Also a :class:`ValueError`, so web handlers that map ``ValueError`` to
    ``400 Bad Request`` handle client-supplied patterns without extra code.
    """
