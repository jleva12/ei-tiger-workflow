"""
What the worker's jobs do to ADK workflow runs, which live in the run store
(``forge_task_adk_workflows.run_store``, in the admin MySQL).

``run_adk`` takes the run (``claim``: a run that isn't to be run now, being
another worker's, paused, finished, still waiting or being abandoned, is left
alone). Taken too late to run it (the database, or this process, took so long
that little of its lease is left), it's left to the maintenance. Otherwise its
attempt runs as a task of its own: the ADK workflows task's ``run`` job on the
run's payload, under a control backed by the run (control.py), then what
became of the run written. Its lease is held throughout, renewed (and its SAQ
job touched) every third of it, from its claim until what became of the run
is written; and the job is stopped:

- when someone abandons the run: a checkpoint stops it at its next step, the
  renewal while a step goes on;
- once the attempt has gone on for ``attempt_seconds`` (the task's
  ``attempt_timeout``);
- while renewals fail, before its last confirmed lease runs out on any
  worker's clock: it never goes on once another worker may have the run;
- when the lease was lost;
- when the worker stops (SAQ cancels its jobs).

A stopped job has STOP_SECONDS to stop, its lease held meanwhile, and never
past what's left of that lease. One that doesn't (it ignores the
cancellation, or holds the event loop) ends the process: the watchdog
(watchdog.py) holds each running job's deadline on a thread of its own, so
the job can't go on beside another worker's attempt at its run. How the job
ends decides what becomes of the run:

==============================  ==============================================
the job ...                     the run ...
==============================  ==============================================
returns a result                succeeds, with the graph's result; or fails
                                (``failed``) with the result's message and step
waits for people                is paused at each gate (also waiting, with a
                                time); a gate with a deadline has its
                                ``expire_pause`` queued for then. A gate
                                decided while it ran queues it again instead
waits for a time                waits; ``run_adk`` is queued for then
hits a hiccup (TransientError)  is queued again as its next attempt, taken
                                no sooner than its backoff ends (``run_adk``
                                queued for then); it fails once it has had
                                MAX_ATTEMPTS
goes on past its deadline       the same, as a hiccup
is stopped by an abandonment    is abandoned; whatever would have carried it on
                                (a pause, a wait, a retry) abandons it instead
raises anything else            fails (``error``)
is stopped as its worker stops  is queued again at once (``interrupted``), once
                                the job has stopped
loses its lease                 is left to whoever has it now
==============================  ==============================================

What became of the run is written while its lease is held, each write taking
WRITE_SECONDS at most, up to WRITE_TRIES times while the database hiccups. A
failure (or a hiccup) is kept in Redis first (``RunQueue.keep_failure``), and
forgotten once written: should the write not land, the maintenance records
it once the run's lease runs out, failing the run rather than running it
again, whatever became of the worker meanwhile.

``expire_pause`` rejects an approval nobody decided by its deadline (the run
carries on down its rejected way; the other gates stay open). ``maintain`` runs every minute: runs whose
worker went are queued again (or fail: once too often, or with the failure
their worker kept; while Redis can't say whether it kept one, they wait for
the next maintenance), runs abandoned while their worker went are abandoned,
queued runs no job took and waits that are over get a job, and pauses past
their deadline expire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from forge_common.adk.usage import recording
from pydantic import ValidationError

from forge_async_worker.control import PauseRun, RunControl, StopRun, WaitRun, is_hiccup
from forge_async_worker.queue import RunQueue, run_key
from forge_async_worker.watchdog import Watchdog
from forge_task_adk_workflows.run_store import (
    QUEUED,
    CancelRequested,
    LostLease,
    RunStore,
    open_gates,
    overdue_gates,
)
from forge_task_adk_workflows.runs import RunPayload
from forge_task_adk_workflows.task import RUN, TASK_NAME
from forge_task_adk_workflows.usage_store import UsageStore
from forge_tasks.control import controlling
from forge_tasks.errors import TransientError
from forge_tasks.runner import JobRunner
from forge_tasks.tasks import JobResult, JobSpec, JobStatus

log = logging.getLogger(__name__)

#: Seconds a worker holds a run without renewing it. It renews every third of
#: it, and every 24th after a renewal failed. While renewals fail, the run's
#: job is stopped a quarter of it before its last confirmed lease runs out, and
#: the process ends should the job still go on a twelfth of it before then
#: (workers' clocks differ a little): another worker may take the run once it
#: has. A run taken with no more than a quarter of it left isn't run.
LEASE_SECONDS = 120.0
#: Seconds one attempt of a run may take, when the worker has no ADK workflows
#: settings to say (their ``attempt_timeout``).
ATTEMPT_SECONDS = 7200.0
#: Seconds a job asked to stop has to stop, its lease held meanwhile: past it,
#: the process ends.
STOP_SECONDS = 30.0
#: Tries at writing what became of a run while the database hiccups, the pause
#: after the first (it doubles), and the seconds one try may take.
WRITE_TRIES = 4
WRITE_PAUSE_SECONDS = 1.0
WRITE_SECONDS = 10.0
#: Seconds one call to Redis (a job queued, a failure kept or read) may take.
REDIS_SECONDS = 5.0
#: The most attempts a run has: past it, a hiccup or an interruption fails it.
MAX_ATTEMPTS = 10
BACKOFF_BASE = 30.0  # seconds
BACKOFF_MAX = 1800.0
#: Seconds a queued run may go without a worker taking it before the
#: maintenance queues it again (its job was lost).
STALLED_AFTER = 120.0
TIMED_OUT = "Nobody decided by its deadline"
INTERRUPTED = "Its worker went while it ran"

# Why a job was stopped.
_LOST = "lost"
_OVERDUE = "overdue"
_ABANDONED = "abandoned"
_STOPPED = "stopped"


class WorkerStops(Exception):
    """The worker stops: the run's job was stopped, and the run is let go."""


