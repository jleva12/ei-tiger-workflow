"""
The worker's queues: SAQ's ``adk_workflows``, ``adk_workflows_upkeep`` and
``documents`` queues on Redis. The ``documents`` queue takes ``run_job`` with
a job spec of the documents task (``{"task_type": "documents", "kind":
"ingest" | "delete", "payload": {...}}``), which the admin API sends for each
knowledge base document uploaded, retried or removed. The admin API sends the
``adk_workflows`` queue ``run_adk`` for each run it starts or moves on (a
decision, an answer, a retry); the worker sends its own: ``run_adk`` when a
wait is over or a hiccup's backoff is, and the maintenance's. Its upkeep,
``maintain`` every minute and ``expire_pause`` at a pause's deadline, has a
queue of its own, so runs filling every worker slot never hold it up.

Every job carries ``key`` (a job whose key is queued or running already isn't
queued again) and the worker's settings for its function (``JOB_OPTIONS``,
which the worker also applies to the jobs other services send).

What a run failed with is kept in Redis too, a week at most, as its worker
writes it to the run store (``keep_failure``): should the store not take it,
the maintenance records it. Each taking of the run keeps its own (by its
``lease_epoch``), so an earlier one forgetting its own never takes a later
one's.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

QUEUE = "adk_workflows"
#: The worker's upkeep of the runs: ``maintain`` and ``expire_pause``.
UPKEEP = "adk_workflows_upkeep"
DOCUMENTS = "documents"
RUN_ADK = "run_adk"
RUN_JOB = "run_job"
EXPIRE_PAUSE = "expire_pause"
MAINTAIN = "maintain"
#: Seconds a running ``run_adk`` may go untouched before SAQ's sweep takes it
#: for lost: never (0). Whether a run's attempt is alive is its lease's to say,
#: in the run store, which the worker renews and fences every write on, and
#: whose end the maintenance recovers the run at, with a job of its own. SAQ's
#: sweep, which can't read the lease, aborted the job of a run whose worker
#: had only lost Redis for a while (its touches failed), cutting off an attempt
#: that held its lease. The worker still touches the job with each renewal,
#: for anyone reading Redis.
HEARTBEAT = 0
#: Each job's SAQ settings, whoever queued it. ``run_adk`` has no heartbeat
#: (HEARTBEAT) and no SAQ retries: a run's own attempts are the run store's (a
#: hiccup queues it again; the maintenance finds what a dead worker left). Its
#: timeout is the worker's that takes it, set as it does (0 here): as long as
#: one attempt may take, and a while (``AdkRunJobs.job_seconds``), past which
#: the worker has stopped it itself. So SAQ's sweep takes for lost only a dead
#: worker's ``run_adk``, left active in Redis, long after the maintenance
#: recovered its run.
JOB_OPTIONS: dict[str, dict[str, Any]] = {
    RUN_ADK: {"timeout": 0, "heartbeat": HEARTBEAT, "retries": 1},
    EXPIRE_PAUSE: {"timeout": 60, "heartbeat": 0, "retries": 1},
    MAINTAIN: {"timeout": 300, "heartbeat": 0, "retries": 1},
    # A document's ingest: parsing a large PDF and embedding its chunks takes
    # minutes; a TransientError (the embedder's rate limit, Mongo's hiccup)
    # is retried by SAQ. Unlike workflow runs, document jobs don't renew a
    # lease or touch SAQ: the execution timeout also bounds abandoned jobs.
    RUN_JOB: {"timeout": 1800, "heartbeat": 0, "retries": 3, "retry_delay": 10.0, "retry_backoff": True},
}


#: Seconds a failure kept for the maintenance lasts (``keep_failure``).
FAILURE_SECONDS = 7 * 24 * 3600


def failure_key(run_id: str, epoch: int | None) -> str:
    """:return: Where a run's failure is kept for the maintenance, by the
    taking of the run that failed it (``epoch``); None: where workers kept
    it before they kept it by taking, one for the run."""
    return f"forge:adk-run-failure:{run_id}" if epoch is None else f"forge:adk-run-failure:{run_id}:{epoch}"


def run_key(run_id: str, suffix: str | None = None) -> str:
    """A ``run_adk`` job's key: one of its own (``suffix`` None), or a fixed
    one, so a job already queued under it isn't queued twice."""
    return f"adk-run:{run_id}:{suffix or uuid.uuid4().hex[:8]}"


class RunQueue:
    """
    Sends the worker's jobs to the ``adk_workflows`` queue, and its upkeep's
    to the ``adk_workflows_upkeep`` queue; keeps runs' failures in Redis.

    :param queue: The ``saq.Queue`` (anything with ``enqueue(function, **kwargs)``,
        ``disconnect()`` and, to keep failures, ``redis``).
    :param upkeep: The upkeep's ``saq.Queue``; None sends its jobs to ``queue``.
    """

    def __init__(
        self, queue: Any, *, upkeep: Any = None, job_options: Mapping[str, Mapping[str, Any]] | None = None
    ) -> None:
        self.queue = queue
        self.upkeep = upkeep if upkeep is not None else queue
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
        await self._send(EXPIRE_PAUSE, {"run_id": run_id, "pause_id": pause_id}, key=key, at=at, to=self.upkeep)

    async def _send(
        self, function: str, kwargs: dict[str, Any], *, key: str, at: datetime | None, to: Any = None
    ) -> None:
        options = {**self.job_options.get(function, {}), "key": key}
        if at is not None:
            options["scheduled"] = math.ceil(at.timestamp())  # never before it
        await (to if to is not None else self.queue).enqueue(function, **kwargs, **options)

    @property
    def queues(self) -> tuple[Any, ...]:
        """The SAQ queues it sends to: its own, and its upkeep's when that's another."""
        return (self.queue,) if self.upkeep is self.queue else (self.queue, self.upkeep)

    async def keep_failure(self, run_id: str, epoch: int, kept: dict[str, Any]) -> None:
        """Keeps what the ``epoch``-th taking of a run failed it with (as its
        worker puts it) where the maintenance finds it, should the run store
        not take it."""
        await self.queue.redis.set(failure_key(run_id, epoch), json.dumps(kept, default=str), ex=FAILURE_SECONDS)

    async def kept_failure(self, run_id: str, epoch: int) -> dict[str, Any] | None:
        """
        :return: What the ``epoch``-th taking of the run kept (``keep_failure``),
            or a worker from before takings kept their own kept for it; None
            when it kept nothing.
        """
        found = await self._read(failure_key(run_id, epoch))
        if found is None:
            # Kept for the run by a worker from before: that taking's only.
            found = await self._read(failure_key(run_id, None))
            if found is not None and found.get("epoch") != epoch:
                return None
        return found

    async def forget_failure(self, run_id: str, epoch: int) -> None:
        """Forgets what the ``epoch``-th taking of the run kept, and nothing
        another kept. (What a worker from before kept for the run runs out on
        its own: no later taking reads it.)"""
        await self.queue.redis.delete(failure_key(run_id, epoch))

    async def _read(self, key: str) -> dict[str, Any] | None:
        raw = await self.queue.redis.get(key)
        try:
            found = json.loads(raw) if raw is not None else None
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    async def close(self) -> None:
        """Disconnects from Redis. Best effort."""
        for queue in self.queues:
            try:
                await queue.disconnect()
            except Exception:
                log.warning("closing queue %s failed", queue.name, exc_info=True)
