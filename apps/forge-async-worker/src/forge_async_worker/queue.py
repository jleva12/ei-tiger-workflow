"""
The worker's queues: SAQ's ``adk_workflows`` and ``documents`` queues on Redis.
The ``documents`` queue takes ``run_job`` with a job spec of the documents task
(``{"task_type": "documents", "kind": "ingest" | "delete", "payload": {...}}``),
which the admin API sends for each knowledge base document uploaded, retried or
removed. The admin API sends the ``adk_workflows`` queue ``run_adk`` for
each run it starts or moves on (a decision, an answer, a retry); the worker
sends its own: ``run_adk`` when a wait is over or a hiccup's backoff is,
``expire_pause`` at a pause's deadline, and the maintenance's.

Every job carries ``key`` (a job whose key is queued or running already isn't
queued again) and the worker's settings for its function (``JOB_OPTIONS``,
which the worker also applies to the jobs other services send).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

QUEUE = "adk_workflows"
DOCUMENTS = "documents"
RUN_ADK = "run_adk"
RUN_JOB = "run_job"
EXPIRE_PAUSE = "expire_pause"
MAINTAIN = "maintain"
#: Seconds a running ``run_adk`` may go untouched before SAQ's sweeper takes
#: it for lost (its worker died); the worker touches it as it renews the run's lease.
HEARTBEAT = 120
#: Each job's SAQ settings, whoever queued it. ``run_adk`` has no timeout (a
#: run takes as long as it takes) and no SAQ retries: a run's own attempts are
#: the run store's (a hiccup queues it again; the maintenance finds what a
#: dead worker left).
JOB_OPTIONS: dict[str, dict[str, Any]] = {
    RUN_ADK: {"timeout": 0, "heartbeat": HEARTBEAT, "retries": 1},
    EXPIRE_PAUSE: {"timeout": 60, "heartbeat": 0, "retries": 1},
    MAINTAIN: {"timeout": 300, "heartbeat": 0, "retries": 1},
    # A document's ingest: parsing a large PDF and embedding its chunks takes
    # minutes; a TransientError (the embedder's rate limit, Mongo's hiccup)
    # is retried by SAQ.
    RUN_JOB: {"timeout": 1800, "heartbeat": HEARTBEAT, "retries": 3, "retry_delay": 10.0, "retry_backoff": True},
}


def run_key(run_id: str, suffix: str | None = None) -> str:
    """A ``run_adk`` job's key: one of its own (``suffix`` None), or a fixed
    one, so a job already queued under it isn't queued twice."""
    return f"adk-run:{run_id}:{suffix or uuid.uuid4().hex[:8]}"


class RunQueue:
    """
    Sends the worker's jobs to the ``adk_workflows`` queue.

    :param queue: The ``saq.Queue`` (anything with ``enqueue(function, **kwargs)``
        and ``disconnect()``).
    """

    def __init__(self, queue: Any, *, job_options: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.queue = queue
        self.job_options = {name: dict(options) for name, options in (job_options or JOB_OPTIONS).items()}

    @property
    def name(self) -> str:
        return str(self.queue.name)

    async def run_adk(self, run_id: str, *, at: datetime | None = None, key: str | None = None) -> None:
        """Run (or carry on) the run, now or at ``at``."""
        await self._send(RUN_ADK, {"run_id": run_id}, key=key or run_key(run_id), at=at)

    async def expire_pause(self, run_id: str, pause_id: str, *, at: datetime) -> None:
        """Reject the pause ``pause_id`` at ``at`` (its deadline), unless someone decided it by then."""
        key = f"adk-expire:{run_id}:{uuid.uuid4().hex[:8]}"
        await self._send(EXPIRE_PAUSE, {"run_id": run_id, "pause_id": pause_id}, key=key, at=at)

    async def _send(self, function: str, kwargs: dict[str, Any], *, key: str, at: datetime | None) -> None:
        options = {**self.job_options.get(function, {}), "key": key}
        if at is not None:
            options["scheduled"] = math.ceil(at.timestamp())  # never before it
        await self.queue.enqueue(function, **kwargs, **options)

    async def close(self) -> None:
        """Disconnects from Redis. Best effort."""
        try:
            await self.queue.disconnect()
        except Exception:
            log.warning("closing queue %s failed", self.name, exc_info=True)
