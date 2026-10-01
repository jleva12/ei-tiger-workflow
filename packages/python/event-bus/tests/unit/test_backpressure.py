import asyncio

import pytest

from event_bus.core.errors import BackpressureError
from event_bus.resilience.backpressure import BackpressureController


@pytest.mark.asyncio
async def test_allows_within_limit() -> None:
    bp = BackpressureController(max_pending=5)
    async with bp:
        assert bp.available == 4
    assert bp.available == 5


@pytest.mark.asyncio
async def test_raises_when_full() -> None:
    bp = BackpressureController(max_pending=1, timeout=0.1)

    async with bp:
        # Second acquire should timeout
        with pytest.raises(BackpressureError):
            async with bp:
                pass


@pytest.mark.asyncio
async def test_concurrent_access() -> None:
    bp = BackpressureController(max_pending=3)
    entered = 0

    async def acquire() -> None:
        nonlocal entered
        async with bp:
            entered += 1
            await asyncio.sleep(0.05)

    tasks = [asyncio.create_task(acquire()) for _ in range(3)]
    await asyncio.gather(*tasks)
    assert entered == 3
