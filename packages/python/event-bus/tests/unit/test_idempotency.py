"""Tests for event_bus/resilience/idempotency.py — IdempotencyFilter."""

import pytest

from event_bus.resilience.idempotency import IdempotencyFilter, InMemoryIdempotencyStore
from tests.helpers import FakeRedisClient


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def idempotency(client: FakeRedisClient) -> IdempotencyFilter:
    return IdempotencyFilter(client=client, key_prefix="test", ttl_seconds=3600)  # type: ignore[arg-type]


class TestCheckAndMark:
    async def test_new_event(self, idempotency: IdempotencyFilter) -> None:
        """First call returns True (new event)."""
        result = await idempotency.check_and_mark("event-1")
        assert result is True

    async def test_duplicate(self, idempotency: IdempotencyFilter) -> None:
        """Second call returns False (duplicate)."""
        await idempotency.check_and_mark("event-1")
        result = await idempotency.check_and_mark("event-1")
        assert result is False


class TestIsDuplicate:
    async def test_new(self, idempotency: IdempotencyFilter) -> None:
        """Returns False for unseen event."""
        result = await idempotency.is_duplicate("event-new")
        assert result is False

    async def test_after_mark(self, idempotency: IdempotencyFilter) -> None:
        """Returns True after marking."""
        await idempotency.check_and_mark("event-1")
        result = await idempotency.is_duplicate("event-1")
        assert result is True


class TestTwoPhase:
    async def test_a_new_event_is_claimed_then_done(self, idempotency: IdempotencyFilter) -> None:
        status, token = await idempotency.begin("g", "evt-1")
        assert status == "new" and token
        assert await idempotency.begin("g", "evt-1") == ("busy", None)
        await idempotency.complete("g", "evt-1", token)
        assert await idempotency.begin("g", "evt-1") == ("done", None)

    async def test_a_failed_claim_is_released_for_the_retry(
        self, idempotency: IdempotencyFilter
    ) -> None:
        _, token = await idempotency.begin("g", "evt-1")
        assert token is not None
        await idempotency.release("g", "evt-1", token)
        status, _ = await idempotency.begin("g", "evt-1")
        assert status == "new"

    async def test_release_only_drops_the_callers_own_claim(
        self, idempotency: IdempotencyFilter
    ) -> None:
        _, token = await idempotency.begin("g", "evt-1")
        await idempotency.release("g", "evt-1", "someone-elses-token")
        assert await idempotency.begin("g", "evt-1") == ("busy", None)
        assert token is not None

    async def test_groups_are_independent(self, idempotency: IdempotencyFilter) -> None:
        _, token = await idempotency.begin("orders", "evt-1")
        assert token is not None
        await idempotency.complete("orders", "evt-1", token)
        status, _ = await idempotency.begin("notifications", "evt-1")
        assert status == "new"


class TestInMemoryStore:
    async def test_same_semantics_in_memory(self) -> None:
        store = InMemoryIdempotencyStore(ttl_seconds=60, lease_seconds=60)
        status, token = await store.begin("g", "e")
        assert status == "new" and token
        assert await store.begin("g", "e") == ("busy", None)
        await store.release("g", "e", token)
        status, token = await store.begin("g", "e")
        assert status == "new" and token
        await store.complete("g", "e", token)
        assert await store.begin("g", "e") == ("done", None)
        assert (await store.begin("other", "e"))[0] == "new"
