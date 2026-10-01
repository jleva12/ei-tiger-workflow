from __future__ import annotations

import asyncio
import logging

from event_bus.core.errors import BackpressureError

logger = logging.getLogger(__name__)


class BackpressureController:
    """
    Limits concurrent in-flight publishes via a semaphore.
    Prevents overwhelming Redis when downstream is slow.

    Usage::

        async with backpressure:
              await transport.publish(event)
    """

    def __init__(
        self,
        max_pending: int = 1000,
        timeout: float = 10.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_pending)
        self._timeout = timeout
        self._max_pending = max_pending
        logger.debug(
            "BackpressureController created (max_pending=%d, timeout=%.1fs)",
            max_pending,
            timeout,
        )

    async def __aenter__(self) -> None:
        logger.debug(
            "Backpressure: acquiring semaphore (available=%d/%d)",
            self.available,
            self._max_pending,
        )
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
        except TimeoutError:
            logger.warning(
                "Backpressure: timed out after %.1fs (%d publishes in flight)",
                self._timeout,
                self._max_pending,
            )
            raise BackpressureError(
                f"Backpressure: {self._max_pending} publishes in flight, "
                f"timed out after {self._timeout}s"
            ) from None
        logger.debug(
            "Backpressure: semaphore acquired (available=%d/%d)",
            self.available,
            self._max_pending,
        )

    async def __aexit__(self, *args: object) -> None:
        self._semaphore.release()
        logger.debug(
            "Backpressure: semaphore released (available=%d/%d)",
            self.available,
            self._max_pending,
        )

    @property
    def available(self) -> int:
        """Number of permits currently available."""
        return self._semaphore._value
