from __future__ import annotations

from event_bus.testing.fake_transport import InMemoryTransport

try:
    import pytest

    @pytest.fixture
    def in_memory_transport() -> InMemoryTransport:
        """Provides a fresh InMemoryTransport for each test."""
        return InMemoryTransport()

except ImportError:
    pass
