"""Orchestration: ``JobLauncher`` (start), ``JobRunner`` (execute), ``JobOperator`` (manage).

This is the only module that ties the contracts together. It depends solely on the ABCs
in :mod:`etf.store`, :mod:`etf.audit`, :mod:`etf.locking`, :mod:`etf.serialization`, and
:mod:`etf.policies` — so swapping any backend leaves this logic untouched. The lifecycle
flows implemented here are specified in ``docs/DESIGN.md`` §4.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .audit import AuditEvent, AuditQuery, AuditSink
from .context import ExecutionContext, StepContext
from .exceptions import (
    DefinitionVersionMismatchError,
    EtfError,
    JobAlreadyRunningError,
    JobInstanceAlreadyCompleteError,
    JobInstanceAlreadyExistsError,
    LockAcquisitionError,
    OptimisticLockError,
    RunNotRestartableError,
    StepExitError,
    StopExecution,
    ValidationStateError,
    ValidationAuthorizationError,
    ValidationSchemaError,
)
from .exceptions import PauseForValidation
from .locking import Lock, RunLockProvider
from .model import (
    Actor,
    FailureRecord,
    JobInstance,
    JobRun,
    StepRun,
    ValidationDecision,
    ValidationOutcome,
    ValidationRequest,
    check_labels,
)
from .policies import (
    Clock,
    DefaultRestartPolicy,
    HashingInstanceResolver,
    IdGenerator,
    JobInstanceResolver,
    NeverSkipPolicy,
    RestartPolicy,
    RetryPolicy,
    SkipPolicy,
    SystemClock,
    UuidGenerator,
    BackoffRetryPolicy,
)
from .serialization import JsonSerializer, Serializer
from .status import (
    AuditEventType,
    BatchStatus,
    ExitStatus,
    ValidationDecisionType,
    ValidationStatus,
)
from .steps import JobDefinition, JobRegistry, Step, StepResult
from .store import StateStore

logger = logging.getLogger(__name__)

#: Statuses of a run some worker is (or is about to be) advancing. A runner only writes
#: framework state while its run is in one of these; anything else means another actor
#: (the recovery sweep, an operator) settled the run while this worker held a stale lease.
_ACTIVE_RUN_STATUSES = (
    BatchStatus.PENDING,
    BatchStatus.RUNNING,
    BatchStatus.STOPPING,
    BatchStatus.PAUSING,
)
#: Settled statuses a runner never writes while its step is executing, so the control
#: monitor seeing one means the run was taken over.
_TAKEN_OVER_RUN_STATUSES = (
    BatchStatus.FAILED,
    BatchStatus.COMPLETED,
    BatchStatus.ABANDONED,
)
#: Cause-chain entries kept on a stored failure (the chain runs outward from the
#: raised exception, so the nearest causes are kept).
_MAX_CAUSE_CHAIN = 10


def _truncate_middle(text: str, limit: int | None) -> str:
    """Bound ``text`` to ``limit`` characters plus a marker, keeping its start and end."""
    if limit is None or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} characters truncated] ...\n{text[-tail:]}"


def _append_failure(failures: list[FailureRecord], failure: FailureRecord, limit: int) -> None:
    """Append, keeping the first failure (the original cause) and the most recent ones."""
    failures.append(failure)
    if len(failures) > limit:
        del failures[1 : len(failures) - limit + 1]


def _failure_key(failure: FailureRecord) -> tuple[object, ...]:
    """Identity of a failure across a store round trip (timestamps may lose precision)."""
    return (failure.exception_type, failure.message, failure.step_name, failure.attempt)


#: Failures that mean the worker went away without a verdict on the work: the sweep
#: found the run orphaned, or the host cancelled it (shutdown, timeout).
WORKER_LOST = "etf.WorkerLost"
_INTERRUPTED_FAILURES = frozenset({WORKER_LOST, "asyncio.exceptions.CancelledError"})


def _interruption(run: JobRun) -> str | None:
    """The failure type if ``run`` may be restarted automatically, else None: its
    worker went away without a verdict, or its last failure was classified
    retryable (``EtfConfig.failure_classifier``).

    A run that was being stopped or paused when that happened is not: someone
    asked for it to halt, so it must not be restarted automatically.
    """
    if run.status is not BatchStatus.FAILED or not run.failures:
        return None
    last = run.failures[-1]
    if last.exception_type not in _INTERRUPTED_FAILURES and not last.retryable:
        return None
    if last.attributes.get("halt_requested") or last.attributes.get("orphaned_in") in (
        BatchStatus.STOPPING.value,
        BatchStatus.PAUSING.value,
    ):
        return None
    return last.exception_type


@dataclass(frozen=True, slots=True)
class OperatorAction:
    """An operator request about to be applied, as passed to ``EtfConfig.authorizer``.

    ``name`` is one of ``stop``, ``pause``, ``resume``, ``restart``, ``redrive``,
    ``abandon`` or ``decide`` (a validation decision). ``details`` carries the
    request's options (parameter overrides, the decision type, …).
    """

    name: str
    actor: Actor
    run_id: str
    instance_id: str
    job_name: str
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Shared configuration / dependency bundle
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EtfConfig:
    """Wiring for the orchestration layer. Defaults give a working in-process setup
    once a concrete :class:`~etf.store.StateStore` and :class:`~etf.audit.AuditSink`
    are supplied.

    **Scaling knobs.** Each *running* step costs recurring store reads:
    ``control_poll_interval`` drives the stop/pause monitor (one ``get_job_run`` per
    tick per active step) and the runner re-reads the run at every step boundary.
    With hundreds of concurrent runs, raise ``control_poll_interval`` (slower reaction
    to ``stop()``/``pause()``, proportionally fewer reads) and ensure the store has the
    documented indexes. ``lock_ttl`` sets the lease heartbeat rate (one lock refresh
    every ``lock_ttl / 3``).
    """

    store: StateStore
    audit: AuditSink
    lock_provider: RunLockProvider
    registry: JobRegistry
    serializer: Serializer = field(default_factory=JsonSerializer)
    clock: Clock = field(default_factory=SystemClock)
    ids: IdGenerator = field(default_factory=UuidGenerator)
    instance_resolver: JobInstanceResolver = field(default_factory=HashingInstanceResolver)
    retry_policy: RetryPolicy = field(default_factory=BackoffRetryPolicy)
    skip_policy: SkipPolicy = field(default_factory=NeverSkipPolicy)
    restart_policy: RestartPolicy = field(default_factory=DefaultRestartPolicy)
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}")
    lock_ttl: timedelta = timedelta(minutes=10)
    control_poll_interval: timedelta = timedelta(seconds=1)
    idempotency_reservation_ttl: timedelta = timedelta(seconds=30)
    idempotency_ttl: timedelta = timedelta(days=1)
    #: How long a duplicate delivery waits (with exponential backoff) for the winning
    #: launch to commit its run before giving up with ``JobAlreadyRunningError``.
    idempotency_poll_budget: timedelta = timedelta(seconds=5)
    #: Persisted stack traces are truncated to this many characters (``None`` = keep
    #: whole), keeping the start and the end — the raised exception is at the end.
    max_stack_trace_length: int | None = 20_000
    #: The same bound for a failure's message and each entry of its cause chain.
    max_failure_message_length: int | None = 4_000
    #: Redacts secrets from every stored failure text — stack trace, message and cause
    #: chain, and the audit messages built from them — before truncation. If it
    #: raises, the text is withheld rather than stored unscrubbed.
    stack_trace_scrubber: Callable[[str], str] | None = None
    #: Failures kept on a run or step record. Beyond it the first failure (the
    #: original cause) and the most recent ones are kept; every failure stays in the
    #: audit trail as a ``STEP_FAILED`` event.
    max_failures_per_record: int = 20
    #: A run whose worker went away without a verdict (lost, or cancelled) is
    #: restarted automatically when its delivery comes back or ``JobOperator.redrive``
    #: is called — at most this many times in a row; 0 turns that off.
    max_auto_redrives: int = 3
    #: Called with every :class:`OperatorAction` before it is applied (stop, pause,
    #: resume, restart, redrive, abandon, validation decisions). Raise to deny; the
    #: denial is audited. ``Actor`` roles are claims made by the caller, so verify them
    #: here (against your identity provider) before trusting ``required_role`` gates.
    authorizer: Callable[["OperatorAction"], Awaitable[None] | None] | None = None
    #: Called with every step failure (the exception and its record) before it is
    #: stored. It may set ``record.retryable`` — a run whose last failure is
    #: retryable is restarted by its redelivery like an interrupted one (within
    #: ``max_auto_redrives``) — or add ``record.attributes``.
    failure_classifier: Callable[[BaseException, FailureRecord], None] | None = None
    #: Backstop against runaway flows (definitions are validated as acyclic, but a
    #: mutated definition or buggy store could still loop): a run aborts if it enters
    #: the same step more than this many times.
    max_step_visits: int = 100
    #: Refuse wiring against a store that cannot atomically commit multi-document
    #: lifecycle transitions. Production deployments should enable this; lightweight
    #: local/custom stores may leave it disabled with explicitly weaker guarantees.
    require_transactional_store: bool = False

    def __post_init__(self) -> None:
        if self.require_transactional_store and not self.store.supports_transactions:
            raise ValueError(
                "require_transactional_store=True requires a StateStore with "
                "multi-document transaction support"
            )
        if self.lock_ttl.total_seconds() <= 0:
            raise ValueError("lock_ttl must be positive")
        if self.control_poll_interval.total_seconds() < 0:
            raise ValueError("control_poll_interval cannot be negative")
        if self.idempotency_reservation_ttl.total_seconds() <= 0:
            raise ValueError("idempotency_reservation_ttl must be positive")
        if self.idempotency_poll_budget.total_seconds() < 0:
            raise ValueError("idempotency_poll_budget cannot be negative")
        if self.max_step_visits <= 0:
            raise ValueError("max_step_visits must be positive")
        if self.max_failures_per_record < 2:
            raise ValueError("max_failures_per_record must be at least 2")
        if self.max_auto_redrives < 0:
            raise ValueError("max_auto_redrives cannot be negative")
        for name in ("max_stack_trace_length", "max_failure_message_length"):
            limit = getattr(self, name)
            if limit is not None and limit <= 0:
                raise ValueError(f"{name} must be None or positive")


# Mutable per-run execution state threaded through the runner so that closures
# (checkpoint / request-validation callbacks) can observe the latest persisted versions
# without `nonlocal` gymnastics.
@dataclass(slots=True)
class _Exec:
    run: JobRun
    definition: JobDefinition
    run_ctx: ExecutionContext
    step: Step | None = None
    step_run: StepRun | None = None
    step_ctx: ExecutionContext | None = None
    fencing_token: int | None = None


@dataclass(slots=True)
class _StepOutcome:
    exit_status: ExitStatus | None = None
    validation_paused: bool = False
    halt_status: BatchStatus | None = None
    failed: bool = False
    failure: FailureRecord | None = None


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    """Validate the useful dependency-free subset of JSON Schema used by HITL gates."""
    if "const" in schema and value != schema["const"]:
        raise ValidationSchemaError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationSchemaError(f"{path} is not one of {schema['enum']!r}")
    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if expected in type_map:
        expected_type = type_map[expected]
        valid = isinstance(value, expected_type)
        if expected in ("number", "integer") and isinstance(value, bool):
            valid = False
        if not valid:
            raise ValidationSchemaError(f"{path} must be of type {expected}")
    if isinstance(value, dict):
        required = schema.get("required", ())
        for key in required:
            if key not in value:
                raise ValidationSchemaError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                _validate_schema_value(value[key], child_schema, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValidationSchemaError(f"{path} contains unsupported properties {sorted(extras)!r}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema_value(item, schema["items"], f"{path}[{index}]")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class JobRunner:
    """Executes a :class:`~etf.model.JobRun` to a terminal or paused state.

    Holds the writer lease for the run's instance while advancing it, skips
    already-completed steps (restart idempotency), drives per-step retry/skip, persists
    checkpoints, and converts a step's pause/stop signals into lifecycle transitions.
    """

    def __init__(self, cfg: EtfConfig) -> None:
        self.cfg = cfg

    # -- public entrypoint --
    async def run(self, run: JobRun, *, lock: Lock | None = None) -> JobRun:
        cfg = self.cfg
        try:
            definition = self._definition_for(run)
        except BaseException:
            if lock is not None:
                await lock.release()
            raise
        held_lock = lock or await cfg.lock_provider.acquire(run.instance_id, cfg.worker_id, cfg.lock_ttl)
        if held_lock is None:
            raise LockAcquisitionError(f"instance {run.instance_id} is being advanced by another worker")
        heartbeat: asyncio.Task[None] | None = None
        lease_lost: asyncio.Event | None = None
        state: _Exec | None = None
        try:
            raw_run_context = cfg.serializer.deserialize(run.execution_context)
            if not isinstance(raw_run_context, dict):
                raise TypeError("serializer.deserialize(run.execution_context) must return a dict")
            state = _Exec(
                run=run,
                definition=definition,
                run_ctx=ExecutionContext(raw_run_context),
                fencing_token=held_lock.fencing_token,
            )
            lease_lost = asyncio.Event()
            heartbeat = asyncio.create_task(self._lease_heartbeat(held_lock, lease_lost))
            # First entry emits RUN_STARTED; re-entry (resume/redelivery) does not — the
            # operator paths already emit RESUMED, so the trail shows one start per run.
            await self._transition_run(
                state,
                BatchStatus.RUNNING,
                emit=AuditEventType.RUN_STARTED if run.start_time is None else None,
            )
            step_name: str | None = run.current_step or definition.first_step
            visits: dict[str, int] = {}
            while step_name is not None:
                # Backstop against runaway flows (definitions are validated acyclic).
                visits[step_name] = visits.get(step_name, 0) + 1
                if visits[step_name] > cfg.max_step_visits:
                    raise EtfError(
                        f"run {run.id} entered step {step_name!r} more than "
                        f"{cfg.max_step_visits} times; aborting a runaway flow"
                    )
                signal = await self._external_signal(state)
                if signal is not None:
                    await self._transition_run(
                        state,
                        signal,
                        emit=AuditEventType.STOPPED
                        if signal is BatchStatus.STOPPED
                        else AuditEventType.PAUSED,
                    )
                    return state.run
                step = definition.step(step_name)
                if lease_lost.is_set():
                    raise LockAcquisitionError(f"lease lost for instance {run.instance_id}")
                # Restart idempotency: a step completed in a prior attempt is not re-run.
                if not step.allow_restart_if_complete:
                    done = await cfg.store.find_last_step_run(
                        run.instance_id, step_name, status=BatchStatus.COMPLETED
                    )
                    if done is not None:
                        await self._emit(
                            AuditEventType.IDEMPOTENT_SKIP,
                            state.run,
                            step_name=step_name,
                            message="step already completed in a prior run",
                        )
                        step_name = definition.next_step(step_name, done.exit_status)
                        continue
                outcome = await self._run_step(state, step, lease_lost)
                if outcome.validation_paused:
                    return state.run  # AWAITING_VALIDATION persisted; worker released
                if outcome.halt_status is not None:
                    event = (
                        AuditEventType.STOPPED
                        if outcome.halt_status is BatchStatus.STOPPED
                        else AuditEventType.PAUSED
                    )
                    await self._transition_run(state, outcome.halt_status, emit=event)
                    return state.run
                if outcome.failed:
                    await self._fail_run(state, outcome.failure)
                    return state.run
                signal = await self._external_signal(state)
                if signal is not None:
                    await self._transition_run(
                        state,
                        signal,
                        emit=AuditEventType.STOPPED
                        if signal is BatchStatus.STOPPED
                        else AuditEventType.PAUSED,
                    )
                    return state.run
                step_name = definition.next_step(step_name, outcome.exit_status or ExitStatus.COMPLETED)  # type: ignore[attr-defined]
            await self._complete(state)
            return state.run
        except BaseException as exc:
            # Crash recovery, half 1 (fail fast): any unexpected exception — store
            # errors, lease loss, serializer TypeError, CancelledError from a killed
            # bridge/task — must not strand the run in RUNNING. Mark it FAILED
            # (restartable) while the lock is still held, then let the original
            # exception propagate.
            if lease_lost is not None and lease_lost.is_set():
                # The fencing boundary has been crossed: this worker no longer has
                # authority to mutate framework state. A new owner or the recovery
                # sweep will settle the orphaned RUNNING records, so the reason this
                # worker stopped is logged here rather than stored.
                logger.error(
                    "run %s: this worker lost its lease and stopped (%s: %s); it no "
                    "longer owns the run, so the recovery sweep settles it",
                    run.id,
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )
            else:
                await self._fail_fast(run, state, exc)
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await held_lock.release()

    def _definition_for(self, run: JobRun) -> JobDefinition:
        """The definition a run executes on: the version it started with, unchanged.

        Raises ``DefinitionVersionMismatchError`` if that version is no longer
        registered, or if its steps or transitions changed without a version bump —
        resuming a checkpoint against a different flow would skip or misplace steps.
        """
        definition = self.cfg.registry.get(run.job_name, run.definition_version)
        if (
            run.definition_fingerprint is not None
            and run.definition_fingerprint != definition.fingerprint
        ):
            raise DefinitionVersionMismatchError(
                f"job {run.job_name!r} version {run.definition_version} changed (steps "
                f"or transitions) since run {run.id} was created; register the changed "
                "flow under a new version and keep this one registered until its runs "
                "finish"
            )
        return definition

    async def _fail_fast(self, run: JobRun, state: _Exec | None, exc: BaseException) -> None:
        """Best-effort transition of a crashing run to FAILED so it stays recoverable.

        The stored run keeps every failure this worker recorded, not only the crash:
        failures from earlier attempts may not have been persisted yet. If the write
        with its audit event fails (an audit store or sink outage, say), the run is
        still marked FAILED without the event rather than left RUNNING for the sweep
        to mislabel. Secondary failures are swallowed (logged) so the original
        exception — the one the caller needs to see — still propagates.
        """
        step_name = state.step.name if state is not None and state.step else None
        try:
            failure = self._sanitize_failure(
                FailureRecord.from_exception(exc, step_name=step_name)
            )
        except Exception:
            logger.exception("could not describe the crash of run %s", run.id)
            failure = FailureRecord(
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                message="[failure details unavailable]",
                step_name=step_name,
            )
        for with_audit in (True, False):
            try:
                await self.cfg.store.run_in_transaction(
                    lambda: self._record_crash(run, state, failure, with_audit=with_audit)
                )
            except BaseException:
                if with_audit:
                    logger.exception(
                        "could not record the crash of run %s with its audit event; "
                        "recording it without one",
                        run.id,
                    )
                    continue
                logger.exception(
                    "could not mark run %s FAILED after a runner crash; "
                    "the recovery sweep (JobOperator.recover_stale_runs) will reclaim it",
                    run.id,
                )
                return
            if not with_audit:
                logger.error(
                    "run %s marked FAILED without its audit event: %s: %s",
                    run.id,
                    failure.exception_type,
                    failure.message,
                )
            return

    async def _record_crash(
        self,
        run: JobRun,
        state: _Exec | None,
        failure: FailureRecord,
        *,
        with_audit: bool,
    ) -> None:
        cfg = self.cfg
        # Read inside the transaction so a replayed callback starts from fresh state.
        fresh = await cfg.store.get_job_run(run.id)
        if fresh.status not in _ACTIVE_RUN_STATUSES:
            return  # already settled; nothing to repair
        if fresh.status in (BatchStatus.STOPPING, BatchStatus.PAUSING):
            # Someone asked it to halt: never restart it automatically.
            failure.attributes["halt_requested"] = fresh.status.value
        now = cfg.clock.now()
        limit = cfg.max_failures_per_record
        stored = {_failure_key(f) for f in fresh.failures}
        for earlier in state.run.failures if state is not None else ():
            if _failure_key(earlier) not in stored:
                _append_failure(fresh.failures, earlier, limit)
        _append_failure(fresh.failures, failure, limit)
        fresh.status = BatchStatus.FAILED
        fresh.exit_status = ExitStatus.FAILED  # type: ignore[attr-defined]
        fresh.end_time = now
        fresh.last_updated = now
        fresh = await cfg.store.update_job_run(fresh, fresh.version)
        if state is not None and state.step_run is not None:
            try:
                active_step = await cfg.store.get_step_run(state.step_run.id)
            except KeyError:
                active_step = None
            if active_step is not None and active_step.status not in (
                BatchStatus.COMPLETED,
                BatchStatus.FAILED,
                BatchStatus.STOPPED,
                BatchStatus.ABANDONED,
            ):
                stored = {_failure_key(f) for f in active_step.failures}
                for earlier in state.step_run.failures:
                    if _failure_key(earlier) not in stored:
                        _append_failure(active_step.failures, earlier, limit)
                _append_failure(active_step.failures, failure, limit)
                active_step.status = BatchStatus.FAILED
                active_step.exit_status = ExitStatus.FAILED  # type: ignore[attr-defined]
                active_step.end_time = now
                active_step.updated_at = now
                await cfg.store.update_step_run(active_step, active_step.version)
        await self._set_instance_status(fresh.instance_id, BatchStatus.FAILED)
        if with_audit:
            await self._emit(
                AuditEventType.FAILED,
                fresh,
                step_name=failure.step_name,
                to_status=BatchStatus.FAILED,
                failure=failure,
                message=f"runner crashed: {failure.exception_type}: {failure.message}",
            )

    async def _load_owned_run(self, run_id: str) -> JobRun:
        """Re-read the run before a runner write, refusing if it was settled elsewhere.

        Runner writes adopt the latest stored version, so the optimistic check alone
        cannot stop a worker whose lease silently expired (a stalled event loop, a GC
        or VM pause) from overwriting the recovery sweep's FAILED verdict and running
        on beside the new owner. Callers use this inside their store transaction.
        """
        fresh = await self.cfg.store.get_job_run(run_id)
        if fresh.status not in _ACTIVE_RUN_STATUSES:
            raise LockAcquisitionError(
                f"run {run_id} was settled as {fresh.status.value} by another actor "
                "while this worker was advancing it; its lease was lost"
            )
        return fresh

    async def _lease_heartbeat(self, lock: Lock, lease_lost: asyncio.Event) -> None:
        """Keep the lease alive; declare it lost only when it really is (or may be).

        A refresh that reports the lease gone is final. A refresh that *errors* (a
        store blip) is retried until shortly before the lease could expire: giving up
        on the first error would abandon a run whose lease is still valid.
        """
        loop = asyncio.get_running_loop()
        ttl = self.cfg.lock_ttl.total_seconds()
        interval = max(0.05, ttl / 3)
        margin = ttl / 10  # stop retrying this long before the lease could lapse
        retry_delay = min(max(0.05, interval / 10), 5.0)
        # The lease was taken just before the runner started; anchor on this moment
        # and let ``margin`` absorb the gap.
        valid_until = loop.time() + ttl
        while True:
            await asyncio.sleep(interval)
            while True:
                attempted_at = loop.time()
                try:
                    refreshed = await lock.refresh(self.cfg.lock_ttl)
                except Exception:
                    remaining = valid_until - margin - loop.time()
                    if remaining <= 0:
                        logger.error(
                            "lock lease refresh kept failing until the lease could "
                            "expire; treating it as lost",
                            exc_info=True,
                        )
                        lease_lost.set()
                        return
                    logger.warning(
                        "lock lease refresh failed; retrying for up to %.1fs",
                        remaining,
                        exc_info=True,
                    )
                    await asyncio.sleep(min(retry_delay, remaining))
                    continue
                if not refreshed:
                    logger.error("lock lease was lost (refresh refused)")
                    lease_lost.set()
                    return
                valid_until = attempted_at + ttl
                break

    # -- per-step execution --
    async def _run_step(self, state: _Exec, step: Step, lease_lost: asyncio.Event) -> _StepOutcome:
        cfg = self.cfg
        # Every persist/transition replaces ``state.step_run`` with the stored copy, so
        # step bookkeeping below always goes through ``state.step_run``, never a local.
        state.step = step
        state.step_run = await self._open_step_run(state, step)
        raw_step_context = cfg.serializer.deserialize(state.step_run.execution_context)
        if not isinstance(raw_step_context, dict):
            raise TypeError("serializer.deserialize(step_run.execution_context) must return a dict")
        state.step_ctx = ExecutionContext(raw_step_context)

        await self._transition_step(state, BatchStatus.RUNNING, emit=AuditEventType.STEP_STARTED)
        control: dict[str, Any] = {
            "status": None,
            "event": asyncio.Event(),
        }
        monitor = asyncio.create_task(
            self._monitor_control_signal(state.run.id, control, lease_lost)
        )
        # Per-execution gate bookkeeping: ``ordinal`` numbers request_validation calls
        # deterministically (0, 1, …) so each gate finds its own decision on resume;
        # ``pause_signalled`` detects a swallowed PauseForValidation (see below).
        gate_state: dict[str, Any] = {
            "ordinal": 0,
            "pause_signalled": False,
            "pause_request": None,
        }
        ctx = self._make_step_context(state, control, lease_lost, gate_state)

        attempt = state.step_run.attempt
        skip_count = state.step_run.skip_count
        result: StepResult | None = None
        try:
            while True:
                ctx.attempt = attempt
                gate_state["ordinal"] = 0
                gate_state["pause_signalled"] = False
                gate_state["pause_request"] = None
                if lease_lost.is_set():
                    raise LockAcquisitionError(
                        f"lease lost for instance {state.run.instance_id}"
                    )
                if control["status"] in (
                    BatchStatus.STOPPING,
                    BatchStatus.PAUSING,
                ):
                    target = (
                        BatchStatus.PAUSED
                        if control["status"] is BatchStatus.PAUSING
                        else BatchStatus.STOPPED
                    )
                    await self._halt_step(state, target)
                    return _StepOutcome(halt_status=target)
                try:
                    result = await self._execute_step_with_lease_guard(
                        step, ctx, lease_lost
                    )
                    if lease_lost.is_set():
                        raise LockAcquisitionError(f"lease lost for instance {state.run.instance_id}")
                    if gate_state["pause_signalled"]:
                        # The step swallowed PauseForValidation and returned success.
                        # Honor the original gate instead of silently completing with
                        # an orphaned pending-marker in the checkpoint.
                        request = gate_state["pause_request"]
                        assert isinstance(request, ValidationRequest)
                        logger.warning(
                            "step %r (run %s) swallowed a pause signal and returned; "
                            "forcing the requested validation pause",
                            step.name,
                            state.run.id,
                        )
                        raise PauseForValidation(request)
                except PauseForValidation as pause:
                    await self._handle_pause(state, pause.request)
                    return _StepOutcome(validation_paused=True)
                except StopExecution as stop:
                    if lease_lost.is_set():
                        raise LockAcquisitionError(f"lease lost for instance {state.run.instance_id}")
                    signal = await self._external_signal(state)
                    target = signal or (
                        BatchStatus.PAUSED
                        if control["status"] is BatchStatus.PAUSING
                        else BatchStatus.STOPPED
                    )
                    await self._halt_step(state, target, message=str(stop))
                    return _StepOutcome(halt_status=target)
                except (LockAcquisitionError, OptimisticLockError):
                    raise
                except Exception as exc:  # business failure
                    if gate_state["pause_signalled"]:
                        # A PauseForValidation was raised this attempt but a plain
                        # exception arrived: the step almost certainly swallowed the
                        # signal in a broad `except Exception:` (see StepSignal docs).
                        logger.warning(
                            "step %r (run %s) raised %s after a pause signal was "
                            "issued this attempt; a PauseForValidation may have been "
                            "swallowed by a broad exception handler",
                            step.name,
                            state.run.id,
                            type(exc).__name__,
                        )
                        await self._emit(
                            AuditEventType.ANNOTATION,
                            state.run,
                            step_name=step.name,
                            message="pause signal possibly swallowed by the step",
                            attributes={"exception_type": type(exc).__name__},
                        )
                    failure = self._sanitize_failure(
                        FailureRecord.from_exception(exc, step_name=step.name, attempt=attempt)
                    )
                    self._classify_failure(exc, failure)
                    # Record before anything else can fail (audit sink, policies): the
                    # crash path keeps these in-memory failures when it settles the run.
                    _append_failure(state.step_run.failures, failure, cfg.max_failures_per_record)
                    _append_failure(state.run.failures, failure, cfg.max_failures_per_record)
                    await self._emit(
                        AuditEventType.STEP_FAILED,
                        state.run,
                        step_name=step.name,
                        failure=failure,
                        message=failure.message,
                    )
                    skip_policy = state.definition.skip_policy or cfg.skip_policy
                    if skip_policy.should_skip(failure, skip_count):
                        skip_count += 1
                        await self._emit(AuditEventType.ITEM_SKIPPED, state.run, step_name=step.name)
                        result = StepResult.completed(skip_count=1)
                        break
                    retry_policy = state.definition.retry_policy or cfg.retry_policy
                    decision = retry_policy.should_retry(failure, attempt)
                    if decision.retry:
                        await self._emit(
                            AuditEventType.RETRY_ATTEMPT,
                            state.run,
                            step_name=step.name,
                            message=decision.reason,
                            attributes={"attempt": attempt + 1},
                        )
                        attempt += 1
                        state.step_run.attempt = attempt
                        await self._persist_step(state)
                        self._restore_durable_contexts_after_failure(state, ctx)
                        if decision.delay.total_seconds() > 0:
                            try:
                                await asyncio.wait_for(
                                    control["event"].wait(),
                                    timeout=decision.delay.total_seconds(),
                                )
                            except TimeoutError:
                                pass
                        continue
                    state.step_run.exit_status = ExitStatus.FAILED  # type: ignore[attr-defined]
                    state.step_run.end_time = cfg.clock.now()
                    await self._transition_step(state, BatchStatus.FAILED, emit=None)
                    return _StepOutcome(failed=True, failure=failure)
                else:
                    break
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

        assert result is not None
        await self._complete_step(state, result)
        return _StepOutcome(exit_status=result.exit_status)

    def _restore_durable_contexts_after_failure(
        self,
        state: _Exec,
        ctx: StepContext,
    ) -> None:
        """Discard mutations made after the most recent durable checkpoint.

        A retry within the same process must observe the same contexts that a crash and
        restart would load. Otherwise a failed attempt can advance a cursor only in
        memory and cause the next attempt to skip work nondeterministically.
        """
        assert state.step_run is not None
        raw_run = self.cfg.serializer.deserialize(state.run.execution_context)
        raw_step = self.cfg.serializer.deserialize(state.step_run.execution_context)
        if not isinstance(raw_run, dict):
            raise TypeError(
                "serializer.deserialize(run.execution_context) must return a dict"
            )
        if not isinstance(raw_step, dict):
            raise TypeError(
                "serializer.deserialize(step execution_context) must return a dict"
            )
        state.run_ctx = ExecutionContext(raw_run)
        state.step_ctx = ExecutionContext(raw_step)
        ctx.run_context = state.run_ctx
        ctx.step_context = state.step_ctx

    async def _execute_step_with_lease_guard(
        self,
        step: Step,
        ctx: StepContext,
        lease_lost: asyncio.Event,
    ) -> StepResult:
        step_task = asyncio.create_task(self._execute_step(step, ctx))
        lease_task = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {step_task, lease_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_task in done and lease_lost.is_set() and not step_task.done():
                step_task.cancel()
                await asyncio.gather(step_task, return_exceptions=True)
                raise LockAcquisitionError(
                    f"lease lost for instance {ctx.instance_id}"
                )
            return await step_task
        except BaseException:
            if not step_task.done():
                step_task.cancel()
                # BlockingStep deliberately does not finish cancellation until its
                # underlying worker thread exits, keeping the instance lease held.
                await asyncio.gather(step_task, return_exceptions=True)
            raise
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)

    @staticmethod
    async def _execute_step(step: Step, ctx: StepContext) -> StepResult:
        """Run the step, failing it with its reason if its code calls ``sys.exit`` or
        raises ``KeyboardInterrupt``: asyncio re-raises those two out of the event loop,
        which would stop every run sharing it and record none of them."""
        try:
            return await step.execute(ctx)
        except (SystemExit, KeyboardInterrupt) as exc:
            raise StepExitError.from_exit(exc) from exc

    async def _monitor_control_signal(
        self, run_id: str, control: dict[str, Any], lease_lost: asyncio.Event
    ) -> None:
        interval = max(0.01, self.cfg.control_poll_interval.total_seconds())
        while True:
            await asyncio.sleep(interval)
            try:
                fresh = await self.cfg.store.get_job_run(run_id)
            except Exception:
                logger.warning(
                    "control monitor read failed for run %s; retrying",
                    run_id,
                    exc_info=True,
                )
                continue
            if fresh.status in (BatchStatus.STOPPING, BatchStatus.PAUSING):
                control["status"] = fresh.status
                control["event"].set()
                return
            if fresh.status in _TAKEN_OVER_RUN_STATUSES:
                # Another actor settled the run (typically the recovery sweep after
                # this worker's lease expired unnoticed): stop the step now rather
                # than let it run on beside the new owner.
                logger.warning(
                    "run %s was settled as %s by another actor; cancelling its step",
                    run_id,
                    fresh.status.value,
                )
                lease_lost.set()
                return

    async def _open_step_run(self, state: _Exec, step: Step) -> StepRun:
        """Return the step run to execute: reuse a non-completed one (resume/retry/pause),
        else create a fresh one seeded from a prior attempt's checkpoint (restart resume)."""
        cfg = self.cfg
        existing = await cfg.store.find_step_run(state.run.id, step.name)
        if existing is not None and existing.status != BatchStatus.COMPLETED:
            if state.run.current_step != step.name:
                state.run.current_step = step.name
                await self._persist_run_merging_control_signal(state)
            return existing
        prior = await cfg.store.find_last_step_run(state.run.instance_id, step.name)
        seed = prior.execution_context if (prior and prior.status != BatchStatus.COMPLETED) else {}
        decisions_discarded = False
        if prior is not None and seed and prior.run_id != state.run.id:
            seed, decisions_discarded = await self._restart_seed(state.run, prior)
        step_run = StepRun(
            id=cfg.ids.new_id(),
            run_id=state.run.id,
            instance_id=state.run.instance_id,
            step_name=step.name,
            status=BatchStatus.PENDING,
            execution_context=seed,
            start_time=cfg.clock.now(),
        )

        async def open_step() -> None:
            nonlocal step_run
            state.run = await self._load_owned_run(state.run.id)
            state.run.current_step = step.name
            await self._persist_run_merging_control_signal(state)
            step_run.version = 0
            step_run = await cfg.store.create_step_run(step_run)
            if decisions_discarded:
                assert prior is not None
                await self._emit(
                    AuditEventType.ANNOTATION,
                    state.run,
                    step_name=step.name,
                    message=(
                        "validation decisions from the prior attempt discarded: "
                        "the restart changed the run parameters"
                    ),
                    attributes={"prior_run_id": prior.run_id},
                )

        await cfg.store.run_in_transaction(open_step)
        return step_run

    async def _restart_seed(self, run: JobRun, prior: StepRun) -> tuple[object, bool]:
        """The step context a restarted run resumes from, and whether human decisions
        were dropped from it.

        Decisions recorded at the prior attempt's gates were given for that attempt's
        parameters. When the restart changed them, every gate must ask again, so the
        recorded outcomes and the pending-gate marker are discarded; the rest of the
        checkpoint (cursors, partial results) still carries over.
        """
        prior_run = await self.cfg.store.get_job_run(prior.run_id)
        if prior_run.parameters == run.parameters:
            return prior.execution_context, False
        raw = self.cfg.serializer.deserialize(prior.execution_context)
        if not isinstance(raw, dict):
            raise TypeError("serializer.deserialize(step execution_context) must return a dict")
        raw = dict(raw)
        discarded = raw.pop(ValidationOutcome.OUTCOMES_KEY, None) is not None
        raw.pop(ValidationOutcome.PENDING_GATE_KEY, None)
        return self.cfg.serializer.serialize(raw), discarded

    def _make_step_context(
        self,
        state: _Exec,
        control: dict[str, BatchStatus | None],
        lease_lost: asyncio.Event,
        gate_state: dict[str, Any],
    ) -> StepContext:
        cfg = self.cfg
        assert state.step is not None and state.step_run is not None and state.step_ctx is not None

        async def checkpoint_fn(message: str = "") -> None:
            await self._persist_checkpoint(state, message=message)

        async def request_validation_fn(**kwargs: Any) -> ValidationOutcome:
            assert state.step_ctx is not None
            # Gates are numbered in call order per execution; a re-executing step asks
            # for the same ordinals again, so each gate finds exactly its own decision
            # (never a later gate's) and only an undecided gate pauses.
            ordinal = gate_state["ordinal"]
            gate_state["ordinal"] += 1
            outcomes = state.step_ctx.get(ValidationOutcome.OUTCOMES_KEY) or {}
            blob = outcomes.get(str(ordinal))
            if blob is not None:
                return ValidationOutcome.from_context_blob(blob)
            sla = kwargs.get("sla_seconds")
            request = ValidationRequest(
                id=cfg.ids.new_id(),
                run_id=state.run.id,
                instance_id=state.run.instance_id,
                step_name=state.step.name,  # type: ignore[union-attr]
                reason=kwargs["reason"],
                payload=kwargs.get("payload") or {},
                decision_schema=kwargs.get("decision_schema") or {},
                required_role=kwargs.get("required_role"),
                sla_deadline=(cfg.clock.now() + timedelta(seconds=sla)) if sla else None,
            )
            # Record which gate is pausing; _prepare_resume keys the decision by it.
            state.step_ctx.put(
                ValidationOutcome.PENDING_GATE_KEY,
                {"ordinal": ordinal, "request_id": request.id},
            )
            gate_state["pause_signalled"] = True
            gate_state["pause_request"] = request
            raise PauseForValidation(request)

        async def emit_audit_fn(*, message: str, attributes: dict[str, Any]) -> None:
            await self._emit(
                AuditEventType.ANNOTATION,
                state.run,
                step_name=state.step.name,  # type: ignore[union-attr]
                message=message,
                attributes=attributes,
            )

        def should_stop_fn() -> bool:
            return control["status"] is not None or lease_lost.is_set()

        return StepContext(
            run_id=state.run.id,
            instance_id=state.run.instance_id,
            step_name=state.step.name,  # type: ignore[union-attr]
            attempt=state.step_run.attempt,  # type: ignore[union-attr]
            params=state.run.parameters,
            run_context=state.run_ctx,
            step_context=state.step_ctx,
            checkpoint_fn=checkpoint_fn,
            request_validation_fn=request_validation_fn,
            emit_audit_fn=emit_audit_fn,
            should_stop_fn=should_stop_fn,
            actor=state.run.requested_by or Actor.system(),
            fencing_token=state.fencing_token,
        )

    # -- pause / completion handling --
    async def _handle_pause(self, state: _Exec, request: ValidationRequest) -> None:
        cfg = self.cfg
        base_run = copy.deepcopy(state.run)
        base_step = copy.deepcopy(state.step_run)

        async def pause_state() -> None:
            state.run = copy.deepcopy(base_run)
            state.step_run = copy.deepcopy(base_step)
            request.version = 0
            await self._persist_checkpoint(state)
            await cfg.store.create_validation_request(request)
            assert state.step_run is not None
            state.step_run.status = BatchStatus.PAUSED
            state.step_run.exit_status = ExitStatus.PAUSED  # type: ignore[attr-defined]
            await self._persist_step(state)
            state.run.open_validation_id = request.id
            state.run.current_step = request.step_name
            await self._emit(
                AuditEventType.VALIDATION_REQUESTED,
                state.run,
                step_name=request.step_name,
                message=request.reason,
                attributes={"validation_request_id": request.id},
            )
            await self._transition_run(
                state,
                BatchStatus.AWAITING_VALIDATION,
                exit_status=ExitStatus.PAUSED,
                emit=AuditEventType.PAUSED,
            )
        await cfg.store.run_in_transaction(pause_state)

    async def _complete_step(self, state: _Exec, result: StepResult) -> None:
        assert state.step_run is not None and state.step_ctx is not None
        await self._persist_checkpoint(state)
        sr = state.step_run
        sr.exit_status = result.exit_status
        sr.read_count += result.read_count
        sr.write_count += result.write_count
        sr.skip_count += result.skip_count
        sr.commit_count += result.commit_count
        sr.attributes.update(result.attributes)
        sr.end_time = self.cfg.clock.now()
        await self._transition_step(
            state,
            BatchStatus.COMPLETED,
            exit_status=result.exit_status,
            emit=AuditEventType.STEP_COMPLETED,
            attributes=sr.attributes,
        )

    async def _halt_step(self, state: _Exec, status: BatchStatus, *, message: str = "") -> None:
        assert state.step_run is not None
        # A step that stopped itself said why (e.g. what it waits for): its checkpoint says so too.
        await self._persist_checkpoint(state, message=message)
        state.step_run.end_time = self.cfg.clock.now()
        exit_status = ExitStatus.STOPPED if status is BatchStatus.STOPPED else ExitStatus.PAUSED
        await self._transition_step(state, status, exit_status=exit_status, emit=None)

    async def _complete(self, state: _Exec) -> None:
        await self._transition_run(
            state,
            BatchStatus.COMPLETED,
            exit_status=ExitStatus.COMPLETED,
            emit=AuditEventType.COMPLETED,  # type: ignore[attr-defined]
        )

    async def _fail_run(self, state: _Exec, failure: FailureRecord | None) -> None:
        await self._transition_run(
            state,
            BatchStatus.FAILED,
            exit_status=ExitStatus.FAILED,
            emit=AuditEventType.FAILED,
            failure=failure,  # type: ignore[attr-defined]
        )

    # -- persistence helpers --
    def _sanitize_failure(self, failure: FailureRecord) -> FailureRecord:
        """Scrub then bound every failure text before it reaches the store/audit trail."""
        cfg = self.cfg
        failure.stack_trace = self._clean_text(failure.stack_trace, cfg.max_stack_trace_length)
        failure.message = self._clean_text(failure.message, cfg.max_failure_message_length)
        failure.cause_chain = [
            self._clean_text(cause, cfg.max_failure_message_length)
            for cause in failure.cause_chain[:_MAX_CAUSE_CHAIN]
        ]
        return failure

    def _classify_failure(self, exc: BaseException, failure: FailureRecord) -> None:
        """Let ``EtfConfig.failure_classifier`` mark a step failure (retryable,
        attributes). A classifier that raises is logged and ignored."""
        classifier = self.cfg.failure_classifier
        if classifier is None:
            return
        try:
            classifier(exc, failure)
        except Exception:
            logger.exception("failure_classifier raised; recording the failure unclassified")

    def _clean_text(self, text: str, limit: int | None) -> str:
        scrubber = self.cfg.stack_trace_scrubber
        if scrubber is not None and text:
            try:
                text = scrubber(text)
            except Exception:
                logger.exception(
                    "stack_trace_scrubber failed; withholding the text rather than "
                    "storing it unscrubbed"
                )
                return "[withheld: stack_trace_scrubber failed]"
        return _truncate_middle(text, limit)

    async def _persist_checkpoint(self, state: _Exec, *, message: str = "") -> None:
        cfg = self.cfg
        desired_run = copy.deepcopy(state.run)
        desired_run.execution_context = cfg.serializer.serialize(
            state.run_ctx.as_dict()
        )
        desired_step = copy.deepcopy(state.step_run)
        if desired_step is not None and state.step_ctx is not None:
            desired_step.execution_context = cfg.serializer.serialize(
                state.step_ctx.as_dict()
            )

        async def persist_checkpoint() -> None:
            fresh_run = await self._load_owned_run(desired_run.id)
            candidate_run = copy.deepcopy(desired_run)
            candidate_run.version = fresh_run.version
            if fresh_run.status in (BatchStatus.STOPPING, BatchStatus.PAUSING):
                candidate_run.status = fresh_run.status
            state.run = candidate_run
            await self._persist_run_merging_control_signal(state)
            if desired_step is not None:
                fresh_step = await cfg.store.get_step_run(desired_step.id)
                candidate_step = copy.deepcopy(desired_step)
                candidate_step.version = fresh_step.version
                state.step_run = candidate_step
                await self._persist_step(state)
            await self._emit(
                AuditEventType.CHECKPOINT_SAVED,
                state.run,
                step_name=state.step.name if state.step else None,
                message=message,
            )
        await cfg.store.run_in_transaction(persist_checkpoint)

    async def _persist_run(self, state: _Exec) -> None:
        state.run.last_updated = self.cfg.clock.now()
        state.run = await self.cfg.store.update_job_run(state.run, state.run.version)

    async def _persist_run_merging_control_signal(self, state: _Exec) -> None:
        try:
            await self._persist_run(state)
        except OptimisticLockError:
            fresh = await self.cfg.store.get_job_run(state.run.id)
            if fresh.status not in (BatchStatus.STOPPING, BatchStatus.PAUSING):
                raise
            state.run.version = fresh.version
            state.run.status = fresh.status
            await self._persist_run(state)

    async def _persist_step(self, state: _Exec) -> None:
        assert state.step_run is not None
        state.step_run.updated_at = self.cfg.clock.now()
        state.step_run = await self.cfg.store.update_step_run(state.step_run, state.step_run.version)

    async def _transition_run(
        self,
        state: _Exec,
        status: BatchStatus,
        *,
        exit_status: ExitStatus | None = None,
        emit: AuditEventType | None,
        failure: FailureRecord | None = None,
    ) -> None:
        cfg = self.cfg
        old = state.run.status
        if status is BatchStatus.RUNNING and state.run.start_time is None:
            state.run.start_time = cfg.clock.now()
        if status.is_terminal or status in (BatchStatus.STOPPED, BatchStatus.FAILED):
            state.run.end_time = cfg.clock.now()
        if exit_status is not None:
            state.run.exit_status = exit_status
        state.run.status = status
        desired_run = copy.deepcopy(state.run)

        async def transition_run() -> None:
            nonlocal status, emit, exit_status
            # A stop/pause requested while the run was settling turns a stop-able
            # outcome into STOPPED/PAUSED. A failure is kept: the run ends FAILED
            # (restartable either way) with its reason, and the request is noted.
            halt_requested: BatchStatus | None = None

            def adopt_halt(requested: BatchStatus) -> None:
                nonlocal status, emit, exit_status, halt_requested
                if status is BatchStatus.FAILED:
                    halt_requested = requested
                    return
                status = (
                    BatchStatus.STOPPED
                    if requested is BatchStatus.STOPPING
                    else BatchStatus.PAUSED
                )
                emit = (
                    AuditEventType.STOPPED
                    if status is BatchStatus.STOPPED
                    else AuditEventType.PAUSED
                )
                exit_status = (
                    ExitStatus.STOPPED
                    if status is BatchStatus.STOPPED
                    else ExitStatus.PAUSED
                )

            fresh = await self._load_owned_run(desired_run.id)
            candidate = copy.deepcopy(desired_run)
            candidate.version = fresh.version
            if fresh.status in (BatchStatus.STOPPING, BatchStatus.PAUSING):
                adopt_halt(fresh.status)
                candidate.status = status
                if exit_status is not None:
                    candidate.exit_status = exit_status
                candidate.end_time = cfg.clock.now()
            state.run = candidate
            try:
                await self._persist_run(state)
            except OptimisticLockError:
                fresh = await cfg.store.get_job_run(state.run.id)
                if fresh.status not in (BatchStatus.STOPPING, BatchStatus.PAUSING):
                    raise
                adopt_halt(fresh.status)
                state.run.version = fresh.version
                state.run.status = status
                assert exit_status is not None
                state.run.exit_status = exit_status
                state.run.end_time = cfg.clock.now()
                await self._persist_run(state)
            if status in {
                BatchStatus.RUNNING,
                BatchStatus.PAUSED,
                BatchStatus.AWAITING_VALIDATION,
                BatchStatus.STOPPED,
                BatchStatus.COMPLETED,
                BatchStatus.FAILED,
                BatchStatus.ABANDONED,
            }:
                await self._set_instance_status(state.run.instance_id, status)
            if emit is not None:
                await self._emit(
                    emit,
                    state.run,
                    step_name=failure.step_name if failure is not None else None,
                    from_status=old,
                    to_status=status,
                    exit_status=exit_status,
                    failure=failure,
                    message=(
                        f"{failure.exception_type}: {failure.message}"
                        if failure is not None
                        else ""
                    ),
                    attributes=(
                        {"halt_requested": halt_requested.value}
                        if halt_requested is not None
                        else None
                    ),
                )
        await cfg.store.run_in_transaction(transition_run)

    async def _transition_step(
        self,
        state: _Exec,
        status: BatchStatus,
        *,
        exit_status: ExitStatus | None = None,
        emit: AuditEventType | None,
        attributes: dict[str, object] | None = None,
    ) -> None:
        assert state.step_run is not None
        old = state.step_run.status
        if status is BatchStatus.RUNNING and state.step_run.start_time is None:
            state.step_run.start_time = self.cfg.clock.now()
        if exit_status is not None:
            state.step_run.exit_status = exit_status
        state.step_run.status = status
        desired_step = copy.deepcopy(state.step_run)

        async def transition_step() -> None:
            await self._load_owned_run(state.run.id)
            fresh = await self.cfg.store.get_step_run(desired_step.id)
            candidate = copy.deepcopy(desired_step)
            candidate.version = fresh.version
            state.step_run = candidate
            await self._persist_step(state)
            if emit is not None:
                await self._emit(
                    emit,
                    state.run,
                    step_name=state.step_run.step_name,
                    from_status=old,
                    to_status=status,
                    exit_status=exit_status,
                    attributes=dict(attributes or {}),
                )
        await self.cfg.store.run_in_transaction(transition_step)

    async def _set_instance_status(self, instance_id: str, status: BatchStatus) -> None:
        for attempt in range(3):
            instance = await self.cfg.store.get_job_instance(instance_id)
            instance.status = status
            instance.updated_at = self.cfg.clock.now()
            try:
                await self.cfg.store.update_job_instance(instance, instance.version)
                return
            except OptimisticLockError:
                if attempt == 2:
                    raise

    async def _external_signal(self, state: _Exec) -> BatchStatus | None:
        """Best-effort check for an out-of-band stop/pause requested via the operator.

        Honored at step boundaries. Mid-step cooperative cancellation is the step's
        responsibility via ``ctx.should_stop()``. Raises ``LockAcquisitionError`` if
        another actor settled the run, so no further step starts on a stale lease.
        """
        fresh = await self._load_owned_run(state.run.id)
        state.run.version = fresh.version  # keep optimistic token current
        if fresh.status is BatchStatus.STOPPING:
            state.run.status = fresh.status
            return BatchStatus.STOPPED
        if fresh.status is BatchStatus.PAUSING:
            state.run.status = fresh.status
            return BatchStatus.PAUSED
        return None

    async def _emit(
        self,
        event_type: AuditEventType,
        run: JobRun,
        *,
        step_name: str | None = None,
        from_status: BatchStatus | None = None,
        to_status: BatchStatus | None = None,
        exit_status: ExitStatus | None = None,
        failure: FailureRecord | None = None,
        message: str = "",
        actor: Actor | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        cfg = self.cfg
        seq = await cfg.store.next_audit_sequence(run.id)
        event = AuditEvent(
            id=cfg.ids.new_id(),
            event_type=event_type,
            run_id=run.id,
            instance_id=run.instance_id,
            sequence=seq,
            at=cfg.clock.now(),
            actor=actor or run.requested_by or Actor.system(),
            step_name=step_name,
            from_status=from_status,
            to_status=to_status,
            exit_status=exit_status,
            failure=failure,
            message=message,
            attributes=attributes or {},
            correlation_id=run.correlation_id,
            trace_id=run.trace_id,
        )
        await cfg.audit.emit(event)


# --------------------------------------------------------------------------- #
# Launcher
# --------------------------------------------------------------------------- #
class JobLauncher:
    """Entry point for new runs. Enforces idempotency, then delegates to the runner."""

    def __init__(self, cfg: EtfConfig, runner: JobRunner | None = None) -> None:
        self.cfg = cfg
        self.runner = runner or JobRunner(cfg)
        self._operator = JobOperator(cfg, self.runner)

    async def launch(self, request: "LaunchRequest") -> JobRun:
        """Run the job for ``request`` to its next durable state and return the run.

        A delivery for an instance that already has runs never starts a duplicate:
        a paused run is returned as is, a pending one is driven, one nobody owns any
        more (its worker died or lost its lease) is settled — and, when its worker
        went away without a verdict, restarted as the next attempt (up to
        ``EtfConfig.max_auto_redrives`` in a row). A completed instance raises
        ``JobInstanceAlreadyCompleteError``; a failed or stopped run that needs a
        decision raises ``RunNotRestartableError``.
        """
        cfg = self.cfg
        definition = cfg.registry.get(request.job_name)
        run_id = cfg.ids.new_id()
        key = request.idempotency_key
        reservation_owned = False
        reservation_committed = False
        if key:
            # A duplicate delivery finds the key reserved but no run yet (the winner is
            # still creating it). Poll with exponential backoff until the run appears
            # or the budget elapses — run creation can easily take longer than a blink.
            deadline = asyncio.get_running_loop().time() + cfg.idempotency_poll_budget.total_seconds()
            delay = 0.05
            while True:
                existing = await cfg.store.register_idempotency_key(
                    key, run_id, ttl=cfg.idempotency_reservation_ttl
                )
                if existing is None:
                    reservation_owned = True
                    break
                try:
                    return await self._continue_existing_delivery(existing, request)
                except KeyError:
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.5)
            if not reservation_owned:
                raise JobAlreadyRunningError(f"idempotency key {key!r} is currently being committed")

        lock: Lock | None = None
        lock_consumed = False
        try:
            resolver = definition.instance_resolver or cfg.instance_resolver
            # Identity is independent of the definition version, so work completed
            # under one version is never repeated after an upgrade.
            identity = resolver.resolve_identity(request.job_name, request.parameters)
            instance = await cfg.store.find_job_instance(request.job_name, identity)
            if instance is None:
                instance = JobInstance(
                    id=cfg.ids.new_id(),
                    job_name=request.job_name,
                    identity_hash=identity,
                    definition_version=definition.version,
                    identifying_parameters=dict(request.parameters.identifying),
                    labels=check_labels(request.labels),
                    status=BatchStatus.PENDING,
                )
                try:
                    instance = await cfg.store.create_job_instance(instance)
                except JobInstanceAlreadyExistsError:
                    instance = await cfg.store.find_job_instance(request.job_name, identity)
            assert instance is not None
            instance_id = instance.id

            lock = await cfg.lock_provider.acquire(instance_id, cfg.worker_id, cfg.lock_ttl)
            if lock is None:
                raise LockAcquisitionError(f"instance {instance.id} is being advanced by another worker")
            instance = await cfg.store.get_job_instance(instance.id)
            latest = await cfg.store.find_latest_run(instance.id)

            if instance.status in (BatchStatus.COMPLETED, BatchStatus.ABANDONED) or (
                latest and latest.status in (BatchStatus.COMPLETED, BatchStatus.ABANDONED)
            ):
                if latest is not None:
                    raise JobInstanceAlreadyCompleteError(
                        f"instance {instance.id} already ended with {latest.status.value} "
                        f"(run {latest.id}); not re-running"
                    )
            if latest is not None:
                if latest.status in (BatchStatus.PAUSED, BatchStatus.AWAITING_VALIDATION):
                    # Durably waiting (for a resume or a human decision): nothing lost.
                    if key:
                        reservation_committed = await self._rebind(key, run_id, latest.id)
                    return latest
                if latest.status is BatchStatus.PENDING:
                    if key:
                        reservation_committed = await self._rebind(key, run_id, latest.id)
                    lock_consumed = True
                    return await self.runner.run(latest, lock=lock)
                if latest.status in (
                    BatchStatus.RUNNING,
                    BatchStatus.STOPPING,
                    BatchStatus.PAUSING,
                    BatchStatus.FAILED,
                    BatchStatus.STOPPED,
                ):
                    lock_consumed = True
                    result = await self._operator._take_over(
                        latest,
                        lock,
                        idempotency_key=key,
                        reservation_run_id=run_id if key else None,
                    )
                    reservation_committed = bool(key)
                    return result
                raise EtfError(
                    f"instance {instance.id} has an unsupported latest run state "
                    f"{latest.status.value}; recover or repair run {latest.id} before launching"
                )

            run = JobRun(
                id=run_id,
                instance_id=instance.id,
                job_name=request.job_name,
                definition_version=definition.version,
                definition_fingerprint=definition.fingerprint,
                parameters=request.parameters,
                status=BatchStatus.PENDING,
                attempt=1,
                requested_by=request.requested_by,
                correlation_id=request.correlation_id,
                trace_id=request.trace_id,
                idempotency_key=key,
            )
            async def create_run_state() -> None:
                nonlocal run, instance
                # The callback may be replayed after an aborted Mongo transaction.
                # Re-read the optimistic token rather than reusing a mutated domain
                # object from the previous attempt.
                instance = await cfg.store.get_job_instance(instance_id)
                run.version = 0
                run = await cfg.store.create_job_run(run)
                if key:
                    rebound = await cfg.store.rebind_idempotency_key(key, run_id, run.id, cfg.idempotency_ttl)
                    if not rebound:
                        raise EtfError(f"lost idempotency reservation for {key!r}")
                instance.status = BatchStatus.RUNNING
                instance.definition_version = definition.version
                instance.updated_at = cfg.clock.now()
                await cfg.store.update_job_instance(instance, instance.version)
                await self.runner._emit(
                    AuditEventType.RUN_CREATED,
                    run,
                    message=f"source={request.source}",
                    attributes={"attempt": 1},
                )
            await cfg.store.run_in_transaction(create_run_state)
            if key:
                reservation_committed = True
            lock_consumed = True
            return await self.runner.run(run, lock=lock)
        except Exception:
            if key and reservation_owned and not reservation_committed:
                await cfg.store.release_idempotency_key(key, run_id)
            raise
        finally:
            if lock is not None and not lock_consumed:
                await lock.release()

    async def _continue_existing_delivery(self, run_id: str, request: "LaunchRequest") -> JobRun:
        """Resolve an idempotency-key hit without treating every state as success.

        The delivery is served by the instance's latest attempt (a restart or re-drive
        may have followed the run the key first pointed at). A run a live worker is
        advancing is returned; one nobody owns is settled and, if interrupted,
        re-driven — exactly as :meth:`launch` does for a delivery without a key.
        """
        cfg = self.cfg
        run = await cfg.store.get_job_run(run_id)
        if run.job_name != request.job_name or run.parameters != request.parameters:
            raise EtfError(
                f"idempotency key {request.idempotency_key!r} was already used "
                "for a different launch request"
            )
        run = await cfg.store.find_latest_run(run.instance_id) or run
        if run.status in (
            BatchStatus.PAUSED,
            BatchStatus.AWAITING_VALIDATION,
            BatchStatus.COMPLETED,
            BatchStatus.ABANDONED,
        ):
            return run

        lock = await cfg.lock_provider.acquire(run.instance_id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            if run.status in (BatchStatus.RUNNING, BatchStatus.STOPPING, BatchStatus.PAUSING):
                return run  # a live worker is advancing it
            raise LockAcquisitionError(
                f"run {run.id} ({run.status.value}) is being advanced by another worker"
            )
        consumed = False
        try:
            run = await cfg.store.find_latest_run(run.instance_id) or run
            if run.status is BatchStatus.PENDING:
                consumed = True
                return await self.runner.run(run, lock=lock)
            if run.status in (
                BatchStatus.RUNNING,
                BatchStatus.STOPPING,
                BatchStatus.PAUSING,
                BatchStatus.FAILED,
                BatchStatus.STOPPED,
            ):
                consumed = True
                return await self._operator._take_over(
                    run,
                    lock,
                    idempotency_key=request.idempotency_key,
                    reservation_run_id=run_id,
                )
            return run
        finally:
            if not consumed:
                await lock.release()

    async def _rebind(self, key: str, reservation_run_id: str, target_run_id: str) -> bool:
        """Point an owned reservation at the run that actually serves this delivery.

        Returns whether the rebind took; a lost reservation (stolen after TTL expiry)
        is logged and left for its new owner rather than treated as an error.
        """
        rebound = await self.cfg.store.rebind_idempotency_key(
            key, reservation_run_id, target_run_id, self.cfg.idempotency_ttl
        )
        if not rebound:
            logger.warning(
                "idempotency reservation for %r was lost before commit; "
                "the key now belongs to another launch",
                key,
            )
        return rebound


# --------------------------------------------------------------------------- #
# Operator (management / control API)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RunStatusView:
    run: JobRun
    steps: list[StepRun]
    open_validation: ValidationRequest | None


class JobOperator:
    """Management API: pause, resume, validate, restart, stop, abandon, plus queries.

    Mirrors Spring Batch's ``JobOperator``, extended with human-in-the-loop validation
    and audit access. See ``docs/DESIGN.md`` §4.3–4.5.
    """

    def __init__(self, cfg: EtfConfig, runner: JobRunner | None = None) -> None:
        self.cfg = cfg
        self.runner = runner or JobRunner(cfg)

    # -- human-in-the-loop --
    async def submit_validation_decision(self, request_id: str, decision: ValidationDecision) -> JobRun:
        """Resolve an open validation gate and drive the run to its next durable state.

        APPROVE/OVERRIDE record the decision for the paused gate and re-execute the
        step (the gate returns the decision instead of pausing). REJECT stops the run
        (restartable). RETRY discards the gate entirely: the step re-executes fresh and
        pauses again with a *new* validation request (the resolved request is recorded
        as APPROVED with the RETRY decision in the audit trail).
        """
        cfg = self.cfg
        decision.request_id = request_id
        request = await cfg.store.get_validation_request(request_id)
        run = await cfg.store.get_job_run(request.run_id)
        await self._authorize(
            "decide",
            decision.actor,
            run,
            {
                "validation_request_id": request_id,
                "decision": decision.decision.value,
                "parameter_overrides": dict(decision.parameter_overrides),
            },
        )
        lock = await cfg.lock_provider.acquire(run.instance_id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            raise LockAcquisitionError(f"instance {run.instance_id} is currently active")
        consumed = False
        try:
            request = await cfg.store.get_validation_request(request_id)
            run = await cfg.store.get_job_run(request.run_id)
            self.runner._definition_for(run)
            if request.status is not ValidationStatus.PENDING:
                if not self._same_validation_decision(request, decision):
                    raise ValidationStateError(
                        f"validation request {request.id} is already "
                        f"{request.status.value}"
                    )
                if run.status is BatchStatus.AWAITING_VALIDATION:
                    if run.open_validation_id != request.id:
                        raise ValidationStateError(
                            f"run {run.id} is awaiting a different validation request"
                        )
                    # A non-transactional store may have committed the request before
                    # failing to resume the run. Fall through to the idempotent repair
                    # callback below.
                elif run.status is BatchStatus.RUNNING and run.open_validation_id is None:
                    # The resume state committed but the original caller died before
                    # handing the run to the runner. Re-enter it under the acquired lock.
                    pass
                else:
                    return run
            elif (
                run.status is not BatchStatus.AWAITING_VALIDATION
                or run.open_validation_id != request.id
            ):
                raise ValidationStateError(f"run {run.id} is not awaiting validation request {request.id}")
            else:
                try:
                    self._validate_decision(request, decision)
                except (ValidationAuthorizationError, ValidationSchemaError) as exc:
                    await self.runner._emit(
                        AuditEventType.ANNOTATION,
                        run,
                        step_name=request.step_name,
                        actor=decision.actor,
                        message=f"validation decision rejected: {exc}",
                        attributes={
                            "validation_request_id": request.id,
                            "decision": decision.decision.value,
                            "rejected": type(exc).__name__,
                        },
                    )
                    raise

            async def apply_decision() -> None:
                nonlocal request, run, proceed
                proceed = False
                request = await cfg.store.get_validation_request(request_id)
                run = await cfg.store.get_job_run(request.run_id)
                if request.status is not ValidationStatus.PENDING:
                    if not self._same_validation_decision(request, decision):
                        raise ValidationStateError(
                            f"validation request {request.id} is already "
                            f"{request.status.value}"
                        )
                    if (
                        run.status is BatchStatus.AWAITING_VALIDATION
                        and run.open_validation_id == request.id
                    ):
                        run, proceed = await self._prepare_resume(run, decision)
                        return
                    if (
                        run.status is BatchStatus.RUNNING
                        and run.open_validation_id is None
                    ):
                        proceed = True
                        return
                    return
                if (
                    run.status is not BatchStatus.AWAITING_VALIDATION
                    or run.open_validation_id != request.id
                ):
                    raise ValidationStateError(
                        f"run {run.id} is not awaiting validation request "
                        f"{request.id}"
                    )
                self._validate_decision(request, decision)
                request.status = (
                    ValidationStatus.APPROVED
                    if decision.decision is not ValidationDecisionType.REJECT
                    else ValidationStatus.REJECTED
                )
                request.decided_at = cfg.clock.now()
                request.decision_type = decision.decision
                request.decision_actor_id = decision.actor.id
                request.decision_comment = decision.comment
                request.decision_overrides = dict(decision.parameter_overrides)
                request = await cfg.store.update_validation_request(request, request.version)
                await self.runner._emit(
                    AuditEventType.VALIDATION_DECISION,
                    run,
                    step_name=request.step_name,
                    actor=decision.actor,
                    message=decision.comment,
                    attributes={
                        "decision": decision.decision.value,
                        "validation_request_id": request_id,
                    },
                )
                run, proceed = await self._prepare_resume(run, decision)
            proceed = False
            await cfg.store.run_in_transaction(apply_decision)
            consumed = proceed
            if not proceed:
                return run
            return await self.runner.run(run, lock=lock)
        finally:
            if not consumed:
                await lock.release()

    @staticmethod
    def _same_validation_decision(
        request: ValidationRequest, decision: ValidationDecision
    ) -> bool:
        """Return whether a resolved gate is the same retried delivery."""
        return (
            request.decision_type is decision.decision
            and request.decision_actor_id == decision.actor.id
            and request.decision_comment == decision.comment
            and request.decision_overrides == decision.parameter_overrides
        )

    async def resume(
        self,
        run_id: str,
        decision: ValidationDecision | None = None,
        parameter_overrides: dict[str, Any] | None = None,
        *,
        actor: Actor | None = None,
    ) -> JobRun:
        """Resume a paused/stopped run. If awaiting validation, routes through the open gate.

        A run awaiting validation needs an explicit ``decision`` (made by the person
        deciding): a plain resume never approves a gate on anyone's behalf.
        """
        cfg = self.cfg
        run = await cfg.store.get_job_run(run_id)
        self.runner._definition_for(run)
        if run.status is BatchStatus.AWAITING_VALIDATION and run.open_validation_id:
            if decision is None:
                raise ValidationStateError(
                    f"run {run.id} is awaiting validation request "
                    f"{run.open_validation_id}; decide it with submit_validation_decision "
                    "(or pass decision=) instead of resuming it"
                )
            dec = decision
            if parameter_overrides:
                dec = ValidationDecision.override(dec.actor, parameter_overrides, dec.comment)
            return await self.submit_validation_decision(run.open_validation_id, dec)
        if run.status not in (BatchStatus.PAUSED, BatchStatus.STOPPED):
            raise ValidationStateError(f"run {run.id} ({run.status.value}) is not resumable")
        dec = decision or ValidationDecision.approve(actor or Actor.system())
        if parameter_overrides:
            dec = ValidationDecision.override(dec.actor, parameter_overrides, dec.comment)
        await self._authorize(
            "resume",
            dec.actor,
            run,
            {"parameter_overrides": dict(parameter_overrides or {})},
        )
        lock = await cfg.lock_provider.acquire(run.instance_id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            raise LockAcquisitionError(f"instance {run.instance_id} is currently active")
        run = await cfg.store.get_job_run(run.id)
        if run.status not in (BatchStatus.PAUSED, BatchStatus.STOPPED):
            await lock.release()
            raise ValidationStateError(f"run {run.id} ({run.status.value}) is not resumable")
        return await self._apply_resume(run, dec, lock)

    async def _apply_resume(self, run: JobRun, decision: ValidationDecision, lock: Lock) -> JobRun:
        cfg = self.cfg
        passed_to_runner = False
        try:
            async def prepare() -> None:
                nonlocal run, proceed
                run = await cfg.store.get_job_run(run.id)
                await self._assert_generic_resume_allowed(run)
                run, proceed = await self._prepare_resume(run, decision)
            proceed = False
            await cfg.store.run_in_transaction(prepare)
            if not proceed:
                return run
            passed_to_runner = True
            return await self.runner.run(run, lock=lock)
        finally:
            if not passed_to_runner:
                await lock.release()

    async def _assert_generic_resume_allowed(self, run: JobRun) -> None:
        """Reject generic resume of a gate that already ended REJECTED or EXPIRED.

        Those outcomes deliberately stop the run. Restarting may reopen the step and a
        fresh gate, but plain ``resume()`` must not manufacture an unvalidated approval
        from the stale pending-gate checkpoint marker.
        """
        if not run.current_step:
            return
        sr = await self.cfg.store.find_step_run(run.id, run.current_step)
        if sr is None or sr.status is BatchStatus.COMPLETED:
            return
        raw = self.cfg.serializer.deserialize(sr.execution_context)
        if not isinstance(raw, dict):
            raise TypeError(
                "serializer.deserialize(step execution_context) must return a dict"
            )
        pending = raw.get(ValidationOutcome.PENDING_GATE_KEY)
        if not isinstance(pending, dict) or not pending.get("request_id"):
            return
        try:
            request = await self.cfg.store.get_validation_request(
                str(pending["request_id"])
            )
        except KeyError:
            return
        if request.status in (
            ValidationStatus.REJECTED,
            ValidationStatus.EXPIRED,
        ):
            raise ValidationStateError(
                f"validation request {request.id} was "
                f"{request.status.value.lower()}; restart the run to open a new gate"
            )

    async def _prepare_resume(self, run: JobRun, decision: ValidationDecision) -> tuple[JobRun, bool]:
        cfg = self.cfg
        if decision.parameter_overrides:
            before = dict(run.parameters.non_identifying)
            run.parameters = run.parameters.with_overrides(decision.parameter_overrides)
            run = await cfg.store.update_job_run(run, run.version)
            await self.runner._emit(
                AuditEventType.PARAMETERS_OVERRIDDEN,
                run,
                actor=decision.actor,
                attributes={"before": before, "after": run.parameters.non_identifying},
            )

        if decision.decision is ValidationDecisionType.REJECT:
            run = await self._halt_awaiting_run(run, actor=decision.actor, message="rejected at validation")
            return run, False

        if run.current_step:
            sr = await cfg.store.find_step_run(run.id, run.current_step)
            if sr is not None and sr.status != BatchStatus.COMPLETED:
                raw = cfg.serializer.deserialize(sr.execution_context)
                if not isinstance(raw, dict):
                    raise TypeError("serializer.deserialize(step execution_context) must return a dict")
                pending = raw.pop(ValidationOutcome.PENDING_GATE_KEY, None)
                if decision.decision is ValidationDecisionType.RETRY:
                    # RETRY: record nothing for the gate. The step re-executes fresh
                    # and the (still undecided) gate pauses again with a new request.
                    sr.status = BatchStatus.PENDING
                    sr.exit_status = ExitStatus.EXECUTING  # type: ignore[attr-defined]
                elif pending is not None:
                    # Key the decision by the gate that actually paused, so a step
                    # with several gates returns each one its own outcome (T5).
                    outcomes = raw.setdefault(ValidationOutcome.OUTCOMES_KEY, {})
                    outcomes[str(pending["ordinal"])] = ValidationOutcome.to_context_blob(decision)
                sr.execution_context = cfg.serializer.serialize(raw)
                await cfg.store.update_step_run(sr, sr.version)

        run.open_validation_id = None
        run.status = BatchStatus.RUNNING
        run = await cfg.store.update_job_run(run, run.version)
        await self.runner._set_instance_status(run.instance_id, BatchStatus.RUNNING)
        await self.runner._emit(AuditEventType.RESUMED, run, actor=decision.actor)
        return run, True

    async def _halt_awaiting_run(self, run: JobRun, *, actor: Actor | None, message: str) -> JobRun:
        """Move a run out of AWAITING_VALIDATION/PAUSED into STOPPED (restartable),
        stopping its in-flight step run and syncing the instance status."""
        cfg = self.cfg
        run.status = BatchStatus.STOPPED
        run.open_validation_id = None
        run.end_time = cfg.clock.now()
        run = await cfg.store.update_job_run(run, run.version)
        if run.current_step:
            sr = await cfg.store.find_step_run(run.id, run.current_step)
            if sr is not None and sr.status is not BatchStatus.COMPLETED:
                sr.status = BatchStatus.STOPPED
                sr.exit_status = ExitStatus.STOPPED  # type: ignore[attr-defined]
                sr.end_time = cfg.clock.now()
                await cfg.store.update_step_run(sr, sr.version)
        await self.runner._set_instance_status(run.instance_id, BatchStatus.STOPPED)
        await self.runner._emit(AuditEventType.STOPPED, run, actor=actor, message=message)
        return run

    @staticmethod
    def _validate_decision(request: ValidationRequest, decision: ValidationDecision) -> None:
        if request.required_role and request.required_role not in decision.actor.roles:
            raise ValidationAuthorizationError(
                f"actor {decision.actor.id!r} lacks required role {request.required_role!r}"
            )
        if request.decision_schema:
            payload = {
                "decision": decision.decision.value,
                "comment": decision.comment,
                "parameter_overrides": decision.parameter_overrides,
                "actor_id": decision.actor.id,
            }
            _validate_schema_value(payload, request.decision_schema, "decision")

    # -- authorization --
    async def _authorize(
        self,
        name: str,
        actor: Actor,
        run: JobRun,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Ask ``EtfConfig.authorizer`` whether ``actor`` may apply action ``name``.

        A denial (the authorizer raising) is audited on the run and re-raised.
        """
        authorizer = self.cfg.authorizer
        if authorizer is None:
            return
        action = OperatorAction(
            name=name,
            actor=actor,
            run_id=run.id,
            instance_id=run.instance_id,
            job_name=run.job_name,
            details=dict(details or {}),
        )
        try:
            outcome = authorizer(action)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            await self.runner._emit(
                AuditEventType.ANNOTATION,
                run,
                actor=actor,
                message=f"{name} denied: {exc}",
                attributes={"action": name, "denied": True},
            )
            raise

    # -- restart with optional config changes --
    async def restart(
        self,
        instance_or_run_id: str,
        parameter_overrides: dict[str, Any] | None = None,
        from_step: str | None = None,
        *,
        actor: Actor | None = None,
        definition_version: int | None = None,
    ) -> JobRun:
        """Start the next attempt of a FAILED or STOPPED instance and drive it.

        Completed steps are skipped; the failed step resumes from its checkpoint.
        ``parameter_overrides`` change non-identifying parameters. The attempt runs on
        the definition version of the last run unless ``definition_version`` moves the
        instance to another registered version (steps are matched by name, so
        completed steps that still exist are skipped there too).
        """
        cfg = self.cfg
        actor = actor or Actor.system()
        instance = await self._resolve_instance(instance_or_run_id)
        last = await cfg.store.find_latest_run(instance.id)
        if last is None:
            raise RunNotRestartableError(f"instance {instance.id} has no runs to restart")
        await self._authorize(
            "restart",
            actor,
            last,
            {
                "parameter_overrides": dict(parameter_overrides or {}),
                "from_step": from_step,
                "definition_version": definition_version,
            },
        )
        lock = await cfg.lock_provider.acquire(instance.id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            raise LockAcquisitionError(f"instance {instance.id} is currently active")
        return await self._restart_locked(
            instance.id,
            lock,
            actor=actor,
            parameter_overrides=parameter_overrides,
            from_step=from_step,
            definition_version=definition_version,
        )

    async def redrive(self, instance_or_run_id: str, *, actor: Actor | None = None) -> JobRun:
        """Restart an instance whose last run was *interrupted* — its worker died
        (recovered by :meth:`recover_stale_runs`) or was cancelled — and drive it.

        This is how a host without message redelivery keeps crashed work from being
        lost: re-dispatch the runs the sweep returns to a worker that calls this. It
        runs the job in the calling process. Refused (``RunNotRestartableError``) for
        a run that failed on its own merits, was being stopped or paused, or was
        interrupted more than ``EtfConfig.max_auto_redrives`` times in a row.
        """
        cfg = self.cfg
        actor = actor or Actor.system()
        instance = await self._resolve_instance(instance_or_run_id)
        last = await cfg.store.find_latest_run(instance.id)
        if last is None:
            raise RunNotRestartableError(f"instance {instance.id} has no runs to re-drive")
        await self._authorize("redrive", actor, last)
        lock = await cfg.lock_provider.acquire(instance.id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            raise LockAcquisitionError(f"instance {instance.id} is currently active")
        try:
            last = await cfg.store.find_latest_run(instance.id)
            assert last is not None
            reason = await self._redrive_reason(last)
            if reason is None:
                raise RunNotRestartableError(await self._not_redrivable(last))
        except BaseException:
            await lock.release()
            raise
        return await self._restart_locked(
            instance.id, lock, actor=actor, redrive_reason=reason
        )

    async def _take_over(
        self,
        run: JobRun,
        lock: Lock,
        *,
        idempotency_key: str | None = None,
        reservation_run_id: str | None = None,
    ) -> JobRun:
        """Serve a delivery that found ``run`` while holding its instance ``lock``.

        Holding the lock proves no live worker owns the run, so a RUNNING, STOPPING or
        PAUSING run is orphaned: it is settled (like the stale-run sweep does) rather
        than handed back as accepted. A run whose worker went away without a verdict is
        then restarted as the next attempt; one that failed on its own merits or was
        being halted raises ``RunNotRestartableError``. Consumes ``lock``; points the
        delivery's idempotency key at the run that serves it.
        """
        cfg = self.cfg
        consumed = False
        try:
            if run.status in (BatchStatus.RUNNING, BatchStatus.STOPPING, BatchStatus.PAUSING):
                recovered = await self._recover_orphan(run.id, cutoff=None)
                run = recovered or await cfg.store.get_job_run(run.id)
            if run.status in (BatchStatus.FAILED, BatchStatus.STOPPED):
                reason = await self._redrive_reason(run)
                if reason is None:
                    raise RunNotRestartableError(await self._not_redrivable(run))
                consumed = True
                return await self._restart_locked(
                    run.instance_id,
                    lock,
                    actor=Actor.system(),
                    redrive_reason=reason,
                    idempotency_key=idempotency_key,
                    reservation_run_id=reservation_run_id,
                )
            if idempotency_key and reservation_run_id and reservation_run_id != run.id:
                await cfg.store.rebind_idempotency_key(
                    idempotency_key, reservation_run_id, run.id, cfg.idempotency_ttl
                )
            return run
        finally:
            if not consumed:
                await lock.release()

    async def _redrive_reason(self, run: JobRun) -> str | None:
        """Why ``run`` may be restarted automatically, or None if it may not."""
        interruption = _interruption(run)
        if interruption is None or self.cfg.max_auto_redrives <= 0:
            return None
        if await self._interruption_streak(run.instance_id) > self.cfg.max_auto_redrives:
            return None
        return interruption

    async def _interruption_streak(self, instance_id: str) -> int:
        """How many of the instance's most recent runs in a row were interrupted."""
        runs = sorted(
            await self.cfg.store.find_runs_for_instance(instance_id),
            key=lambda item: item.attempt,
        )
        streak = 0
        for run in reversed(runs):
            if _interruption(run) is None:
                break
            streak += 1
        return streak

    async def _not_redrivable(self, run: JobRun) -> str:
        if _interruption(run) is not None:
            streak = await self._interruption_streak(run.instance_id)
            return (
                f"run {run.id} was interrupted {streak} times in a row, more than "
                f"max_auto_redrives={self.cfg.max_auto_redrives}; restart it explicitly "
                "with JobOperator.restart(...)"
            )
        return (
            f"instance {run.instance_id} has a {run.status.value} run ({run.id}); "
            "use JobOperator.restart(...) to retry with optional config changes"
        )

    async def _restart_locked(
        self,
        instance_id: str,
        lock: Lock,
        *,
        actor: Actor,
        parameter_overrides: dict[str, Any] | None = None,
        from_step: str | None = None,
        definition_version: int | None = None,
        redrive_reason: str | None = None,
        idempotency_key: str | None = None,
        reservation_run_id: str | None = None,
    ) -> JobRun:
        """Create the next attempt under the caller's ``lock`` and drive it.

        Consumes ``lock``: it is released on failure and handed to the runner
        otherwise.
        """
        cfg = self.cfg
        consumed = False
        try:
            instance = await cfg.store.get_job_instance(instance_id)
            last = await cfg.store.find_latest_run(instance.id)
            if last is None:
                raise RunNotRestartableError(f"instance {instance.id} has no runs to restart")
            if last.status.is_running or last.status.is_paused:
                raise RunNotRestartableError(f"run {last.id} is still active ({last.status.value})")
            if definition_version is None or definition_version == last.definition_version:
                definition = self.runner._definition_for(last)
            else:
                definition = cfg.registry.get(instance.job_name, definition_version)
            policy = definition.restart_policy or cfg.restart_policy
            if not policy.can_restart(instance, last):
                raise RunNotRestartableError(f"restart policy refused instance {instance.id}")
            if from_step is not None:
                definition.step(from_step)
                target_index = [
                    step.name for step in definition.steps
                ].index(from_step)
                for prerequisite in definition.steps[:target_index]:
                    completed = await cfg.store.find_last_step_run(
                        instance.id,
                        prerequisite.name,
                        status=BatchStatus.COMPLETED,
                    )
                    if completed is None:
                        raise RunNotRestartableError(
                            f"cannot restart from {from_step!r}: prerequisite "
                            f"{prerequisite.name!r} has not completed"
                        )

            params = last.parameters
            if parameter_overrides:
                params = params.with_overrides(parameter_overrides)
            new_run = JobRun(
                id=cfg.ids.new_id(),
                instance_id=instance.id,
                job_name=instance.job_name,
                definition_version=definition.version,
                definition_fingerprint=definition.fingerprint,
                parameters=params,
                status=BatchStatus.PENDING,
                attempt=last.attempt + 1,
                restart_of=last.id,
                # Completed steps are skipped on restart, so the run-level context they
                # handed to later steps must carry over from the last durable state.
                execution_context=copy.deepcopy(last.execution_context),
                current_step=from_step,
                requested_by=last.requested_by,
                correlation_id=last.correlation_id,
                trace_id=last.trace_id,
                idempotency_key=idempotency_key,
            )
            attributes: dict[str, Any] = {"restart_of": last.id, "attempt": new_run.attempt}
            if redrive_reason is not None:
                attributes["redrive"] = redrive_reason
            if definition.version != last.definition_version:
                attributes["definition_version"] = {
                    "from": last.definition_version,
                    "to": definition.version,
                }

            async def create_restart() -> None:
                nonlocal new_run, instance
                instance = await cfg.store.get_job_instance(instance.id)
                new_run.version = 0
                new_run = await cfg.store.create_job_run(new_run)
                if idempotency_key and reservation_run_id:
                    await cfg.store.rebind_idempotency_key(
                        idempotency_key, reservation_run_id, new_run.id, cfg.idempotency_ttl
                    )
                instance.status = BatchStatus.RUNNING
                instance.definition_version = definition.version
                instance.updated_at = cfg.clock.now()
                await cfg.store.update_job_instance(instance, instance.version)
                await self.runner._emit(
                    AuditEventType.RESTARTED,
                    new_run,
                    actor=actor,
                    message=(
                        f"re-drive of {last.id} ({redrive_reason})"
                        if redrive_reason is not None
                        else f"restart of {last.id}"
                    ),
                    attributes=attributes,
                )
                if parameter_overrides:
                    await self.runner._emit(
                        AuditEventType.PARAMETERS_OVERRIDDEN,
                        new_run,
                        actor=actor,
                        attributes={
                            "overrides": dict(parameter_overrides),
                            "before": dict(last.parameters.non_identifying),
                            "after": dict(params.non_identifying),
                        },
                    )

            await cfg.store.run_in_transaction(create_restart)
            consumed = True
            return await self.runner.run(new_run, lock=lock)
        finally:
            if not consumed:
                await lock.release()

    # -- stop / pause / abandon --
    async def stop(self, run_id: str, *, actor: Actor | None = None) -> JobRun:
        return await self._request_halt(
            run_id, BatchStatus.STOPPING, AuditEventType.STATUS_CHANGED, actor or Actor.system()
        )

    async def pause(self, run_id: str, *, actor: Actor | None = None) -> JobRun:
        return await self._request_halt(
            run_id, BatchStatus.PAUSING, AuditEventType.STATUS_CHANGED, actor or Actor.system()
        )

    async def abandon(self, run_id: str, *, actor: Actor | None = None) -> JobRun:
        cfg = self.cfg
        actor = actor or Actor.system()
        run = await cfg.store.get_job_run(run_id)
        await self._authorize("abandon", actor, run)
        lock = await cfg.lock_provider.acquire(
            run.instance_id, cfg.worker_id, cfg.lock_ttl
        )
        if lock is None:
            raise LockAcquisitionError(f"instance {run.instance_id} is currently active")
        try:
            run = await cfg.store.get_job_run(run_id)
            if run.status in (
                BatchStatus.PENDING,
                BatchStatus.RUNNING,
                BatchStatus.STOPPING,
                BatchStatus.PAUSING,
            ):
                raise EtfError(f"cannot abandon an active run ({run.id}); stop it first")

            async def abandon_state() -> None:
                nonlocal run
                run = await cfg.store.get_job_run(run_id)
                if run.status in (
                    BatchStatus.PENDING,
                    BatchStatus.RUNNING,
                    BatchStatus.STOPPING,
                    BatchStatus.PAUSING,
                ):
                    raise EtfError(
                        f"cannot abandon an active run ({run.id}); stop it first"
                    )
                await self._resolve_open_validation(run, "run abandoned", actor=actor)
                run.status = BatchStatus.ABANDONED
                run.open_validation_id = None
                run.end_time = cfg.clock.now()
                run = await cfg.store.update_job_run(run, run.version)
                await self.runner._emit(
                    AuditEventType.STATUS_CHANGED,
                    run,
                    actor=actor,
                    to_status=BatchStatus.ABANDONED,
                )
                await self.runner._set_instance_status(
                    run.instance_id, BatchStatus.ABANDONED
                )
            await cfg.store.run_in_transaction(abandon_state)
            return run
        finally:
            await lock.release()

    async def _resolve_open_validation(self, run: JobRun, message: str, *, actor: Actor) -> None:
        """Close a run's dangling PENDING validation request as EXPIRED.

        Called whenever a run leaves AWAITING_VALIDATION through a path that is not a
        human decision (abandon, crash recovery) so no request stays open forever.
        """
        if not run.open_validation_id:
            return
        cfg = self.cfg
        try:
            request = await cfg.store.get_validation_request(run.open_validation_id)
        except KeyError:
            return
        if request.status is not ValidationStatus.PENDING:
            return
        request.status = ValidationStatus.EXPIRED
        request.decided_at = cfg.clock.now()
        await cfg.store.update_validation_request(request, request.version)
        await self.runner._emit(
            AuditEventType.VALIDATION_DECISION,
            run,
            step_name=request.step_name,
            actor=actor,
            message=message,
            attributes={
                "validation_request_id": request.id,
                "resolution": ValidationStatus.EXPIRED.value,
            },
        )

    # -- maintenance sweeps --
    async def recover_stale_runs(
        self,
        *,
        older_than: timedelta,
        limit: int = 100,
        scan_limit: int | None = None,
    ) -> list[JobRun]:
        """Crash recovery, half 2 (the sweep): reclaim runs orphaned by a dead worker.

        A worker killed without cleanup (SIGKILL, OOM, hard time limit) leaves its run
        in RUNNING/STOPPING/PAUSING forever — the lock TTL frees the *lock*, nothing
        frees the *run*. For each such run not updated within ``older_than``, try to
        acquire its instance lock: if the lock is free the owner is dead, so the run is
        transitioned to FAILED (restartable), its dangling validation request (if any)
        is expired, the instance is synced, and a RECOVERED audit event is emitted.
        A held lock means a live worker owns the run — it is skipped.

        Each status is scanned oldest-first in pages until ``limit`` runs were
        recovered or ``scan_limit`` candidates (default ``10 * limit``) were examined.
        Skipped candidates are paged past, so healthy long-running steps — whose lease
        heartbeat does not touch ``last_updated`` — cannot hide an orphan behind them,
        and a run that cannot be recovered (a store error on it) is logged and skipped
        rather than stopping the sweep.

        Run this periodically (from any scheduler) and once at worker start.
        Returns the recovered runs.
        """
        cfg = self.cfg
        cutoff = cfg.clock.now() - older_than
        max_scan = scan_limit if scan_limit is not None else 10 * limit
        recovered: list[JobRun] = []
        for status in (
            BatchStatus.PENDING,
            BatchStatus.RUNNING,
            BatchStatus.STOPPING,
            BatchStatus.PAUSING,
        ):
            recovered_here = 0
            scanned = 0
            still_stale = 0  # examined candidates that remain in the stale query's results
            while recovered_here < limit and scanned < max_scan:
                page_size = min(limit, max_scan - scanned)
                candidates = await cfg.store.list_stale_runs(
                    status=status,
                    updated_before=cutoff,
                    limit=page_size,
                    offset=still_stale,
                )
                for candidate in candidates:
                    scanned += 1
                    try:
                        run, stays = await self._recover_candidate(candidate, cutoff)
                    except Exception:
                        logger.exception(
                            "could not recover stale run %s; skipping it this sweep",
                            candidate.id,
                        )
                        run, stays = None, True
                    if run is not None:
                        recovered.append(run)
                        recovered_here += 1
                    if stays:
                        still_stale += 1
                    if recovered_here >= limit:
                        break
                if len(candidates) < page_size:
                    break
        return recovered

    async def _recover_candidate(
        self, candidate: JobRun, cutoff: datetime
    ) -> tuple[JobRun | None, bool]:
        """Recover one stale-looking run.

        Returns the recovered run (or None) and whether the candidate still matches
        the stale query afterwards (a live worker holds it), which drives paging.
        """
        cfg = self.cfg
        lock = await cfg.lock_provider.acquire(candidate.instance_id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            return None, True  # a live worker holds the lease; not orphaned
        try:
            return await self._recover_orphan(candidate.id, cutoff=cutoff), False
        finally:
            await lock.release()

    async def _recover_orphan(self, run_id: str, *, cutoff: datetime | None) -> JobRun | None:
        """Settle an active run nobody owns as FAILED (``etf.WorkerLost``), with its
        open validation request expired, its steps failed and a RECOVERED event.

        The caller holds the instance lock. With a ``cutoff`` the run must also not
        have been updated since then (the sweep's staleness check). Returns the
        recovered run, or None if it was no longer an orphan.
        """
        cfg = self.cfg
        system = Actor.system()
        recovered: JobRun | None = None

        async def recover_state() -> None:
            nonlocal recovered
            recovered = None
            run = await cfg.store.get_job_run(run_id)
            if run.status not in _ACTIVE_RUN_STATUSES or (
                cutoff is not None and run.last_updated and run.last_updated > cutoff
            ):
                return
            from_status = run.status
            now = cfg.clock.now()
            limit = cfg.max_failures_per_record
            failure = FailureRecord(
                exception_type=WORKER_LOST,
                message=(
                    f"run orphaned in {from_status.value}: its worker stopped without "
                    "recording an outcome (it died, or lost its lease and stopped "
                    "writing)"
                ),
                step_name=run.current_step,
                occurred_at=now,
                attributes={"orphaned_in": from_status.value},
            )
            await self._resolve_open_validation(run, "run recovered", actor=system)
            _append_failure(run.failures, failure, limit)
            run.status = BatchStatus.FAILED
            run.exit_status = ExitStatus.FAILED  # type: ignore[attr-defined]
            run.open_validation_id = None
            run.end_time = now
            run.last_updated = now
            run = await cfg.store.update_job_run(run, run.version)
            for step_run in await cfg.store.find_step_runs(run.id):
                if not (
                    step_run.status.is_terminal
                    or step_run.status in (BatchStatus.STOPPED, BatchStatus.ABANDONED)
                ):
                    _append_failure(step_run.failures, failure, limit)
                    step_run.status = BatchStatus.FAILED
                    step_run.exit_status = ExitStatus.FAILED  # type: ignore[attr-defined]
                    step_run.end_time = now
                    step_run.updated_at = now
                    await cfg.store.update_step_run(step_run, step_run.version)
            # The instance reflects its latest run; a newer run (e.g. a
            # restart that already completed) keeps its own status.
            latest = await cfg.store.find_latest_run(run.instance_id)
            if latest is None or latest.id == run.id:
                await self.runner._set_instance_status(run.instance_id, BatchStatus.FAILED)
            await self.runner._emit(
                AuditEventType.RECOVERED,
                run,
                step_name=failure.step_name,
                actor=system,
                from_status=from_status,
                to_status=BatchStatus.FAILED,
                failure=failure,
                message="orphaned run recovered; restart or re-drive to resume work",
            )
            recovered = run

        await cfg.store.run_in_transaction(recover_state)
        return recovered

    async def expire_validations(
        self, *, limit: int = 100, scan_limit: int | None = None
    ) -> list[ValidationRequest]:
        """SLA sweep: expire PENDING validation requests past their ``sla_deadline``.

        The expired request's run leaves AWAITING_VALIDATION and becomes STOPPED
        (restartable) so it cannot wedge behind a request nobody will answer.
        Overdue requests are queried directly, earliest deadline first, so requests
        without a deadline (which may wait indefinitely) never hide an overdue one.
        A request that cannot be expired now (its instance is busy, or a store error)
        is skipped and paged past; it never stops the sweep. At most ``scan_limit``
        candidates (default ``10 * limit``) are examined per call.
        Run alongside :meth:`recover_stale_runs`. Returns the expired requests.
        """
        cfg = self.cfg
        now = cfg.clock.now()
        max_scan = scan_limit if scan_limit is not None else 10 * limit
        expired: list[ValidationRequest] = []
        scanned = 0
        still_overdue = 0  # examined candidates that remain PENDING and overdue
        while len(expired) < limit and scanned < max_scan:
            page_size = min(limit, max_scan - scanned)
            candidates = await cfg.store.list_overdue_validations(
                now=now, limit=page_size, offset=still_overdue
            )
            for candidate in candidates:
                scanned += 1
                try:
                    request, stays = await self._expire_candidate(candidate)
                except Exception:
                    logger.exception(
                        "could not expire validation request %s; skipping it this sweep",
                        candidate.id,
                    )
                    request, stays = None, True
                if request is not None:
                    expired.append(request)
                if stays:
                    still_overdue += 1
                if len(expired) >= limit:
                    break
            if len(candidates) < page_size:
                break
        return expired

    async def _expire_candidate(
        self, candidate: ValidationRequest
    ) -> tuple[ValidationRequest | None, bool]:
        """Expire one overdue request. Returns the expired request (or None) and
        whether it is still PENDING afterwards (its instance was busy)."""
        cfg = self.cfg
        lock = await cfg.lock_provider.acquire(candidate.instance_id, cfg.worker_id, cfg.lock_ttl)
        if lock is None:
            return None, True  # instance is being advanced; revisit next sweep
        try:
            expired_request: ValidationRequest | None = None

            async def expire_state() -> None:
                nonlocal expired_request
                expired_request = None
                request = await cfg.store.get_validation_request(candidate.id)
                if request.status is not ValidationStatus.PENDING:
                    return
                request.status = ValidationStatus.EXPIRED
                request.decided_at = cfg.clock.now()
                request = await cfg.store.update_validation_request(request, request.version)
                run = await cfg.store.get_job_run(request.run_id)
                await self.runner._emit(
                    AuditEventType.VALIDATION_DECISION,
                    run,
                    step_name=request.step_name,
                    actor=Actor.system(),
                    message="validation SLA expired",
                    attributes={
                        "validation_request_id": request.id,
                        "resolution": ValidationStatus.EXPIRED.value,
                    },
                )
                if (
                    run.status is BatchStatus.AWAITING_VALIDATION
                    and run.open_validation_id == request.id
                ):
                    await self._halt_awaiting_run(
                        run, actor=Actor.system(), message="validation SLA expired"
                    )
                expired_request = request

            await cfg.store.run_in_transaction(expire_state)
            return expired_request, False
        finally:
            await lock.release()

    async def _request_halt(
        self, run_id: str, signal: BatchStatus, emit: AuditEventType, actor: Actor
    ) -> JobRun:
        cfg = self.cfg
        run = await cfg.store.get_job_run(run_id)
        if not run.status.is_running:
            raise EtfError(f"run {run.id} is {run.status.value}; nothing to {signal.value.lower()}")
        await self._authorize(
            "stop" if signal is BatchStatus.STOPPING else "pause", actor, run
        )
        async def request_halt() -> None:
            nonlocal run
            run = await cfg.store.get_job_run(run_id)
            if not run.status.is_running:
                raise EtfError(
                    f"run {run.id} is {run.status.value}; "
                    f"nothing to {signal.value.lower()}"
                )
            old = run.status
            run.status = signal
            run = await cfg.store.update_job_run(run, run.version)
            await self.runner._set_instance_status(run.instance_id, signal)
            await self.runner._emit(
                emit,
                run,
                actor=actor,
                from_status=old,
                to_status=signal,
                message="cooperative halt requested",
            )
        await cfg.store.run_in_transaction(request_halt)
        return run

    # -- queries --
    async def get_status(self, run_id: str) -> RunStatusView:
        cfg = self.cfg
        run = await cfg.store.get_job_run(run_id)
        steps = await cfg.store.find_step_runs(run_id)
        validation = (
            await cfg.store.get_validation_request(run.open_validation_id) if run.open_validation_id else None
        )
        return RunStatusView(run=run, steps=steps, open_validation=validation)

    async def get_audit_trail(self, run_id: str) -> list[AuditEvent]:
        return await self.cfg.audit.query(AuditQuery(run_id=run_id, limit=10_000))

    async def list_runs(
        self, *, job_name: str | None = None, status: BatchStatus | None = None, limit: int = 50
    ) -> list[JobRun]:
        return await self.cfg.store.list_runs(job_name=job_name, status=status, limit=limit)

    async def list_pending_validations(
        self, *, required_role: str | None = None, limit: int = 50
    ) -> list[ValidationRequest]:
        return await self.cfg.store.list_pending_validations(required_role=required_role, limit=limit)

    async def _resolve_instance(self, instance_or_run_id: str) -> JobInstance:
        cfg = self.cfg
        try:
            return await cfg.store.get_job_instance(instance_or_run_id)
        except KeyError:
            # Not an instance id — treat it as a run id. Anything else (store outage,
            # bad data) must propagate rather than be masked as "not found".
            run = await cfg.store.get_job_run(instance_or_run_id)
            return await cfg.store.get_job_instance(run.instance_id)


# Imported late to avoid a cycle (ingress imports model only).
from .ingress import LaunchRequest  # noqa: E402
