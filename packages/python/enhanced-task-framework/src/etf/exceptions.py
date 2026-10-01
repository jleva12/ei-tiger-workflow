"""Framework exceptions and control-flow signals.

Two families live here:

* **Errors** (``EtfError`` subclasses) — abnormal conditions raised by the framework.
* **Control-flow signals** (``PauseForValidation``, ``StopExecution``) — raised by a
  :class:`etf.steps.Step` to direct the runner. They are not errors; the runner catches
  them and drives the lifecycle (pause/stop) rather than treating them as failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import ValidationRequest


class EtfError(Exception):
    """Base class for all framework errors."""


# --------------------------------------------------------------------------- #
# Idempotency / lifecycle errors
# --------------------------------------------------------------------------- #
class JobInstanceAlreadyExistsError(EtfError):
    """Raised by the store when a unique (job_name, identity_hash) already exists."""


class JobInstanceAlreadyCompleteError(EtfError):
    """Launch was attempted against an instance that has already COMPLETED.

    Completed instances are terminal and cannot be re-run (Spring Batch semantics).
    """


class RunNotRestartableError(EtfError):
    """Restart attempted on a run whose status / policy forbids restarting."""


class StepExitError(EtfError):
    """Step code raised ``SystemExit`` or ``KeyboardInterrupt`` — typically ``sys.exit``
    from a CLI library run in-process.

    Raised in its place (the original is the ``__cause__``) so the step fails with that
    reason instead of the exception escaping and stopping the event loop.
    """

    @classmethod
    def from_exit(cls, exc: BaseException) -> "StepExitError":
        args = ", ".join(repr(arg) for arg in exc.args)
        return cls(f"step code raised {type(exc).__name__}({args})")


class JobAlreadyRunningError(EtfError):
    """The idempotency key is reserved by a concurrent launch that has not committed yet.

    Raised when a duplicate delivery arrives while the winning launch is still creating
    its run and the poll budget (``EtfConfig.idempotency_poll_budget``) elapses before
    the reservation resolves to a run. Safe to retry the delivery later.
    """


class UnknownJobError(EtfError):
    """No :class:`etf.steps.JobDefinition` registered under the given name."""


class OptimisticLockError(EtfError):
    """A persisted entity changed underneath us (version mismatch). Reload and retry."""


class LockAcquisitionError(EtfError):
    """Could not acquire the single-writer lock for an instance."""


class DefinitionVersionMismatchError(EtfError):
    """A persisted run cannot safely execute under the registered job definition."""


class ValidationStateError(EtfError):
    """A validation decision was submitted against a run not awaiting validation."""


class ValidationAuthorizationError(EtfError):
    """The decision actor is not authorized to resolve the validation request."""


class ValidationSchemaError(EtfError):
    """A validation decision does not satisfy the request's decision schema."""


# --------------------------------------------------------------------------- #
# Control-flow signals (NOT errors)
# --------------------------------------------------------------------------- #
class StepSignal(Exception):
    """Base for control-flow signals a step raises to steer the runner.

    .. warning::
        ``StepSignal`` subclasses :class:`Exception`, so a step's own broad
        ``except Exception:`` handler will swallow pause/stop signals and turn them
        into business failures. Steps that catch broadly **must re-raise**
        ``StepSignal``::

            try:
                ...
            except StepSignal:
                raise                      # let the runner pause/stop the run
            except Exception:
                ...                        # genuine business failure handling
    """


class PauseForValidation(StepSignal):
    """Raised by a step to pause the run for human-in-the-loop validation.

    The runner checkpoints durable state, opens the carried
    :class:`~etf.model.ValidationRequest`, transitions the run to
    ``AWAITING_VALIDATION``, and **returns** (releasing the worker).
    """

    def __init__(self, request: "ValidationRequest") -> None:
        super().__init__(f"pause for validation: {request.reason}")
        self.request = request


class StopExecution(StepSignal):
    """Raised by a step to gracefully stop the run at this point (restartable)."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "stop requested")
        self.reason = reason
