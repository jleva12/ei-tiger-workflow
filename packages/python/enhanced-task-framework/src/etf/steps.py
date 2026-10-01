"""User-facing work units: ``Step``, ``StepResult``, ``JobDefinition``, ``JobRegistry``.

Integrators implement :class:`Step` subclasses and compose them into a
:class:`JobDefinition`. The flow between steps is linear by default but supports
conditional transitions keyed on a step's :class:`~etf.status.ExitStatus` code.
Flows must be acyclic — revisiting a step within one run is not supported and is
rejected at definition time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .context import StepContext
from .exceptions import DefinitionVersionMismatchError, StepExitError, UnknownJobError
from .status import ExitStatus
from .policies import JobInstanceResolver, RestartPolicy, RetryPolicy, SkipPolicy


@dataclass(slots=True)
class StepResult:
    """The outcome a step returns on success.

    ``exit_status`` drives conditional flow (see :class:`FlowTransition`). The metric
    counters are folded into the persisted :class:`~etf.model.StepRun`.
    """

    exit_status: ExitStatus = field(default_factory=lambda: ExitStatus.COMPLETED)  # type: ignore[attr-defined]
    read_count: int = 0
    write_count: int = 0
    skip_count: int = 0
    commit_count: int = 0
    attributes: dict[str, object] = field(default_factory=dict)

    @staticmethod
    def completed(
        read_count: int = 0, write_count: int = 0, skip_count: int = 0, commit_count: int = 0
    ) -> "StepResult":
        return StepResult(
            exit_status=ExitStatus.COMPLETED,  # type: ignore[attr-defined]
            read_count=read_count,
            write_count=write_count,
            skip_count=skip_count,
            commit_count=commit_count,
        )

    @staticmethod
    def with_exit(code: str, description: str = "") -> "StepResult":
        return StepResult(exit_status=ExitStatus(code, description))


class Step(ABC):
    """A unit of work within a job.

    Implementations must set :attr:`name` (unique within the job) and implement
    :meth:`execute`. To pause for human validation, call ``ctx.request_validation(...)``
    or raise :class:`~etf.exceptions.PauseForValidation`. To stop gracefully, raise
    :class:`~etf.exceptions.StopExecution`.

    **Idempotency contract:** a step may be re-invoked after a crash that occurred
    *after* a side effect but *before* the checkpoint persisted. Write steps to be
    idempotent (keyed upserts, "reserve if not reserved") and stash progress in
    ``ctx.step_context`` + ``await ctx.checkpoint()``.

    **Steps must not block the event loop.** The runner, its lease heartbeat, and the
    stop/pause control monitor all share one loop; synchronous I/O or CPU work inside
    :meth:`execute` silently stops the heartbeat until the lease expires and the run is
    orphaned. Use :func:`run_blocking_cancellation_safe`, or subclass
    :class:`BlockingStep` which does it for you. Raw ``asyncio.to_thread`` returns
    immediately when its awaiter is cancelled even though the OS thread keeps running,
    which can release the instance lease around live side effects.

    **Exception handling:** pause/stop signals are exceptions
    (:class:`~etf.exceptions.StepSignal`). A broad ``except Exception:`` inside
    :meth:`execute` must re-raise ``StepSignal`` or the runner will record the pause
    as a business failure.
    """

    #: Unique step name within the job. Subclasses must override.
    name: str = ""

    @abstractmethod
    async def execute(self, ctx: StepContext) -> StepResult:
        """Perform the work. Return a :class:`StepResult` on success."""
        raise NotImplementedError

    @property
    def allow_restart_if_complete(self) -> bool:
        """If True, the step re-runs on restart even when previously COMPLETED.

        Default False gives Spring Batch semantics: completed steps are skipped on
        restart. Set True for steps that must always run (e.g. a final notification).
        """
        return False


class BlockingStep(Step):
    """A :class:`Step` whose work is synchronous — it runs in a worker thread.

    Implement :meth:`execute_sync` instead of :meth:`execute`; the framework runs it
    via a cancellation-safe ``asyncio.to_thread`` wrapper so the shared event loop
    (heartbeat, control monitor) keeps running and the instance lease is retained until
    the worker thread actually exits.

    Because the body executes off the loop, the async services on the context are
    unavailable mid-step: do not call ``ctx.checkpoint()``, ``ctx.request_validation()``
    or ``ctx.emit_audit()`` from :meth:`execute_sync`. Reading ``ctx.params`` and
    mutating ``ctx.step_context`` is fine (the context is checkpointed when the step
    finishes). Steps that need mid-step checkpoints or HITL gates should be async.
    """

    @abstractmethod
    def execute_sync(self, ctx: StepContext) -> StepResult:
        """Perform the (blocking) work. Runs in a worker thread."""
        raise NotImplementedError

    async def execute(self, ctx: StepContext) -> StepResult:
        return await run_blocking_cancellation_safe(self.execute_sync, ctx)


def _without_process_exit(fn: Callable[..., Any], *args: Any) -> Any:
    """Call ``fn`` in the worker thread, turning ``sys.exit``/``KeyboardInterrupt`` into
    :class:`~etf.exceptions.StepExitError`: asyncio re-raises those two out of the event
    loop, which would stop every run sharing it."""
    try:
        return fn(*args)
    except (SystemExit, KeyboardInterrupt) as exc:
        raise StepExitError.from_exit(exc) from exc


async def run_blocking_cancellation_safe(
    fn: Callable[..., Any],
    *args: Any,
    on_cancel: Callable[[], None] | None = None,
) -> Any:
    """Run blocking work without releasing its caller while the thread is still alive.

    Cancelling ``asyncio.to_thread`` only cancels the awaiter, not the OS thread.  Shield
    the thread task, notify cooperative blocking code when requested, and keep awaiting
    termination before propagating cancellation so locks are not released around live
    side effects.
    """

    thread_task = asyncio.create_task(asyncio.to_thread(_without_process_exit, fn, *args))
    try:
        return await asyncio.shield(thread_task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                if on_cancel is not None:
                    on_cancel()
        # Observe a worker exception, but cancellation remains the public outcome.
        with contextlib.suppress(BaseException):
            thread_task.result()
        raise


@dataclass(slots=True)
class FlowTransition:
    """A conditional edge: from ``from_step`` on ``on_exit_code`` go to ``to_step``.

    ``on_exit_code`` matches a step's :class:`~etf.status.ExitStatus` code; ``"*"`` is a
    wildcard. ``to_step`` of ``None`` ends the job. Transitions must not form cycles —
    :class:`JobDefinition` rejects flows that could revisit a step.
    """

    from_step: str
    on_exit_code: str
    to_step: str | None


@dataclass
class JobDefinition:
    """An ordered list of steps plus optional conditional transitions and policies.

    Flow is linear (list order) unless a :class:`FlowTransition` matches the finished
    step's exit code; an unmatched code falls through to the next step in list order.
    The resulting graph must be acyclic — loops would either re-skip a completed step
    forever or violate the one-step-run-per-``(run, step)`` uniqueness constraint, so
    they are rejected in ``__post_init__``.

    ``retry_policy`` / ``skip_policy`` / ``restart_policy`` / ``instance_resolver``
    override the :class:`~etf.operator.EtfConfig` defaults for this job only.
    """

    name: str
    steps: list[Step]
    version: int = 1
    transitions: list[FlowTransition] = field(default_factory=list)
    # Optional per-job policy overrides (else launcher/operator defaults are used).
    retry_policy: RetryPolicy | None = None
    skip_policy: SkipPolicy | None = None
    restart_policy: RestartPolicy | None = None
    instance_resolver: JobInstanceResolver | None = None

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"job {self.name!r}: version must be a positive integer")
        names = [s.name for s in self.steps]
        if "" in names:
            raise ValueError(f"job {self.name!r}: every step must set a non-empty .name")
        if len(set(names)) != len(names):
            raise ValueError(f"job {self.name!r}: duplicate step names {names}")
        if not names:
            raise ValueError(f"job {self.name!r}: at least one step is required")
        self._by_name: dict[str, Step] = {s.name: s for s in self.steps}
        self._order: list[str] = names
        for transition in self.transitions:
            if transition.from_step not in self._by_name:
                raise ValueError(
                    f"job {self.name!r}: transition starts at unknown step {transition.from_step!r}"
                )
            if transition.to_step is not None and transition.to_step not in self._by_name:
                raise ValueError(
                    f"job {self.name!r}: transition targets unknown step {transition.to_step!r}"
                )
        self._reject_cycles()

    def _successors(self, name: str) -> set[str]:
        """All steps reachable in one hop: explicit transition targets, plus the linear
        next step unless a ``"*"`` wildcard makes fallthrough unreachable."""
        explicit = [t for t in self.transitions if t.from_step == name]
        targets = {t.to_step for t in explicit if t.to_step is not None}
        if not any(t.on_exit_code == "*" for t in explicit):
            idx = self._order.index(name)
            if idx + 1 < len(self._order):
                targets.add(self._order[idx + 1])
        return targets

    def _reject_cycles(self) -> None:
        """Depth-first walk over every possible edge; raise on a back-edge (cycle)."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self._order, WHITE)

        def visit(node: str, path: list[str]) -> None:
            color[node] = GRAY
            for succ in self._successors(node):
                if color[succ] == GRAY:
                    cycle = " -> ".join([*path, node, succ])
                    raise ValueError(
                        f"job {self.name!r}: flow contains a cycle ({cycle}); "
                        "revisiting a step within one run is not supported"
                    )
                if color[succ] == WHITE:
                    visit(succ, [*path, node])
            color[node] = BLACK

        for name in self._order:
            if color[name] == WHITE:
                visit(name, [])

    @property
    def first_step(self) -> str:
        return self._order[0]

    @property
    def fingerprint(self) -> str:
        """A digest of the flow's shape: step names in order and the transitions.

        Stored on every run, so a definition whose steps or transitions changed without
        a version bump is refused instead of resuming a checkpoint that no longer lines
        up. Step *implementations* are not part of it: changing what a step does is an
        ordinary code change.
        """
        shape = {
            "steps": self._order,
            "transitions": [[t.from_step, t.on_exit_code, t.to_step] for t in self.transitions],
        }
        encoded = json.dumps(shape, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    def step(self, name: str) -> Step:
        return self._by_name[name]

    def next_step(self, current: str, exit_status: ExitStatus) -> str | None:
        """Return the step after ``current`` given its exit status, or None to end.

        Resolution order: an explicit transition matching the exit code, then a ``"*"``
        wildcard transition, then linear fallthrough to the next step in list order.
        """
        explicit = [t for t in self.transitions if t.from_step == current]
        for t in explicit:
            if t.on_exit_code == exit_status.code:
                return t.to_step
        for t in explicit:
            if t.on_exit_code == "*":
                return t.to_step
        idx = self._order.index(current)
        return self._order[idx + 1] if idx + 1 < len(self._order) else None

    def steps_from(self, current: str | None) -> list[str]:
        """Return the ordered step names beginning at ``current``."""
        start = self._order.index(current) if current else 0
        return self._order[start:]


class JobRegistry:
    """Maps job names to their :class:`JobDefinition` versions.

    Several versions of a job can be registered side by side. New launches use the
    latest; a run always executes on the version it started with, so keep a version
    registered until its runs (including paused and failed ones) have finished — or
    move them forward with ``JobOperator.restart(..., definition_version=...)``.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[int, JobDefinition]] = {}

    def register(self, definition: JobDefinition) -> None:
        """Register ``definition`` under its name and version.

        Registering the same flow again replaces the definition (new step code, say);
        registering a different flow (steps or transitions) under a version that is
        already registered raises ``ValueError`` — give it a new version instead.
        """
        versions = self._jobs.setdefault(definition.name, {})
        existing = versions.get(definition.version)
        if existing is not None and existing.fingerprint != definition.fingerprint:
            raise ValueError(
                f"job {definition.name!r} version {definition.version} is already "
                "registered with a different flow (steps or transitions); register the "
                "changed flow under a new version"
            )
        versions[definition.version] = definition

    def get(self, name: str, version: int | None = None) -> JobDefinition:
        """Return ``name``'s definition: the latest version, or the given one.

        Raises ``UnknownJobError`` if no version of ``name`` is registered, and
        ``DefinitionVersionMismatchError`` if the requested version is not.
        """
        versions = self._jobs.get(name)
        if not versions:
            raise UnknownJobError(f"no job registered under {name!r}")
        if version is None:
            return versions[max(versions)]
        try:
            return versions[version]
        except KeyError:
            raise DefinitionVersionMismatchError(
                f"job {name!r} version {version} is not registered (registered: "
                f"{sorted(versions)}); keep it registered until its runs finish, or "
                "restart them on a newer version with restart(..., definition_version=...)"
            ) from None

    def versions(self, name: str) -> list[int]:
        """Return the registered versions of ``name``, oldest first."""
        return sorted(self._jobs.get(name, {}))

    def names(self) -> list[str]:
        """Return all registered job names."""
        return list(self._jobs)