class _Unknown(Exception):
    """Redis didn't answer: what it keeps isn't known."""


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
    #: Where runs' model and tool calls are recorded; beside the runs by default.
    usage: UsageStore | None = None
    #: Seconds one attempt of a run may take, all of it.
    attempt_seconds: float = ATTEMPT_SECONDS
    #: Ends the process should a run's job outlive what holds it.
    watchdog: Watchdog = field(default_factory=Watchdog)
    #: The attempts going on: each a task of its own, which a worker stopping waits for.
    _attempts: set[asyncio.Task[str]] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = UsageStore(self.store.engine)

    def _owner(self) -> str:
        # One attempt's: a worker that lost a run and took it again isn't the same owner.
        return f"{self.name}/{uuid.uuid4().hex[:12]}"[-128:]

    # ------------------------------------------------------------------ run_adk

    async def run_adk(self, run_id: str, *, job: Any = None) -> dict[str, Any]:
        """
        One attempt of a run, if it's to be run now.

        Cancelled (the worker stops), the run's job is stopped and the run let
        go once it has, which is waited for unless cancelled again: the
        attempt goes on all the same, as a task of its own.

        :param job: The SAQ job, touched while the run goes on.
        :return: What became of it: ``skipped``, ``succeeded``, ``failed``,
            ``paused``, ``waiting``, ``retrying``, ``abandoned`` or ``dropped``.
        """
        owner = self._owner()
        asked = asyncio.get_running_loop().time()
        run = await self.store.claim(run_id, owner=owner, lease_seconds=LEASE_SECONDS)
        if run is None:
            log.info("run %s isn't to be run now", run_id)
            return {"run_id": run_id, "outcome": "skipped"}
        control = RunControl(run, store=self.store, owner=owner, queue=self.queue.run_adk)
        hold = _Hold(self, control, job, confirmed=self._claimed(run, asked))
        if hold.left() <= LEASE_SECONDS / 4:
            log.warning(
                "run %s: only %.1fs of its lease is left now it's taken (the database or this worker was slow): "
                "it isn't run, and is left to the maintenance",
                run_id,
                hold.left(),
            )
            outcome = "dropped"
        else:
            attempt = hold.start(self._attempt(run, control, hold))
            self._attempts.add(attempt)
            attempt.add_done_callback(self._attempts.discard)
            try:
                outcome = await asyncio.shield(attempt)
            except asyncio.CancelledError:
                hold.stop(_STOPPED, "its worker stops")
                try:
                    await asyncio.shield(attempt)  # its job stops, and the run is let go
                except asyncio.CancelledError:
                    attempt.add_done_callback(_logged)  # cancelled again: it goes on, on its own
                    raise
                except Exception:
                    log.exception("run %s: its attempt failed as its worker stopped", run_id)
                raise
        log.info("run %s: %s", run_id, outcome, extra={"run_id": run_id, "outcome": outcome})
        return {"run_id": run_id, "outcome": outcome}

    def _claimed(self, run: dict[str, Any], asked: float) -> float:
        """
        :param asked: When the claim was asked for, on the event loop's clock.
        :return: When the claim's lease runs out at the soonest, on the event
            loop's clock: LEASE_SECONDS after it was asked for (the run store
            may have taken its time), and no later than its ``lease_until``
            says on the run store's clock (should this process have stood
            still since).
        """
        loop = asyncio.get_running_loop()
        confirmed = asked + LEASE_SECONDS
        until = run.get("lease_until")
        if isinstance(until, datetime):
            confirmed = min(confirmed, loop.time() + (until - self.store.clock()).total_seconds())
        return confirmed

    async def close(self) -> None:
        """Waits for the attempts still going (their jobs stopping, what became
        of their runs written), as long as they may take at most."""
        if self._attempts:
            await asyncio.wait(set(self._attempts), timeout=STOP_SECONDS + WRITE_TRIES * WRITE_SECONDS)

    async def _attempt(self, run: dict[str, Any], control: RunControl, hold: _Hold) -> str:
        """The run's attempt, and what became of it written: what ``run_adk`` reports."""
        try:
            return await self._carry_on(run, control, hold)
        except LostLease as lost:
            log.warning("run %s is dropped: %s; it's left to whoever has it, or the maintenance", control.run_id, lost)
            return "dropped"

    async def _carry_on(self, run: dict[str, Any], control: RunControl, hold: _Hold) -> str:
        try:
            return await self._settle(run, control, hold)
        except (StopRun, CancelRequested):
            # Someone abandoned it while it ran: nothing carries it on.
            await self._durably(
                control,
                "its abandonment",
                lambda: self.store.abandon_running(control.run_id, owner=control.owner, state=control.state),
            )
            return "abandoned"
        except WorkerStops:
            await self._let_go(control)
            return "let go"

    async def _settle(self, run: dict[str, Any], control: RunControl, hold: _Hold) -> str:
        """The run's attempt, and what became of the run written."""
        try:
            RunPayload.model_validate(run["payload"])
        except ValidationError as error:
            return await self._fail(control, f"The run can't start: its payload isn't valid: {error}", "error")
        spec = JobSpec(task_type=TASK_NAME, kind=RUN, payload=run["payload"], tenant_id=run["organization_id"])
        try:
            result = await hold.run(self._run_job(spec, control))
        except PauseRun as pause:
            return await self._pause(control, pause)
        except WaitRun as wait:
            return await self._wait(control, wait)
        except TransientError as error:
            return await self._hiccup(run, control, error)
        except (LostLease, StopRun, CancelRequested, WorkerStops):
            raise
        except Exception as error:
            log.exception("run %s: its job raised", control.run_id)
            return await self._fail(control, f"{type(error).__name__}: {error}", "error")
        detail = result.detail
        if result.status is JobStatus.FAILED:
            message = result.error or "The run failed"
            return await self._fail(control, message, "failed", step=detail.get("step"), result=detail.get("result"))
        await self._durably(
            control,
            "its result",
            lambda: self.store.finish(
                control.run_id, owner=control.owner, state=control.state, succeeded=True, result=detail.get("result")
            ),
        )
        return "succeeded"

    async def _run_job(self, spec: JobSpec, control: RunControl) -> JobResult:
        sink = self.usage.record if self.usage is not None else None
        with controlling(control), recording(sink):
            return await self.runner.run(spec)

    async def _pause(self, control: RunControl, signal: PauseRun) -> str:
        gates = await self._durably(
            control,
            "its pause",
            lambda: self.store.pause(
                control.run_id,
                owner=control.owner,
                state=control.state,
                gates=signal.gates,
                until=signal.until,
                reason=signal.reason,
            ),
        )
        if gates is None:
            # Someone decided one of them meanwhile: it carries on with it.
            await self._send(f"run {control.run_id} after a decision", self.queue.run_adk(control.run_id))
            return "queued"
        # Those open when it was taken have their expiry queued already.
        known = {gate.get("id") for gate in open_gates(control.run)}
        for gate in gates:
            deadline = _when(gate.get("deadline"))
            if deadline is not None and gate["id"] not in known:
                await self._send(
                    f"the expiry of run {control.run_id}'s pause",
                    self.queue.expire_pause(control.run_id, gate["id"], at=deadline),
                )
        if signal.until is not None:
            await self._send(f"run {control.run_id} at its time", self.queue.run_adk(control.run_id, at=signal.until))
            return "waiting"
        return "paused"

    async def _wait(self, control: RunControl, signal: WaitRun) -> str:
        await self._durably(
            control,
            "its wait",
            lambda: self.store.wait(
                control.run_id, owner=control.owner, state=control.state, until=signal.when, reason=signal.reason
            ),
        )
        await self._send(f"run {control.run_id} at its time", self.queue.run_adk(control.run_id, at=signal.when))
        return "waiting"

    async def _hiccup(self, run: dict[str, Any], control: RunControl, error: TransientError) -> str:
        attempt = int(run["attempt"])
        message = str(error) or type(error).__name__
        if attempt >= MAX_ATTEMPTS:
            log.error("run %s gave up after %d attempts: %s", control.run_id, attempt, message)
            return await self._fail(control, f"{message} (it gave up after {attempt} attempts)", "transient")
        failure = {"message": message, "category": "transient"}
        delay = backoff(attempt - 1)
        # Not taken before then, whatever job comes for it first (the run store holds it to it).
        at = self.store.clock() + timedelta(seconds=delay)
        # Kept first: should the run store not take it, the maintenance queues the run with it.
        kept = await self._keep_failure(control, failure, final=False)
        try:
            queued = await self._durably(
                control,
                "its retry",
                lambda: self.store.requeue(
                    control.run_id, owner=control.owner, error=failure, state=control.state, not_before=at
                ),
            )
        except (LostLease, CancelRequested):
            raise
        except Exception:
            self._unwritten(control, kept)
            raise
        if queued is None:
            raise LostLease(f"run {control.run_id} is no longer {control.owner}'s")
        if kept:
            await self._forget(control.run_id, control.lease_epoch)
        log.warning("run %s retries in %.0fs (attempt %d): %s", control.run_id, delay, attempt + 1, message)
        await self._send(
            f"run {control.run_id}'s retry",
            self.queue.run_adk(control.run_id, at=at, key=run_key(control.run_id, f"retry-{attempt + 1}")),
        )
        return "retrying"

    async def _fail(
        self, control: RunControl, message: str, category: str, *, step: Any = None, result: Any = None
    ) -> str:
        error = {"message": message, "category": category, "step": step}
        # Kept first: should the run store not take it before the run's lease
        # runs out, the maintenance records it, rather than "its worker went".
        kept = await self._keep_failure(control, error, final=True, result=result)
        try:
            await self._durably(
                control,
                "its failure",
                lambda: self.store.finish(
                    control.run_id,
                    owner=control.owner,
                    state=control.state,
                    succeeded=False,
                    result=result,
                    error=error,
                ),
            )
        except LostLease:
            raise
        except Exception:
            self._unwritten(control, kept)
            raise
        if kept:
            await self._forget(control.run_id, control.lease_epoch)
        return "failed"

    async def _durably[T](self, control: RunControl, what: str, write: Callable[[], Awaitable[T]]) -> T:
        """
        A write of what became of the run, each try taking WRITE_SECONDS at
        most, tried WRITE_TRIES times while the database hiccups (a lost
        connection, a failover, a write that hangs), pausing longer each time.
        Anything else (a lost lease, an abandonment) isn't tried again.
        """
        pause = WRITE_PAUSE_SECONDS
        for left in range(WRITE_TRIES - 1, -1, -1):
            try:
                async with asyncio.timeout(WRITE_SECONDS):
                    return await write()
            except Exception as error:
                if not left or not _retried(error):
                    raise
                log.warning(
                    "run %s: writing %s failed (%s); trying again in %gs",
                    control.run_id,
                    what,
                    type(error).__name__,
                    pause,
                )
            await asyncio.sleep(pause)
            pause *= 2
        raise AssertionError("WRITE_TRIES is at least 1")

    async def _keep_failure(
        self, control: RunControl, error: dict[str, Any], *, final: bool, result: Any = None
    ) -> bool:
        """
        What the run failed with, kept for the maintenance: whether it ends the
        run (``final``: else it's a hiccup, and the run is queued again), and
        which taking of the run failed it (``epoch``: a failure an earlier one
        kept is no later one's). Best effort: the process's log has it too.

        :return: Whether it's kept.
        """
        kept = {"error": error, "result": result, "final": final, "epoch": control.lease_epoch}
        try:
            async with asyncio.timeout(REDIS_SECONDS):
                await self.queue.keep_failure(control.run_id, control.lease_epoch, kept)
        except Exception:
            log.warning(
                "run %s: its failure can't be kept for the maintenance: %s", control.run_id, error, exc_info=True
            )
            return False
        return True

    def _unwritten(self, control: RunControl, kept: bool) -> None:
        if kept:
            log.error("run %s: the run store can't take its failure; it's kept for the maintenance", control.run_id)
        else:
            log.error("run %s: neither the run store nor Redis can take its failure", control.run_id)

    async def _let_go(self, control: RunControl) -> None:
        """The worker stops mid-run, and the run's job has: the run is queued
        again at once, for the next one, or abandoned when someone abandoned
        it. Best effort: should it fail, the maintenance finds the run once
        its lease runs out."""
        try:
            try:
                async with asyncio.timeout(WRITE_SECONDS):
                    await self.store.requeue(
                        control.run_id,
                        owner=control.owner,
                        error={"message": "Its worker stopped while it ran", "category": "interrupted"},
                        state=control.state,
                    )
            except CancelRequested:
                async with asyncio.timeout(WRITE_SECONDS):
                    await self.store.abandon_running(control.run_id, owner=control.owner, state=control.state)
        except Exception:
            log.warning("run %s: couldn't let it go as its worker stops", control.run_id, exc_info=True)

    # ------------------------------------------------------------------ expire_pause

    async def expire_pause(self, run_id: str, pause_id: str) -> dict[str, Any]:
        """
        A gate's deadline came: unless someone decided it, it's rejected (by
        nobody), and the run carries on (at once, unless a worker has it: its
        next attempt does).

        :return: What became of it: ``skipped`` (decided, or moved on),
            ``rescheduled`` (too early, on this worker's clock) or ``timed_out``.
        """
        run = await self.store.get(run_id)
        pause = next((gate for gate in open_gates(run or {}) if gate.get("id") == pause_id), None)
        if run is None or pause is None:
            return {"run_id": run_id, "outcome": "skipped"}
        deadline = _when(pause.get("deadline"))
        if deadline is not None and deadline > self.store.clock():
            async with asyncio.timeout(REDIS_SECONDS):
                await self.queue.expire_pause(run_id, pause_id, at=deadline)
            return {"run_id": run_id, "outcome": "rescheduled"}
        timed_out = await self.store.time_out(run_id, pause_id=pause_id, comment=TIMED_OUT)
        if timed_out is None:
            return {"run_id": run_id, "outcome": "skipped"}
        if timed_out["status"] == QUEUED:
            await self._send(f"run {run_id} after its timeout", self.queue.run_adk(run_id))
        return {"run_id": run_id, "outcome": "timed_out"}

    # ------------------------------------------------------------------ maintain

    async def maintain(self) -> dict[str, list[str]]:
        """
        The runs' upkeep: what a dead worker, a lost job or a missed deadline
        left behind.

        :return: The runs it ``recovered`` (queued again after their worker
            went), ``failed`` (their worker went once too often, or failed them
            and kept the failure), ``deferred`` (their worker went, but whether
            it kept a failure isn't known: Redis didn't answer), ``abandoned``
            (abandoned while their worker went), ``queued`` (a job for a run no
            job took, or whose wait is over) and ``timed_out``.
        """
        done: dict[str, list[str]] = {
            "recovered": [],
            "failed": [],
            "deferred": [],
            "abandoned": [],
            "queued": [],
            "timed_out": [],
        }
        for run in await self.store.interrupted():
            with _apart(run["id"]):
                await self._recover(run, done)
        for run in await self.store.stopping_left():
            with _apart(run["id"]):
                if await self.store.abandon_stopping(run["id"]):
                    done["abandoned"].append(run["id"])
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
            with _apart(run["id"]):
                timed_out = False
                for gate in overdue_gates(run, now):
                    if await self.store.time_out(run["id"], pause_id=gate["id"], comment=TIMED_OUT):
                        timed_out = True
                if timed_out:
                    await self._send(f"run {run['id']} after its timeout", self.queue.run_adk(run["id"]))
                    done["timed_out"].append(run["id"])
        if any(done.values()):
            log.warning("maintenance: %s", {name: len(ids) for name, ids in done.items() if ids})
        return done

    async def _recover(self, run: dict[str, Any], done: dict[str, list[str]]) -> None:
        """A run whose worker went (its lease ran out): it fails, with the
        failure its worker kept, or once too often; else it's queued again.
        While Redis can't say what its worker kept, it's left for the next
        maintenance: queued again, the failure kept would be an earlier
        taking's, and lost."""
        run_id, attempt = run["id"], int(run["attempt"])
        try:
            kept = await self._kept(run)
        except _Unknown:
            log.warning("run %s: whether its worker kept a failure isn't known; it's recovered later", run_id)
            done["deferred"].append(run_id)
            return
        if kept is not None and kept.get("final"):
            # Its worker failed it, and couldn't say so: it fails, as its worker meant.
            try:
                handled = await self.store.fail_interrupted(run_id, error=kept["error"], result=kept.get("result"))
            except Exception:
                log.warning("run %s: its result can't be written; it fails without it", run_id, exc_info=True)
                handled = await self.store.fail_interrupted(run_id, error=kept["error"])
            if handled:
                done["failed"].append(run_id)
        elif attempt >= MAX_ATTEMPTS:
            error = {"message": f"{INTERRUPTED}, {attempt} times", "category": "interrupted"}
            handled = await self.store.fail_interrupted(run_id, error=error)
            if handled:
                done["failed"].append(run_id)
        else:
            # Its worker went, or hit a hiccup it couldn't say so of (the hiccup it kept).
            error = kept["error"] if kept is not None else {"message": INTERRUPTED, "category": "interrupted"}
            handled = await self.store.requeue(run_id, owner=None, error=error) is not None
            if handled:
                await self._send(f"recovered run {run_id}", self.queue.run_adk(run_id))
                done["recovered"].append(run_id)
        if handled and kept is not None:
            await self._forget(run_id, int(run.get("lease_epoch") or 0))

    async def _kept(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """
        :return: The failure the run's worker kept for the maintenance
            (``_keep_failure``) as it ran it last; None when it kept none.
        :raises _Unknown: Redis didn't answer: whether it kept one isn't known.
        """
        epoch = int(run.get("lease_epoch") or 0)
        try:
            async with asyncio.timeout(REDIS_SECONDS):
                kept = await self.queue.kept_failure(run["id"], epoch)
        except Exception as error:
            log.warning("run %s: reading the failure its worker kept failed", run["id"], exc_info=True)
            raise _Unknown(f"Redis didn't say what run {run['id']}'s worker kept") from error
        if kept is None:
            return None
        if kept.get("epoch") != epoch or not isinstance(kept.get("error"), dict):
            # Nothing a worker of this taking kept.
            await self._forget(run["id"], epoch)
            return None
        return kept

    async def _forget(self, run_id: str, epoch: int) -> None:
        """Forgets what the ``epoch``-th taking of the run kept: never another's."""
        try:
            async with asyncio.timeout(REDIS_SECONDS):
                await self.queue.forget_failure(run_id, epoch)
        except Exception:
            log.warning("run %s: forgetting the failure its worker kept failed", run_id, exc_info=True)

    async def _send(self, what: str, sending: Awaitable[None]) -> bool:
        """Queues a job, REDIS_SECONDS at most. Best effort: the maintenance
        finds a run whose job was never queued."""
        try:
            async with asyncio.timeout(REDIS_SECONDS):
                await sending
        except Exception:
            log.warning("couldn't queue %s; the maintenance will", what, exc_info=True)
            return False
        return True


class _Hold:
    """
    An attempt's hold on its run, from its claim until what became of the run
    is written: its lease, renewed every third of it (its SAQ job touched with
    it) by a keeper task; and the attempt's job, run as a task of its own,
    stopped when it must be, its deadline held by the watchdog while it runs.
    Times are the event loop's clock.

    :param confirmed: When the lease runs out at the soonest, as its claim
        said.
    """

    def __init__(self, jobs: AdkRunJobs, control: RunControl, job: Any, *, confirmed: float) -> None:
        self.jobs = jobs
        self.control = control
        #: The SAQ job, touched as the lease is renewed.
        self.job = job
        self.loop = asyncio.get_running_loop()
        #: When the lease runs out at the soonest, as its claim or last renewal confirmed.
        self.confirmed = confirmed
        #: When the attempt's job goes on too long (its ``attempt_seconds``).
        self.overdue = self.loop.time() + jobs.attempt_seconds
        #: Why the job was stopped, once it was; and by when it must have ended.
        self.stopped: str | None = None
        self.stop_by: float | None = None
        self.work: asyncio.Task[JobResult] | None = None
        #: Set once the attempt is done: its keeper stops, between two renewals.
        self._done = asyncio.Event()

    def left(self) -> float:
        """:return: Seconds the lease is confirmed for, from now."""
        return self.confirmed - self.loop.time()

    def working(self) -> bool:
        return self.work is not None and not self.work.done()

    def start(self, attempt: Coroutine[Any, Any, str]) -> asyncio.Task[str]:
        """:return: The attempt, as a task of its own, its lease kept until it's done."""
        return asyncio.create_task(self._held(attempt))

    async def _held(self, attempt: Coroutine[Any, Any, str]) -> str:
        keeper = asyncio.create_task(self._keep())
        try:
            return await attempt
        finally:
            # Not cancelled: a renewal cut off mid-query would cost its connection.
            self._done.set()
            await asyncio.wait({keeper})
            _logged(keeper)

    async def run(self, job: Coroutine[Any, Any, JobResult]) -> JobResult:
        """
        The attempt's job, as a task of its own, its deadline held by the
        watchdog until it has ended.

        :raises LostLease: The lease was lost, or couldn't be renewed (the job was stopped).
        :raises TransientError: The attempt went on past its deadline (the job was stopped).
        :raises StopRun: Someone abandoned the run (the job was stopped).
        :raises WorkerStops: The worker stops (the job was stopped).
        """
        if self.stopped is not None:
            job.close()  # stopped before it started
            raise self._signal()
        work = self.work = asyncio.create_task(job)
        work.add_done_callback(lambda _: self.jobs.watchdog.release(self))
        self._watch()
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError:
            # The attempt itself is (the process ends): its job stops with it.
            self.stop(_STOPPED, "its attempt was cancelled")
            raise
        try:
            return work.result()
        except asyncio.CancelledError:
            raise self._signal() from None

    def stop(self, why: str, said: str, *, within: float | None = None) -> None:
        """
        Stops the attempt's job, if it runs: it has ``within`` seconds to
        (STOP_SECONDS by default), never past the time its lease holds,
        else the watchdog ends the process. Stopped already, it has no
        longer than it had. ``why`` is the first reason given.
        """
        if self.stopped is None:
            self.stopped = why
        if not self.working():
            return  # it hasn't started (it won't), or it ended
        assert self.work is not None
        by = self.loop.time() + (STOP_SECONDS if within is None else within)
        if self.stop_by is None:
            log.warning("run %s: %s; its job stops", self.control.run_id, said)
            self.work.cancel()
        self.stop_by = by if self.stop_by is None else min(self.stop_by, by)
        self._watch()

    def _signal(self) -> Exception:
        """What the job being stopped means for the run."""
        if self.stopped == _OVERDUE:
            return TransientError(
                f"Its attempt went on for more than {self.jobs.attempt_seconds:g} seconds, so the worker stopped it"
            )
        if self.stopped == _ABANDONED:
            return StopRun()
        if self.stopped == _STOPPED:
            return WorkerStops()
        return LostLease(f"run {self.control.run_id} is no longer {self.control.owner}'s, or may not be")

    def _watch(self) -> None:
        """Holds the job's deadline with the watchdog: a twelfth of the lease
        before it runs out, and the time it had to stop, once stopped."""
        if not self.working():
            return
        limit = self.confirmed - LEASE_SECONDS / 12
        if self.stop_by is not None:
            limit = min(limit, self.stop_by)
        why = (
            f"run {self.control.run_id}'s job didn't stop in time ({self.stopped})"
            if self.stopped is not None
            else f"run {self.control.run_id}'s job went on past what it could hold of its lease"
        )
        self.jobs.watchdog.hold(self, time.monotonic() + (limit - self.loop.time()), why)

    async def _keep(self) -> None:
        """
        Renews the run's lease, and touches its SAQ job, until the attempt
        ends. Stops the job: when the lease was lost; when someone abandoned
        the run; once the attempt has gone on for ``attempt_seconds``; and
        while renewals fail, before its last confirmed lease runs out
        (LEASE_SECONDS), giving up the lease then.
        """
        every, retry = LEASE_SECONDS / 3, LEASE_SECONDS / 24
        run_id, store = self.control.run_id, self.jobs.store
        due = self.loop.time() + every
        while True:
            unsafe = self.confirmed - LEASE_SECONDS / 4
            wake = min(due, unsafe)
            if self.stopped is None:
                wake = min(wake, self.overdue)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0.0, wake - self.loop.time())):
                    await self._done.wait()
            if self._done.is_set():
                return
            now = self.loop.time()
            if self.stopped is None and now >= self.overdue:
                # Its job stops, or doesn't start; once it ended, what became of the run is written all the same.
                self.stop(_OVERDUE, f"its attempt went on for {self.jobs.attempt_seconds:g}s")
            if now >= unsafe:
                self.stop(_LOST, "its lease can't be renewed, and runs out")
                return
            sent = now
            try:
                # Bounded, so a renewal that hangs can't keep the job going past its lease.
                async with asyncio.timeout(min(every, unsafe - now)):
                    renewal = await store.renew(run_id, owner=self.control.owner, lease_seconds=LEASE_SECONDS)
            except Exception:
                log.warning("run %s: renewing its lease failed; it tries again", run_id, exc_info=True)
                due = self.loop.time() + retry
                continue
            if not renewal.kept:
                # Another worker may have the run already: its job stops at once.
                self.stop(_LOST, "its lease was lost", within=LEASE_SECONDS / 12)
                return
            confirmed = sent + LEASE_SECONDS
            if renewal.until is not None:
                # On the run store's clock too, should this process have stood still since.
                confirmed = min(confirmed, self.loop.time() + (renewal.until - store.clock()).total_seconds())
            self.confirmed, due = confirmed, self.loop.time() + every
            self._watch()
            if self.job is not None:
                try:
                    async with asyncio.timeout(retry):
                        await self.job.update()
                except Exception:
                    log.warning("run %s: touching its job failed", run_id, exc_info=True)
            if renewal.stopping:
                self.control.stopping = True
                self.stop(_ABANDONED, "someone abandoned it")


