"""Execution context (the durable checkpoint bag) and the per-step run context.

``ExecutionContext`` is the Spring Batch analogue: a mutable key/value store where
steps stash resume state (cursor offsets, page tokens, partial aggregates).
``StepContext`` is what a :class:`etf.steps.Step` receives — it exposes the parameters,
the contexts, and the framework services (checkpoint, request-validation, audit) via
callbacks injected by the runner, so the model layer stays free of orchestration
dependencies.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from .model import Actor, JobParameters, ValidationOutcome


class ExecutionContext:
    """A serializable key/value bag for durable resume state.

    Contents must round-trip through the configured
    :class:`~etf.serialization.Serializer` (JSON-compatible values by default).
    Nothing is persisted until the runner checkpoints — call ``await
    ctx.checkpoint()`` from the step after mutating.

    The bag owns its data: ``initial`` is deep-copied in and :meth:`as_dict` returns a
    deep copy, so in-place changes to nested values (``ctx.get("cursor")["offset"] +=
    10``) never reach the durable state they were loaded from until a checkpoint.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(dict(initial or {}))

    # -- reads --
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._data.get(key, default))

    def contains(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> Iterator[str]:
        return iter(self._data.keys())

    # -- writes --
    def put(self, key: str, value: Any) -> None:
        self._data[key] = value

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def as_dict(self) -> dict[str, Any]:
        """A deep-copied snapshot, safe to persist or hand to another context."""
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ExecutionContext(keys={list(self._data)})"


# Callback signatures injected by the runner. Keeping these as callables (rather than a
# back-reference to the runner/store) avoids a circular dependency and keeps StepContext
# easy to construct in tests.
CheckpointFn = Callable[[str], Awaitable[None]]
RequestValidationFn = Callable[..., Awaitable[ValidationOutcome]]
EmitAuditFn = Callable[..., Awaitable[None]]


class StepContext:
    """What a :class:`etf.steps.Step.execute` receives.

    Exposes read-only parameters, the run- and step-level execution contexts, and the
    framework services a step needs: checkpointing, requesting human validation, and
    emitting audit annotations. ``should_stop()`` lets long-running steps cooperatively
    honor a stop/pause request mid-flight.
    """

    def __init__(
        self,
        *,
        run_id: str,
        instance_id: str,
        step_name: str,
        attempt: int,
        params: JobParameters,
        run_context: ExecutionContext,
        step_context: ExecutionContext,
        checkpoint_fn: CheckpointFn,
        request_validation_fn: RequestValidationFn,
        emit_audit_fn: EmitAuditFn,
        should_stop_fn: Callable[[], bool],
        actor: Actor,
        fencing_token: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.instance_id = instance_id
        self.step_name = step_name
        self.attempt = attempt
        self.params = params
        self.run_context = run_context
        self.step_context = step_context
        self.actor = actor
        self.fencing_token = fencing_token
        self._checkpoint_fn = checkpoint_fn
        self._request_validation_fn = request_validation_fn
        self._emit_audit_fn = emit_audit_fn
        self._should_stop_fn = should_stop_fn

    # -- step-context convenience (defaults to the per-step bag) --
    def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the step-level context bag."""
        return self.step_context.get(key, default)

    def put(self, key: str, value: Any) -> None:
        """Write a value into the step-level context bag (persisted on checkpoint)."""
        self.step_context.put(key, value)

    async def checkpoint(self, message: str = "") -> None:
        """Durably persist the run- and step-level contexts now.

        Call after completing a resumable unit of work so a crash/restart resumes
        from this point instead of re-doing it. ``message`` says what was saved
        (e.g. which part of the work it's at), on the ``CHECKPOINT_SAVED`` event.
        """
        await self._checkpoint_fn(message)

    async def request_validation(
        self,
        *,
        reason: str,
        payload: dict[str, Any] | None = None,
        decision_schema: dict[str, Any] | None = None,
        required_role: str | None = None,
        sla_seconds: int | None = None,
    ) -> ValidationOutcome:
        """Open a human-in-the-loop gate.

        **First call:** checkpoints durable state and raises
        :class:`~etf.exceptions.PauseForValidation`, releasing the worker.

        **After a decision arrives** and the run resumes, the step re-executes and this
        same call *returns* the :class:`~etf.model.ValidationOutcome` instead of pausing
        again — so a step can simply do ``outcome = await ctx.request_validation(...)``
        and branch on ``outcome.approved``. A step may open several gates; each receives
        its own decision, in call order.

        .. warning::
            The pause works by raising a :class:`~etf.exceptions.StepSignal`. Do not
            wrap this call in a broad ``except Exception:`` without re-raising the
            signal, or the pause becomes a business failure.
        """
        return await self._request_validation_fn(
            reason=reason,
            payload=payload or {},
            decision_schema=decision_schema or {},
            required_role=required_role,
            sla_seconds=sla_seconds,
        )

    async def emit_audit(self, message: str, **attributes: Any) -> None:
        """Append a free-form ``ANNOTATION`` event to the run's audit trail."""
        await self._emit_audit_fn(message=message, attributes=attributes)

    def should_stop(self) -> bool:
        """True once a stop/pause was requested (or the worker's lease was lost).

        Long-running steps should poll this and raise
        :class:`~etf.exceptions.StopExecution` to wind down at a safe point.
        """
        return self._should_stop_fn()
