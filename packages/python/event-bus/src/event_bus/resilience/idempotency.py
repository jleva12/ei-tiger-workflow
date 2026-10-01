from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, cast

from event_bus.interfaces.transport import IdempotencyStatus

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_DONE = b"done"
_PROCESSING = b"processing:"

# Delete the key only if it still holds this consumer's processing token.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class IdempotencyFilter:
    """
    Records which events each consumer group has processed, in Redis, so an
    event (by ID) is handled once per group even when it's published twice.

    Processing is two-phase: :meth:`begin` claims the event with a lease
    (``SET NX EX lease``), :meth:`complete` marks it done for the TTL, and
    :meth:`release` gives an unfinished claim up (only if it's still this
    caller's) so a retry can process it. Keys are scoped by group: one
    group's success never makes another group skip the event.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        key_prefix: str = "eventbus",
        ttl_seconds: int = 86400,
        lease_seconds: int = 300,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds
        self._lease = lease_seconds
        logger.debug(
            "IdempotencyFilter created (prefix=%s, ttl=%ds, lease=%ds)",
            key_prefix,
            ttl_seconds,
            lease_seconds,
        )

    def _key(self, group: str, event_id: str) -> str:
        return f"{self._key_prefix}:idem:{group}:{event_id}"

    async def begin(self, group: str, event_id: str) -> tuple[IdempotencyStatus, str | None]:
        key = self._key(group, event_id)
        token = uuid.uuid4().hex
        claimed = await self._client.set(key, _PROCESSING + token.encode(), nx=True, ex=self._lease)
        if claimed:
            return "new", token
        current = await self._client.get(key)
        if current == _DONE:
            return "done", None
        if current is None:
            # Released or expired between the two calls: try once more.
            claimed = await self._client.set(
                key, _PROCESSING + token.encode(), nx=True, ex=self._lease
            )
            return ("new", token) if claimed else ("busy", None)
        return "busy", None

    async def complete(self, group: str, event_id: str, token: str) -> None:
        await self._client.set(self._key(group, event_id), _DONE, ex=self._ttl)

    async def release(self, group: str, event_id: str, token: str) -> None:
        key = self._key(group, event_id)
        await cast(
            Awaitable[Any],
            self._client.eval(_RELEASE_SCRIPT, 1, key, _PROCESSING + token.encode()),
        )

    # -- Earlier API: one mark per event, before processing --

    async def check_and_mark(self, event_id: str) -> bool:
        """
        Deprecated: marks the event before it's processed, so a failed
        handler's retry is skipped. Use begin/complete/release.
        Returns True if the event is new (and now marked).
        """
        key = f"{self._key_prefix}:idem:{event_id}"
        was_set = await self._client.set(key, "1", nx=True, ex=self._ttl)
        return was_set is not None and was_set is not False

    async def is_duplicate(self, event_id: str) -> bool:
        """Deprecated: whether ``check_and_mark`` has marked the event."""
        key = f"{self._key_prefix}:idem:{event_id}"
        count = await self._client.exists(key)
        return bool(count)


class InMemoryIdempotencyStore:
    """The same semantics as :class:`IdempotencyFilter`, in process memory
    (for tests and single-process use)."""

    def __init__(self, ttl_seconds: int = 86400, lease_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._lease = lease_seconds
        # key -> (value, expires_at monotonic)
        self._store: dict[str, tuple[str, float]] = {}

    def _get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires = entry
        if time.monotonic() >= expires:
            del self._store[key]
            return None
        return value

    async def begin(self, group: str, event_id: str) -> tuple[IdempotencyStatus, str | None]:
        key = f"{group}:{event_id}"
        current = self._get(key)
        if current == "done":
            return "done", None
        if current is not None:
            return "busy", None
        token = uuid.uuid4().hex
        self._store[key] = (f"processing:{token}", time.monotonic() + self._lease)
        return "new", token

    async def complete(self, group: str, event_id: str, token: str) -> None:
        self._store[f"{group}:{event_id}"] = ("done", time.monotonic() + self._ttl)

    async def release(self, group: str, event_id: str, token: str) -> None:
        key = f"{group}:{event_id}"
        if self._get(key) == f"processing:{token}":
            del self._store[key]
