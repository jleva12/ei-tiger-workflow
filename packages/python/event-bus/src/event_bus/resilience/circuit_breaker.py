from __future__ import annotations

import logging
import time
from typing import Literal

from event_bus.core.errors import CircuitOpenError

logger = logging.getLogger(__name__)

State = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """
    Three-state circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

    When *failure_threshold* consecutive failures occur, the circuit opens.
    After *recovery_timeout* seconds it becomes half-open: at most
    *half_open_max_calls* probe calls may be in flight at once, and once that
    many probes have succeeded the circuit closes; any probe failure re-opens
    it.

    Callers take a slot with :meth:`acquire` and settle it with
    :meth:`on_success`, :meth:`on_failure` or :meth:`on_ignored` (the call
    failed for a reason that says nothing about the dependency's health, such
    as bad input). All state changes happen without awaiting, so they're
    atomic under asyncio.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
    ) -> None:
        if failure_threshold < 1 or half_open_max_calls < 1:
            raise ValueError("failure_threshold and half_open_max_calls must be at least 1")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state: State = "closed"
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        # Half-open: probes admitted and not yet settled, and probes succeeded.
        self._half_open_in_flight: int = 0
        self._half_open_calls: int = 0

    def _maybe_transition_to_half_open(self) -> None:
        """Check if the recovery timeout has elapsed and transition if so."""
        if (
            self._state == "open"
            and time.monotonic() - self._last_failure_time >= self._recovery_timeout
        ):
            logger.debug(
                "Circuit breaker transitioning open -> half_open (recovery_timeout=%.1fs elapsed)",
                self._recovery_timeout,
            )
            self._state = "half_open"
            self._half_open_in_flight = 0
            self._half_open_calls = 0

    @property
    def state(self) -> State:
        self._maybe_transition_to_half_open()
        return self._state

    def _refuse(self) -> CircuitOpenError:
        if self._state == "half_open":
            return CircuitOpenError(
                f"Circuit breaker is half-open with {self._half_open_in_flight} probe "
                "call(s) in flight"
            )
        remaining = max(0.0, self._recovery_timeout - (time.monotonic() - self._last_failure_time))
        logger.debug("Circuit breaker is open, rejecting call (recovery in %.1fs)", remaining)
        return CircuitOpenError(f"Circuit breaker is open. Recovery in {remaining:.1f}s")

    def acquire(self) -> bool:
        """
        Admit a call, or raise :class:`CircuitOpenError`.

        :return: True when the call is a half-open probe (it holds one of the
            probe slots until settled).
        """
        self._maybe_transition_to_half_open()
        if self._state == "open":
            raise self._refuse()
        if self._state == "half_open":
            if self._half_open_in_flight >= self._half_open_max_calls:
                raise self._refuse()
            self._half_open_in_flight += 1
            return True
        return False

    def on_success(self, probe: bool = False) -> None:
        if probe:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        if self._state == "half_open":
            if not probe:
                return  # admitted before the circuit opened: not a probe
            self._half_open_calls += 1
            logger.debug(
                "Circuit breaker half_open probe succeeded (%d/%d)",
                self._half_open_calls,
                self._half_open_max_calls,
            )
            if self._half_open_calls >= self._half_open_max_calls:
                logger.info("Circuit breaker closing (probes succeeded)")
                self._state = "closed"
                self._failure_count = 0
                self._half_open_calls = 0
        elif self._state == "closed":
            self._failure_count = 0

    def on_failure(self, probe: bool = False) -> None:
        if probe:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._state == "half_open":
            logger.warning("Circuit breaker re-opening (probe failed)")
            self._state = "open"
        elif self._state == "closed" and self._failure_count >= self._failure_threshold:
            logger.warning(
                "Circuit breaker opening (%d consecutive failures)",
                self._failure_count,
            )
            self._state = "open"
        else:
            logger.debug(
                "Circuit breaker recorded failure (%d/%d)",
                self._failure_count,
                self._failure_threshold,
            )

    def on_ignored(self, probe: bool = False) -> None:
        """The call ended without saying anything about the dependency's
        health: free its probe slot without counting it."""
        if probe:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)

    # -- Earlier API, kept for callers that don't track probe slots --

    def ensure_closed(self) -> None:
        """Raise :class:`CircuitOpenError` if the circuit is open, or half-open
        with every probe slot taken. Doesn't take a slot."""
        self._maybe_transition_to_half_open()
        if self._state == "open" or (
            self._state == "half_open" and self._half_open_in_flight >= self._half_open_max_calls
        ):
            raise self._refuse()

    def record_success(self) -> None:
        """A successful call; in half-open it counts as a probe."""
        self.on_success(probe=self._state == "half_open")

    def record_failure(self) -> None:
        self.on_failure()

    def reset(self) -> None:
        """Manually reset the circuit breaker to closed."""
        logger.info("Circuit breaker manually reset to closed")
        self._state = "closed"
        self._failure_count = 0
        self._half_open_in_flight = 0
        self._half_open_calls = 0
