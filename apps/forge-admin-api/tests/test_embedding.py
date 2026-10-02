"""The async worker's queue client, without Redis: fake queues stand in."""

import asyncio
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from forge_admin.config import Settings
from forge_admin.embedding import ADK_WORKFLOWS, RESULT_TTL, Embedding, EmbeddingError


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


def test_an_adk_workflow_run_goes_to_its_queue() -> None:
    adk_workflows = FakeQueue()
    client = Embedding({ADK_WORKFLOWS: adk_workflows})
    asyncio.run(
        client.run_adk_workflow(
            tenant_id="org-1",
            key="adk_workflows.run:ag_abc123:1",
            labels={"adk_workflow": "ag_abc123", "adk_session": "s-1"},
            payload={"document": {}, "input": None, "session_id": "s-1"},
            requested_by={"id": "u1", "display_name": "Ada"},
        )
    )
    assert adk_workflows.sent == [
        (
            "run_job",
            {
                "key": "adk_workflows.run:ag_abc123:1",
                "ttl": RESULT_TTL,
                "spec": {
                    "task_type": "adk_workflows",
                    "kind": "run",
                    "tenant_id": "org-1",
                    "labels": {"adk_workflow": "ag_abc123", "adk_session": "s-1"},
                    "payload": {"document": {}, "input": None, "session_id": "s-1"},
                    "requested_by": {"id": "u1", "display_name": "Ada"},
                },
            },
        )
    ]


def test_a_run_nobody_requested_carries_no_requester() -> None:
    adk_workflows = FakeQueue()
    client = Embedding({ADK_WORKFLOWS: adk_workflows})
    asyncio.run(client.run_adk_workflow(tenant_id="t", key="k", labels={}, payload={}))
    [(_, options)] = adk_workflows.sent
    assert "requested_by" not in options["spec"]


def test_an_unreachable_redis_is_an_embedding_error() -> None:
    down = FakeQueue(fail=RedisConnectionError("Connection refused"))
    client = Embedding({ADK_WORKFLOWS: down})
    with pytest.raises(EmbeddingError, match="Connection refused"):
        asyncio.run(
            client.run_adk_workflow(tenant_id="t", key="k", labels={}, payload={})
        )
    asyncio.run(client.aclose())  # closing still works
    assert down.closed


def test_set_up_only_with_the_settings(settings: Settings) -> None:
    assert Embedding.from_settings(settings) is None
    configured = settings.model_copy(
        update={"embedding_redis_url": "redis://127.0.0.1:16379/0"}
    )
    client = Embedding.from_settings(configured)
    assert client is not None
    # A queue per task type.
    assert set(client._queues) == {ADK_WORKFLOWS}
