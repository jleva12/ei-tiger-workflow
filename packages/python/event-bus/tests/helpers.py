"""Shared test utilities for the event bus test suite."""

from __future__ import annotations

from event_bus.core.event import Event, EventEnvelope, EventMetadata


def make_envelope(
    topic: str = "orders.created",
    data: dict | None = None,
    event_id: str = "evt-1",
    retry_count: int = 0,
    stream_id: str = "1-0",
) -> EventEnvelope:
    """Factory for test EventEnvelopes."""
    event = Event(
        topic=topic,
        data=data or {"order_id": "42"},
        metadata=EventMetadata(event_id=event_id, retry_count=retry_count),
    )
    return EventEnvelope(event=event, stream_id=stream_id, stream_name=topic)


class FakeMetrics:
    """Records all metrics calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float, dict[str, str] | None]] = []

    def increment(self, name: str, value: float = 1.0, tags: dict[str, str] | None = None) -> None:
        self.calls.append(("increment", name, value, tags))

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.calls.append(("gauge", name, value, tags))

    def histogram(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.calls.append(("histogram", name, value, tags))

    def timing(self, name: str, value_ms: float, tags: dict[str, str] | None = None) -> None:
        self.calls.append(("timing", name, value_ms, tags))

    def find(self, method: str, name: str) -> list[tuple[str, str, float, dict[str, str] | None]]:
        """Find all calls matching method and name."""
        return [(m, n, v, t) for m, n, v, t in self.calls if m == method and n == name]


class FakeRedisClient:
    """Minimal dict-based mock for IdempotencyFilter tests."""

    def __init__(self) -> None:
        self._store: dict[str, str | bytes] = {}

    async def set(
        self,
        key: str,
        value: str | bytes,
        *,
        nx: bool = False,
        ex: int | None = None,  # noqa: ARG002
    ) -> bool | None:
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def get(self, key: str) -> bytes | None:
        value = self._store.get(key)
        if value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode()

    async def eval(self, script: str, numkeys: int, key: str, expected: bytes) -> int:
        """The release script: delete the key only if it holds ``expected``."""
        if await self.get(key) == expected:
            del self._store[key]
            return 1
        return 0
