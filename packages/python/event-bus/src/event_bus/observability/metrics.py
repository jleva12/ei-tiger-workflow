from __future__ import annotations

from typing import Protocol


class MetricsCollector(Protocol):
    """Pluggable metrics backend."""

    def increment(
        self, name: str, value: float = 1.0, tags: dict[str, str] | None = None
    ) -> None: ...

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...

    def histogram(self, name: str, value: float, tags: dict[str, str] | None = None) -> None: ...

    def timing(self, name: str, value_ms: float, tags: dict[str, str] | None = None) -> None: ...


class NoOpMetrics:
    """Default no-op metrics collector."""

    def increment(self, name: str, value: float = 1.0, tags: dict[str, str] | None = None) -> None:
        pass

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        pass

    def histogram(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        pass

    def timing(self, name: str, value_ms: float, tags: dict[str, str] | None = None) -> None:
        pass
