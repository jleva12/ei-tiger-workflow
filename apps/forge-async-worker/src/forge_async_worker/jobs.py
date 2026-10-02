"""
What the worker's jobs do to ADK workflow runs, which live in the run store
(``forge_task_adk_workflows.run_store``, in the admin MySQL).

``run_adk`` takes the run (``claim``: a run that isn't to be run now, being
another worker's, paused, finished or still waiting, is left alone), then
runs the ADK workflows task's ``run`` job on its payload under a control
backed by the run (control.py), renewing its lease (and touching its SAQ job)
every third of it. How the job ends decides what becomes of the run:

==============================  ==============================================
the job ...                     the run ...
==============================  ==============================================
returns a result                succeeds, with the graph's result; or fails
                                (``failed``) with the result's message and step
waits for a person              is paused; with a deadline, ``expire_pause``
                                is queued for then
waits for a time                waits; ``run_adk`` is queued for then
hits a hiccup (TransientError)  is queued again as its next attempt, and
                                ``run_adk`` with backoff; it fails once it has
                                had MAX_ATTEMPTS
raises anything else            fails (``error``)
is cut off by a stop            is queued again at once (``interrupted``)
loses its lease                 is left to whoever has it now
==============================  ==============================================

``expire_pause`` rejects an approval nobody decided by its deadline (the run
carries on down its rejected way). ``maintain`` runs every minute: runs whose
worker went are queued again (or fail, once too often), queued runs no job
took and waits that are over get a job, and pauses past their deadline expire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from forge_async_worker.control import PauseRun, RunControl, WaitRun
from forge_async_worker.queue import RunQueue, run_key
from forge_task_adk_workflows.run_store import PAUSED, QUEUED, LostLease, RunStore
from forge_task_adk_workflows.runs import RunPayload
from forge_task_adk_workflows.task import RUN, TASK_NAME
from forge_tasks.control import controlling
from forge_tasks.errors import TransientError
from forge_tasks.runner import JobRunner
from forge_tasks.tasks import JobResult, JobSpec, JobStatus

log = logging.getLogger(__name__)

#: Seconds a worker holds a run without renewing it; it renews every third of it.
LEASE_SECONDS = 120.0
#: The most attempts a run has: past it, a hiccup or an interruption fails it.
MAX_ATTEMPTS = 10
BACKOFF_BASE = 30.0  # seconds
BACKOFF_MAX = 1800.0
#: Seconds a queued run may go without a worker taking it before the
#: maintenance queues it again (its job was lost).
STALLED_AFTER = 120.0
TIMED_OUT = "Nobody decided by its deadline"


def backoff(retries: int) -> float:
    """30s, 60s, 2m, 4m ... capped at 30m, with +-25% jitter: rides out
    provider overloads, rate limits and database failovers."""
    return _backoff(retries) * random.uniform(0.75, 1.25)


def _backoff(retries: int) -> float:
    return min(BACKOFF_BASE * (2 ** max(retries, 0)), BACKOFF_MAX)


def worker_name() -> str:
    return f"{socket.gethostname()}.{os.getpid()}"


@dataclass
class AdkRunJobs:
    """The worker's jobs, over the run store, the queue, and the runner of the built tasks."""

    store: RunStore
    queue: RunQueue
    runner: JobRunner
    name: str = field(default_factory=worker_name)

    def _owner(self) -> str:
        # One attempt's: a worker that lost a run and took it again isn't the same owner.
        return f"{self.name}/{uuid.uuid4().hex[:12]}"[-128:]

    # ------------------------------------------------------------------ run_adk

    async def run_adk(self, run_id: str, *, job: Any = None) -> dict[str, Any]:
        """
        One attempt of a run, if it's to be run now.

        :param job: The SAQ job, touched while the run goes on.
        :return: What became of it: ``skipped``, ``succeeded``, ``failed``,
            ``paused``, ``waiting``, ``retrying`` or ``dropped``.
        """
        owner = self._owner()
        run = await self.store.claim(run_id, owner=owner, lease_seconds=LEASE_SECONDS)
        if run is None:
            log.info("run %s isn't to be run now", run_id)
            return {"run_id": run_id, "outcome": "skipped"}
        control = RunControl(run, store=self.store, owner=owner)
        try:
            outcome = await self._carry_on(run, control, job)
        except LostLease:
            log.warning("run %s: this worker lost it to another; it's left to that one", run_id)
            outcome = "dropped"
        except asyncio.CancelledError:
            await self._let_go(control)
            raise
        log.info("run %s: %s", run_id, outcome, extra={"run_id": run_id, "outcome": outcome})
        return {"run_id": run_id, "outcome": outcome}

    async def _carry_on(self, run: dict[str, Any], control: RunControl, job: Any) -> str:
        try:
            RunPayload.model_validate(run["payload"])
        except ValidationError as error:
            return await self._fail(control, f"The run can't start: its payload isn't valid: {error}", "error")
        try:
            result = await self._attempt(run, control, job)
        except PauseRun as pause:
            return await self._pause(control, pause)
        except WaitRun as wait:
            return await self._wait(control, wait)
        except TransientError as error:
            return await self._hiccup(run, control, error)
        except LostLease:
            raise
        except Exception as error:
            log.exception("run %s: its job raised", control.run_id)
            return await self._fail(control, f"{type(error).__name__}: {error}", "error")
        detail = result.detail
        if result.status is JobStatus.FAILED:
            message = result.error or "The run failed"
            return await self._fail(control, message, "failed", step=detail.get("step"), result=detail.get("result"))
        await self.store.finish(
            control.run_id, owner=control.owner, state=control.state, succeeded=True, result=detail.get("result")
        )
        return "succeeded"

    async def _attempt(self, run: dict[str, Any], control: RunControl, job: Any) -> JobResult:
        """The run's job, while its lease is kept.

        :raises LostLease: The lease was lost meanwhile (the job was stopped).
        """
        spec = JobSpec(task_type=TASK_NAME, kind=RUN, payload=run["payload"], tenant_id=run["organization_id"])
        work = asyncio.create_task(self._run_job(spec, control))
        keeper = asyncio.create_task(self._keep(control, job, work))
        try:
            return await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the worker stops
            raise LostLease(f"run {control.run_id} is no longer {control.owner}'s") from None
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper

    async def _run_job(self, spec: JobSpec, control: RunControl) -> JobResult:
        with controlling(control):
            return await self.runner.run(spec)

    async def _keep(self, control: RunControl, job: Any, work: asyncio.Task[Any]) -> None:
        """Renews the run's lease, and touches its SAQ job, until the job ends;
        stops the job if the lease was lost."""
        while True:
            await asyncio.sleep(LEASE_SECONDS / 3)
            try:
                kept = await self.store.renew(control.run_id, owner=control.owner, lease_seconds=LEASE_SECONDS)
            except Exception:
                log.warning("run %s: renewing its lease failed", control.run_id, exc_info=True)
                continue
            if not kept:
                log.warning("run %s: its lease was lost; its job stops", control.run_id)
                work.cancel()
                return
            if job is not None:
                try:
                    await job.update()
                except Exception:
                    log.warning("run %s: touching its job failed", control.run_id, exc_info=True)

    async def _pause(self, control: RunControl, signal: PauseRun) -> str:
        pause = await self.store.pause(
            control.run_id,
            owner=control.owner,
            state=control.state,
            key=signal.key,
            kind=signal.kind,
            reason=signal.reason,
            details=signal.details,
            deadline=signal.deadline,
        )
        if signal.deadline is not None:
            await self._send(
                f"the expiry of run {control.run_id}'s pause",
                self.queue.expire_pause(control.run_id, pause["id"], at=signal.deadline),
            )
        return "paused"

    async def _wait(self, control: RunControl, signal: WaitRun) -> str:
        await self.store.wait(
            control.run_id, owner=control.owner, state=control.state, until=signal.when, reason=signal.reason
        )
        await self._send(f"run {control.run_id} at its time", self.queue.run_adk(control.run_id, at=signal.when))
        return "waiting"

    async def _hiccup(self, run: dict[str, Any], control: RunControl, error: TransientError) -> str:
        attempt = int(run["attempt"])
        message = str(error) or type(error).__name__
        if attempt >= MAX_ATTEMPTS:
            log.error("run %s gave up after %d attempts: %s", control.run_id, attempt, message)
            return await self._fail(control, f"{message} (it gave up after {attempt} attempts)", "transient")
        queued = await self.store.requeue(
            control.run_id,
            owner=control.owner,
            error={"message": message, "category": "transient"},
            state=control.state,
        )
        if queued is None:
            raise LostLease(f"run {control.run_id} is no longer {control.owner}'s")
        delay = backoff(attempt - 1)
        log.warning("run %s retries in %.0fs (attempt %d): %s", control.run_id, delay, attempt + 1, message)
        at = self.store.clock() + timedelta(seconds=delay)
        await self._send(
            f"run {control.run_id}'s retry",
            self.queue.run_adk(control.run_id, at=at, key=run_key(control.run_id, f"retry-{attempt + 1}")),
        )
        return "retrying"

    async def _fail(
        self, control: RunControl, message: str, category: str, *, step: Any = None, result: Any = None
    ) -> str:
        await self.store.finish(
            control.run_id,
            owner=control.owner,
            state=control.state,
            succeeded=False,
            result=result,
            error={"message": message, "category": category, "step": step},
        )
        return "failed"

    async def _let_go(self, control: RunControl) -> None:
        """The worker stops mid-run: the run is queued again at once, for the next one. Best effort:
        should it fail, the maintenance finds the run once its lease runs out."""
        try:
            await self.store.requeue(
                control.run_id,
                owner=control.owner,
                error={"message": "Its worker stopped while it ran", "category": "interrupted"},
                state=control.state,
            )
        except Exception:
            log.warning("run %s: couldn't queue it again as its worker stops", control.run_id, exc_info=True)

    # ------------------------------------------------------------------ expire_pause

    async def expire_pause(self, run_id: str, pause_id: str) -> dict[str, Any]:
        """
        The pause's deadline came: unless someone decided it, it's rejected
        (by nobody), and the run carries on.

        :return: What became of it: ``skipped`` (decided, or moved on),
            ``rescheduled`` (too early, on this worker's clock) or ``timed_out``.
        """
        run = await self.store.get(run_id)
        pause = (run or {}).get("pause") or {}
        if run is None or run["status"] != PAUSED or pause.get("id") != pause_id:
            return {"run_id": run_id, "outcome": "skipped"}
        deadline = _when(pause.get("deadline"))
        if deadline is not None and deadline > self.store.clock():
            await self.queue.expire_pause(run_id, pause_id, at=deadline)
            return {"run_id": run_id, "outcome": "rescheduled"}
        if await self.store.time_out(run_id, pause_id=pause_id, comment=TIMED_OUT) is None:
            return {"run_id": run_id, "outcome": "skipped"}
        await self._send(f"run {run_id} after its timeout", self.queue.run_adk(run_id))
        return {"run_id": run_id, "outcome": "timed_out"}

    # ------------------------------------------------------------------ maintain

    async def maintain(self) -> dict[str, list[str]]:
        """
        The runs' upkeep: what a dead worker, a lost job or a missed deadline
        left behind.

        :return: The runs it ``recovered`` (queued again after their worker
            went), ``failed`` (their worker went once too often),
            ``queued`` (a job for a run no job took, or whose wait is over)
            and ``timed_out``.
        """
        done: dict[str, list[str]] = {"recovered": [], "failed": [], "queued": [], "timed_out": []}
        for run in await self.store.interrupted():
            run_id, attempt = run["id"], int(run["attempt"])
            if attempt >= MAX_ATTEMPTS:
                error = {"message": f"Its worker went while it ran, {attempt} times", "category": "interrupted"}
                if await self.store.fail_interrupted(run_id, error=error):
                    done["failed"].append(run_id)
                continue
            error = {"message": "Its worker went while it ran", "category": "interrupted"}
            if await self.store.requeue(run_id, owner=None, error=error) is not None:
                await self._send(f"recovered run {run_id}", self.queue.run_adk(run_id))
                done["recovered"].append(run_id)
        now = self.store.clock()
        for run in await self.store.stalled(idle_seconds=STALLED_AFTER):
            if _backing_off(run, now):
                continue
            # A fixed key: a run whose job waits in a busy queue gets no more of them.
            if await self._send(
                f"stalled run {run['id']}", self.queue.run_adk(run["id"], key=run_key(run["id"], "sweep"))
            ):
                done["queued"].append(run["id"])
        for run in await self.store.overdue():
            pause_id = (run.get("pause") or {}).get("id")
            if pause_id and await self.store.time_out(run["id"], pause_id=pause_id, comment=TIMED_OUT) is not None:
                await self._send(f"run {run['id']} after its timeout", self.queue.run_adk(run["id"]))
                done["timed_out"].append(run["id"])
        if any(done.values()):
            log.warning("maintenance: %s", {name: len(ids) for name, ids in done.items() if ids})
        return done

    async def _send(self, what: str, sending: Awaitable[None]) -> bool:
        """Queues a job. Best effort: the maintenance finds a run whose job was never queued."""
        try:
            await sending
        except Exception:
            log.warning("couldn't queue %s; the maintenance will", what, exc_info=True)
            return False
        return True


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _backing_off(run: dict[str, Any], now: datetime) -> bool:
    """A run queued again after a hiccup waits out its backoff (its job is
    queued for then), at most the longest backoff its attempt can get."""
    error = run.get("error") or {}
    if run["status"] != QUEUED or error.get("category") != "transient":
        return False
    occurred = _when(error.get("occurred_at"))
    longest = _backoff(int(run["attempt"]) - 2) * 1.25
    return occurred is not None and now < occurred + timedelta(seconds=longest)
