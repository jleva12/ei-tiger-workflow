import time

import pytest

from event_bus.core.errors import CircuitOpenError
from event_bus.resilience.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "closed"

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"

    def test_stays_closed_below_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"

    def test_ensure_closed_raises_when_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        with pytest.raises(CircuitOpenError):
            cb.ensure_closed()

    def test_transitions_to_half_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half_open"

    def test_half_open_closes_after_successes(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, half_open_max_calls=2)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == "half_open"
        cb.record_success()
        cb.record_success()
        assert cb.state == "closed"

    def test_half_open_reopens_on_failure(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == "half_open"
        cb.record_failure()
        assert cb.state == "open"

    def test_manual_reset(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"


class TestHalfOpenProbes:
    def _half_open(self, max_calls: int = 3) -> CircuitBreaker:
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=max_calls
        )
        cb.on_failure()
        time.sleep(0.06)
        assert cb.state == "half_open"
        return cb

    def test_half_open_admits_only_its_probe_slots(self) -> None:
        cb = self._half_open(max_calls=3)
        probes = [cb.acquire() for _ in range(3)]
        assert probes == [True, True, True]
        with pytest.raises(CircuitOpenError):
            cb.acquire()
        cb.on_success(probe=True)
        assert cb.acquire() is True  # a slot freed up

    def test_probes_close_the_circuit_after_enough_successes(self) -> None:
        cb = self._half_open(max_calls=2)
        for _ in range(2):
            cb.on_success(probe=cb.acquire())
        assert cb.state == "closed"

    def test_ignored_outcomes_free_the_slot_without_counting(self) -> None:
        cb = self._half_open(max_calls=1)
        probe = cb.acquire()
        cb.on_ignored(probe)
        assert cb.state == "half_open"
        assert cb.acquire() is True

    def test_closed_calls_arent_probes(self) -> None:
        cb = CircuitBreaker()
        assert cb.acquire() is False
        cb.on_failure()
        cb.on_success()
        assert cb.state == "closed"
