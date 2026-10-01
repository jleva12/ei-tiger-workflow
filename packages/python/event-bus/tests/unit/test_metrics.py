"""Tests for event_bus/observability/metrics.py — NoOpMetrics."""

from event_bus.observability.metrics import NoOpMetrics


class TestNoOpMetrics:
    def test_increment(self) -> None:
        """NoOpMetrics.increment() does not raise."""
        m = NoOpMetrics()
        m.increment("events.published", value=1.0, tags={"topic": "test"})

    def test_gauge(self) -> None:
        """NoOpMetrics.gauge() does not raise."""
        m = NoOpMetrics()
        m.gauge("connections.active", value=5.0, tags={"pool": "cmd"})

    def test_histogram(self) -> None:
        """NoOpMetrics.histogram() does not raise."""
        m = NoOpMetrics()
        m.histogram("event.size", value=1024.0, tags={"topic": "test"})

    def test_timing(self) -> None:
        """NoOpMetrics.timing() does not raise."""
        m = NoOpMetrics()
        m.timing("handler.duration", value_ms=42.5, tags={"group": "test"})
