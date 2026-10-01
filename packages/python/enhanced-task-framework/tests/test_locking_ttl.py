"""InMemoryLockProvider TTL semantics (audit T12): expired leases are free to
acquire and lost to refresh — matching the distributed providers' contract."""

from __future__ import annotations

from datetime import timedelta

from etf.locking import InMemoryLockProvider


def make_provider():
    now = [0.0]
    provider = InMemoryLockProvider(time_source=lambda: now[0])
    return provider, now


async def test_expired_lease_is_stealable_and_lost_to_refresh():
    provider, now = make_provider()
    first = await provider.acquire("inst", "worker-1", timedelta(seconds=10))
    assert first is not None
    assert await provider.acquire("inst", "worker-2", timedelta(seconds=10)) is None

    now[0] += 11  # lease expires (the holder crashed / stopped heartbeating)

    second = await provider.acquire("inst", "worker-2", timedelta(seconds=10))
    assert second is not None                                      # stolen
    assert first.fencing_token == 1
    assert second.fencing_token == 2
    assert await first.refresh(timedelta(seconds=10)) is False     # loser knows
    assert await second.refresh(timedelta(seconds=10)) is True


async def test_refresh_extends_the_lease():
    provider, now = make_provider()
    lock = await provider.acquire("inst", "worker-1", timedelta(seconds=10))
    now[0] += 8
    assert await lock.refresh(timedelta(seconds=10)) is True       # extends to t=18
    now[0] += 8                                                    # t=16 < 18: still held
    assert await provider.acquire("inst", "worker-2", timedelta(seconds=10)) is None
    now[0] += 3                                                    # t=19 > 18: expired
    assert await provider.acquire("inst", "worker-2", timedelta(seconds=10)) is not None


async def test_stale_holder_release_does_not_clobber_new_owner():
    provider, now = make_provider()
    first = await provider.acquire("inst", "worker-1", timedelta(seconds=10))
    now[0] += 11
    second = await provider.acquire("inst", "worker-2", timedelta(seconds=10))
    assert second is not None

    await first.release()  # too late — the lease belongs to worker-2 now

    assert await provider.acquire("inst", "worker-3", timedelta(seconds=10)) is None
    assert await second.refresh(timedelta(seconds=10)) is True
    await second.release()  # idempotent, releases its own lease
    await second.release()
    assert await provider.acquire("inst", "worker-3", timedelta(seconds=10)) is not None
