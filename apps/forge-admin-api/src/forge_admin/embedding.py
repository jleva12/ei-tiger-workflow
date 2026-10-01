"""The async worker (apps/forge-async-worker): submitting its jobs through its
SAQ job queues on Redis. The module keeps its first name, from when the
worker only embedded documents.

The worker has one queue per task type (here ``workflows`` and
``adk_workflows``) and one job function, ``run_job``, which runs a spec:
``{"task_type", "kind", "payload"}``, and the ``tenant_id`` and ``labels``
its tracked run carries when given. It applies its own retries, timeout and
heartbeat to every job, so a submission names only the queue, the spec and a
key; a key that is already queued or running isn't submitted again. This
client only relays; the routes decide who may submit what.
"""

import asyncio
import contextlib
from typing import Any, Self

from redis import asyncio as aioredis
from redis.exceptions import RedisError
from saq.queue.redis import RedisQueue

from forge_admin.config import Settings

WORKFLOWS = "workflows"
# ADK workflows' runs, apart from Forge workflows' (the ADK workflows task).
ADK_WORKFLOWS = "adk_workflows"
FUNCTION = "run_job"
# How long the worker keeps a finished job for reading it back, in seconds.
RESULT_TTL = 7 * 24 * 60 * 60
# Seconds a submission may take.
TIMEOUT = 5.0


class EmbeddingError(Exception):
    """The async worker's queue could not be reached."""


class Embedding:
    """
    Submits jobs to the async worker.

    :param queues: The worker's queues by name: ``workflows``,
        ``adk_workflows``.
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
        return cls(
            {name: RedisQueue(redis, name=name) for name in (WORKFLOWS, ADK_WORKFLOWS)}
        )

    async def run_workflow(
        self,
        *,
        tenant_id: str,
        key: str,
        labels: dict[str, str],
        payload: dict[str, Any],
        requested_by: dict[str, str] | None = None,
    ) -> None:
        """
        Submit one run of an organization's workflow: the worker runs it as a tracked
        run among the organization's background tasks, found again by its labels.

        :param tenant_id: The organization.
        :param key: The job's key; a key already queued or kept isn't
            submitted again, which makes a retried start one run.
        :param labels: What the run carries to be found by: ``workflow``, and
            ``parent`` for a run another run started.
        :param payload: The run: the workflow's document as it is now, the
            input and the member it acts as (the workflows task's
            ``RunPayload``).
        :param requested_by: Who started it, ``{"id", "display_name"}``, as
            the run records it.
        :raises EmbeddingError: The queue could not be reached.
        """
        await self._run(WORKFLOWS, tenant_id, key, labels, payload, requested_by)

    async def run_adk_workflow(
        self,
        *,
        tenant_id: str,
        key: str,
        labels: dict[str, str],
        payload: dict[str, Any],
        requested_by: dict[str, str] | None = None,
    ) -> None:
        """
        Submit one run of an organization's ADK workflow, on its own queue:
        the worker's ADK workflows task runs it as a tracked run among the
        organization's background tasks, found again by its labels.

        :param tenant_id: The organization.
        :param key: The job's key; a key already queued or kept isn't
            submitted again.
        :param labels: What the run carries to be found by: ``adk_workflow``
            and ``adk_session``.
        :param payload: The run: the ADK workflow's document as it is now,
            the saved ones it runs, the input, its ADK session and the member
            it acts as (``forge_admin.adk_runs``).
        :param requested_by: Who started it, ``{"id", "display_name"}``.
        :raises EmbeddingError: The queue could not be reached.
        """
        await self._run(ADK_WORKFLOWS, tenant_id, key, labels, payload, requested_by)

    async def _run(
        self,
        task_type: str,
        tenant_id: str,
        key: str,
        labels: dict[str, str],
        payload: dict[str, Any],
        requested_by: dict[str, str] | None,
    ) -> None:
        # A run of a task type, on its queue.
        spec: dict[str, Any] = {
            "task_type": task_type,
            "kind": "run",
            "tenant_id": tenant_id,
            "labels": labels,
            "payload": payload,
        }
        if requested_by:
            spec["requested_by"] = requested_by
        await self._enqueue(task_type, key, spec)

    async def aclose(self) -> None:
        for queue in self._queues.values():
            # Closing a connection to a Redis that went away.
            with contextlib.suppress(RedisError, OSError):
                await queue.disconnect()

    async def _enqueue(self, queue: str, key: str, spec: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(TIMEOUT):
                await self._queues[queue].enqueue(
                    FUNCTION, key=key, ttl=RESULT_TTL, spec=spec
                )
        except (RedisError, OSError, TimeoutError) as error:
            raise EmbeddingError(str(error) or type(error).__name__) from None
