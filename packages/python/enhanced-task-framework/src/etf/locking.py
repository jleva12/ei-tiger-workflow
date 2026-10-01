"""Renewable writer leases for coordinating :class:`~etf.model.JobInstance` execution.

``RunLockProvider`` is the contract; the framework ships an in-process reference
(``InMemoryLockProvider``) and the default Mongo TTL-based lock lives in the Beanie
store module. Locks are leased with a TTL so a crashed worker's lock auto-expires and
the instance becomes reclaimable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from datetime import timedelta


class Lock(ABC):
    """A held lease. Release it when done; refresh to extend a long-running hold."""

    @abstractmethod
    async def refresh(self, ttl: timedelta) -> bool:
        """Extend the lease. Returns False if the lease was lost (e.g. expired/stolen)."""

    @abstractmethod
    async def release(self) -> None:
        """Release the lease. Idempotent."""

    @property
    def fencing_token(self) -> int | None:
        """Monotonic acquisition token for fencing downstream side effects.

        Custom providers predating fencing may return ``None``. Distributed providers
        should return a token that strictly increases for every acquisition of a key.
        """
        return None

    async def __aenter__(self) -> "Lock":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()


class RunLockProvider(ABC):
    """Acquires writer leases keyed by instance id.

    Lease expiry may overlap stale and replacement work. Distributed implementations
    should issue monotonic fencing tokens, and unsafe downstream writes must enforce
    those tokens.
    """

    @abstractmethod
    async def acquire(self, key: str, owner: str, ttl: timedelta) -> Lock | None:
        """Acquire the lock for ``key`` on behalf of ``owner``.

        Returns a :class:`Lock` on success, or ``None`` if it is already held by someone
        else (the caller decides whether to back off or treat the instance as busy).
        """


class _InMemoryLock(Lock):
    def __init__(
        self,
        provider: "InMemoryLockProvider",
        key: str,
        owner: str,
        lease_id: str,
        fencing_token: int,
    ) -> None:
        self._provider = provider
        self._key = key
        self._owner = owner
        self._lease_id = lease_id
        self._fencing_token = fencing_token
        self._released = False

    @property
    def fencing_token(self) -> int:
        return self._fencing_token

    async def refresh(self, ttl: timedelta) -> bool:
        if self._released:
            return False
        return await self._provider._refresh(self._key, self._owner, self._lease_id, ttl)

    async def release(self) -> None:
        if not self._released:
            self._released = True
            await self._provider._release(self._key, self._owner, self._lease_id)


class InMemoryLockProvider(RunLockProvider):
    """Process-local lock provider. Suitable for single-process deployments and tests.

    Honors the TTL contract like the distributed providers: an expired lease is treated
    as free by :meth:`acquire` and as lost by ``refresh``, so a holder that stops
    heartbeating (crashed task, wedged coroutine) cannot hold an instance hostage for
    the process lifetime.

    For multi-worker deployments use ``MongoLockProvider`` (or an equivalent shared
    provider) so the lease is visible across processes.
    """

    def __init__(self, *, time_source=time.monotonic) -> None:
        # key -> (owner, lease_id, expires_at, fencing_token)
        self._leases: dict[str, tuple[str, str, float, int]] = {}
        self._fencing_counters: dict[str, int] = {}
        self._mutex = asyncio.Lock()
        self._now = time_source

    def _expired(self, key: str) -> bool:
        lease = self._leases.get(key)
        return lease is not None and lease[2] <= self._now()

    async def acquire(self, key: str, owner: str, ttl: timedelta) -> Lock | None:
        async with self._mutex:
            if key in self._leases and not self._expired(key):
                return None
            lease_id = uuid.uuid4().hex
            fencing_token = self._fencing_counters.get(key, 0) + 1
            self._fencing_counters[key] = fencing_token
            self._leases[key] = (
                owner,
                lease_id,
                self._now() + ttl.total_seconds(),
                fencing_token,
            )
            return _InMemoryLock(self, key, owner, lease_id, fencing_token)

    async def _refresh(self, key: str, owner: str, lease_id: str, ttl: timedelta) -> bool:
        async with self._mutex:
            lease = self._leases.get(key)
            if lease is None or lease[:2] != (owner, lease_id) or lease[2] <= self._now():
                return False
            self._leases[key] = (
                owner,
                lease_id,
                self._now() + ttl.total_seconds(),
                lease[3],
            )
            return True

    async def _release(self, key: str, owner: str, lease_id: str) -> None:
        async with self._mutex:
            lease = self._leases.get(key)
            if lease is not None and lease[:2] == (owner, lease_id):
                del self._leases[key]
