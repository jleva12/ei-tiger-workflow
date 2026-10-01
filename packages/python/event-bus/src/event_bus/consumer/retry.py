from __future__ import annotations

import random
from typing import Protocol


class RetryPolicy(Protocol):
    """Calculates the delay before the next retry attempt."""

    def delay(self, attempt: int) -> float:
        """Return delay in seconds for the given attempt number (0-based)."""
        ...


class ExponentialBackoff:
    """Exponential backoff with jitter."""

    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        multiplier: float = 2.0,
        jitter: float = 0.1,
    ) -> None:
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter = jitter

    def delay(self, attempt: int) -> float:
        delay = min(self.base_delay * (self.multiplier**attempt), self.max_delay)
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)
        return max(0, delay)


class FixedDelay:
    """Fixed delay between retries."""

    def __init__(self, delay: float = 5.0) -> None:
        self._delay = delay

    def delay(self, attempt: int) -> float:
        return self._delay
