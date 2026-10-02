"""The async worker's queue client, without Redis: fake queues stand in."""

import asyncio
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from forge_admin.adk_workflows.queue import (
    ADK_WORKFLOWS,
    RUN_ADK,
    Embedding,
    EmbeddingError,
)
from forge_admin.config import Settings


class FakeQueue:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.closed = False

    async def enqueue(self, function: str, **options: Any) -> None:
        if self.fail:
            raise self.fail
        self.sent.append((function, options))

    async def disconnect(self) -> None:
        self.closed = True
        if self.fail:
            raise self.fail


def test_a_run_is_taken_by_a_job_on_its_queue() -> None:
    adk_workflows = FakeQueue()
    client = Embedding({ADK_WORKFLOWS: adk_workflows})
    run_id = "0123456789abcdef0123456789abcdef"
    asyncio.run(client.run_adk(run_id))
    asyncio.run(client.run_adk(run_id))
    [(function, first), (_, second)] = adk_workflows.sent
    assert function == RUN_ADK == "run_adk"
    assert set(first) == {"key", "run_id"}
    assert first["run_id"] == run_id
    # A key of its own each time: a duplicate job is harmless (the worker
    # claims the run), a lost one isn't.
    assert first["key"].startswith(f"adk-run:{run_id}:")
    assert len(first["key"]) == len(f"adk-run:{run_id}:") + 8
    assert first["key"] != second["key"]


def test_an_unreachable_redis_is_an_embedding_error() -> None:
    down = FakeQueue(fail=RedisConnectionError("Connection refused"))
    client = Embedding({ADK_WORKFLOWS: down})
    with pytest.raises(EmbeddingError, match="Connection refused"):
        asyncio.run(client.run_adk("0123456789abcdef0123456789abcdef"))
    asyncio.run(client.aclose())  # closing still works
    assert down.closed


def test_set_up_only_with_the_settings(settings: Settings) -> None:
    assert Embedding.from_settings(settings) is None
    configured = settings.model_copy(
        update={"embedding_redis_url": "redis://127.0.0.1:16379/0"}
    )
    client = Embedding.from_settings(configured)
    assert client is not None
    # The ADK workflows queue.
    assert set(client._queues) == {ADK_WORKFLOWS}
