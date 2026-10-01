"""The worker's job queue: SAQ queues on Redis, one per task type (task types
may share one), each job the generic ``run_job`` function with a JobSpec."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

from forge_tasks.tasks import JobSpec

log = logging.getLogger(__name__)


class SaqJobQueue:
    """Sends JobSpecs to the generic ``run_job`` SAQ job, on the queue of the
    spec's task type. ``queues`` maps task type -> ``saq.Queue`` (anything with
    ``enqueue(function, **kwargs)``); ``job_options`` are the SAQ job settings
    every job carries (retries, heartbeat, timeout; see saq_worker.py)."""

    FUNCTION = "run_job"
    REDRIVE = "redrive_run"
    RESTART = "restart_run"
    RESUME = "resume_run"
    DECIDE = "decide_run"

    def __init__(self, queues: Mapping[str, Any], *, job_options: Mapping[str, Any] | None = None) -> None:
        self.queues = dict(queues)
        self.job_options = dict(job_options or {})

    def queue_of(self, task_type: str) -> Any:
        queue = self.queues.get(task_type)
        if queue is None:
            raise KeyError(f"no queue for task type {task_type!r}; queues: {sorted(self.queues)}")
        return queue

    async def enqueue(self, spec: JobSpec, *, countdown: float | None = None, key: str | None = None) -> None:
        queue = self.queue_of(spec.task_type)
        options = dict(self.job_options)
        if countdown:
            options["scheduled"] = int(time.time() + countdown)
        if key:
            options["key"] = key
        await queue.enqueue(self.FUNCTION, spec=spec.wire(), **options)

    async def resubmit(self, spec: JobSpec, *, of: str) -> tuple[str, str]:
        """The job again, as a new task (a delivery of its own); returns its (queue, key)."""
        key = f"resubmit:{of}:{uuid.uuid4().hex[:12]}"
        await self.enqueue(spec, key=key)
        return self.queue_of(spec.task_type).name, key

    async def enqueue_restart(
        self, task_type: str, instance_id: str, attempt: int, actor: dict[str, str]
    ) -> tuple[str, str]:
        """Restart a task on a worker of its queue, once per attempt however often
        it's asked; returns the SAQ job's (queue, key)."""
        queue = self.queue_of(task_type)
        key = f"restart:{instance_id}:{attempt}"
        await queue.enqueue(self.RESTART, instance_id=instance_id, actor=dict(actor), key=key, **self.job_options)
        return queue.name, key

    async def enqueue_resume(self, task_type: str, instance_id: str, *, at: float, key: str) -> None:
        """Resume a run that waits until ``at`` (a Unix time), on a worker of its queue, then."""
        queue = self.queue_of(task_type)
        options = {**self.job_options, "scheduled": int(at), "key": key}
        await queue.enqueue(self.RESUME, instance_id=instance_id, **options)

    async def enqueue_decision(
        self,
        task_type: str,
        instance_id: str,
        *,
        approved: bool,
        comment: str,
        actor: dict[str, str] | None,
        request_id: str | None = None,
        gate: str | None = None,
        at: float | None = None,
        key: str | None = None,
    ) -> tuple[str, str]:
        """Decide a run's open approval on a worker of its queue (now, or at ``at``:
        a timeout), since the run carries on in the process that decides it.
        Returns the SAQ job's (queue, key)."""
        queue = self.queue_of(task_type)
        key = key or f"decide:{instance_id}:{request_id or gate}"
        options = {**self.job_options, "key": key}
        if at is not None:
            options["scheduled"] = int(at)
        await queue.enqueue(
            self.DECIDE,
            instance_id=instance_id,
            request_id=request_id,
            gate=gate,
            approved=approved,
            comment=comment,
            actor=dict(actor) if actor else None,
            **options,
        )
        return queue.name, key

    async def enqueue_redrive(self, task_type: str, instance_id: str) -> None:
        """Re-drive a recovered run on its task type's queue; one job per run,
        however many sweeps find it."""
        queue = self.queues.get(task_type)
        if queue is None:
            log.warning("run %s of task type %r has no queue here; not re-driven", instance_id, task_type)
            return
        await queue.enqueue(self.REDRIVE, instance_id=instance_id, key=f"redrive:{instance_id}", **self.job_options)

    async def close(self) -> None:
        """Disconnects each queue once (task types may share one). Best effort,
        so one queue that can't reach Redis doesn't keep the others open."""
        for queue in {id(q): q for q in self.queues.values()}.values():
            try:
                await queue.disconnect()
            except Exception:
                log.warning("closing queue %s failed", getattr(queue, "name", queue), exc_info=True)