def _logged(task: asyncio.Task[Any]) -> None:
    """Logs what a task nobody waits for any more failed with, if anything."""
    if not task.cancelled() and (error := task.exception()) is not None:
        log.error("%s failed", task.get_name(), exc_info=error)


@contextlib.contextmanager
def _apart(run_id: str) -> Iterator[None]:
    """One run's upkeep: should it fail, the maintenance goes on with the
    others (and tries it again next time)."""
    try:
        yield
    except Exception:
        log.exception("maintenance: run %s", run_id)


def _retried(error: BaseException) -> bool:
    """Whether writing again may get past an error: the database's hiccup, or a write that took too long."""
    return is_hiccup(error) or isinstance(error, ConnectionError | TimeoutError)


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _backing_off(run: dict[str, Any], now: datetime) -> bool:
    """A run a worker from before ``next_attempt_at`` queued again after a
    hiccup waits out its backoff (its job is queued for then), at most the
    longest backoff its attempt can get. One with ``next_attempt_at`` isn't
    stalled before it (``RunStore.stalled``), nor taken (``RunStore.claim``)."""
    error = run.get("error") or {}
    if run["status"] != QUEUED or error.get("category") != "transient" or run.get("next_attempt_at") is not None:
        return False
    occurred = _when(error.get("occurred_at"))
    longest = _backoff(int(run["attempt"]) - 2) * 1.25
    return occurred is not None and now < occurred + timedelta(seconds=longest)
