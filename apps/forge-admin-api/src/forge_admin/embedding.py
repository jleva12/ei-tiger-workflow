"""The async worker (apps/forge-async-worker): queueing its jobs on its SAQ
queues on Redis. The module keeps its first name, from when the worker only
embedded documents.

ADK workflow runs are kept in this API's database (``forge_task_adk_workflows.
run_store``); a job only names the run to take: ``run_adk`` with
``{"run_id"}``, on the ``adk_workflows`` queue. The worker claims the run
(a run is one worker's at a time), so a job that finds it taken, paused or
finished does nothing, and a duplicate is harmless. Each job's key is the
run's ID and a random suffix. The worker applies its own retries, timeout and
heartbeat to every job. This client only relays; the routes decide who may
queue what.
"""

import asyncio
import contextlib
from typing import Any, Self
from uuid import uuid4

from redis import asyncio as aioredis
from redis.exceptions import RedisError
from saq.queue.redis import RedisQueue

from forge_admin.config import Settings

# ADK workflows' runs (the ADK workflows task).
ADK_WORKFLOWS = "adk_workflows"
# The worker's job that takes a run and runs it, or carries it on.
RUN_ADK = "run_adk"
# Seconds a submission may take.
TIMEOUT = 5.0


class EmbeddingError(Exception):
    """The async worker's queue could not be reached."""


class Embedding:
    """
    Queues jobs for the async worker.

    :param queues: The worker's queues by name: ``adk_workflows``.
    """

    def __init__(self, queues: dict[str, Any]) -> None:
        self._queues = queues

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``embedding_redis_url``.
        :return: A client, or None when the async worker isn't set up.
        """
        if settings.embedding_redis_url is None:
            return None
        redis = aioredis.from_url(
            settings.embedding_redis_url,
            socket_connect_timeout=TIMEOUT,
            socket_timeout=TIMEOUT,
        )
        return cls({ADK_WORKFLOWS: RedisQueue(redis, name=ADK_WORKFLOWS)})

    async def run_adk(self, run_id: str) -> None:
        """
        Queue a job that takes an ADK workflow run (a new one, or one decided,
        answered or retried) and runs it.

        :param run_id: The run, as the run store keeps it.
        :raises EmbeddingError: The queue could not be reached.
        """
        key = f"adk-run:{run_id}:{uuid4().hex[:8]}"
        await self._enqueue(ADK_WORKFLOWS, RUN_ADK, key, run_id=run_id)

    async def aclose(self) -> None:
        for queue in self._queues.values():
            # Closing a connection to a Redis that went away.
            with contextlib.suppress(RedisError, OSError):
                await queue.disconnect()

    async def _enqueue(
        self, queue: str, function: str, key: str, **kwargs: Any
    ) -> None:
        try:
            async with asyncio.timeout(TIMEOUT):
                await self._queues[queue].enqueue(function, key=key, **kwargs)
        except (RedisError, OSError, TimeoutError) as error:
            raise EmbeddingError(str(error) or type(error).__name__) from None
