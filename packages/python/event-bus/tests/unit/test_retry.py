"""Tests for event_bus/consumer/retry.py — retry policies."""

from event_bus.consumer.retry import ExponentialBackoff, FixedDelay


class TestExponentialBackoff:
    def test_growth(self) -> None:
        """Delays grow as base * mult^attempt: 1, 2, 4, 8..."""
        eb = ExponentialBackoff(base_delay=1.0, multiplier=2.0, jitter=0.0)
        assert eb.delay(0) == 1.0
        assert eb.delay(1) == 2.0
        assert eb.delay(2) == 4.0
        assert eb.delay(3) == 8.0

    def test_max_cap(self) -> None:
        """Delay never exceeds max_delay."""
        eb = ExponentialBackoff(base_delay=1.0, max_delay=5.0, multiplier=2.0, jitter=0.0)
        assert eb.delay(10) == 5.0
        assert eb.delay(100) == 5.0

    def test_jitter_range(self) -> None:
        """Jitter stays within [-jitter*delay, +jitter*delay]."""
        eb = ExponentialBackoff(base_delay=10.0, multiplier=1.0, jitter=0.1)
        for _ in range(100):
            d = eb.delay(0)
            assert 9.0 <= d <= 11.0  # 10 +/- 10*0.1

    def test_zero_jitter(self) -> None:
        """jitter=0 produces exact deterministic delays."""
        eb = ExponentialBackoff(base_delay=3.0, multiplier=2.0, jitter=0.0)
        assert eb.delay(0) == 3.0
        assert eb.delay(1) == 6.0
        assert eb.delay(2) == 12.0

    def test_custom_params(self) -> None:
        """Non-default base, multiplier, max work correctly."""
        eb = ExponentialBackoff(base_delay=0.5, max_delay=10.0, multiplier=3.0, jitter=0.0)
        assert eb.delay(0) == 0.5
        assert eb.delay(1) == 1.5
        assert eb.delay(2) == 4.5
        assert eb.delay(3) == 10.0  # capped

    def test_delay_nonnegative(self) -> None:
        """Delay is always >= 0 even with large jitter."""
        eb = ExponentialBackoff(base_delay=0.01, multiplier=1.0, jitter=1.0)
        for _ in range(200):
            assert eb.delay(0) >= 0


class TestFixedDelay:
    def test_constant(self) -> None:
        """Always returns same delay regardless of attempt."""
        fd = FixedDelay(delay=5.0)
        assert fd.delay(0) == 5.0
        assert fd.delay(1) == 5.0
        assert fd.delay(99) == 5.0

    def test_custom_value(self) -> None:
        """Custom delay value works."""
        fd = FixedDelay(delay=0.1)
        assert fd.delay(0) == 0.1
        assert fd.delay(10) == 0.1
