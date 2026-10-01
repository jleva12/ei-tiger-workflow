"""Enhanced Task Framework (ETF).

A library to *track, manage, and recover* long-running asynchronous task runs in any
Python application, whatever triggers them (a task queue, a message consumer, an HTTP
handler, a scheduler, …). See ``docs/DESIGN.md`` for the full design specification.

Quick start::

    from etf import (
        EtfConfig, JobLauncher, JobOperator, JobRegistry, JobDefinition,
        Step, StepContext, StepResult, LaunchRequest, JobParameters, Actor,
    )
    from etf.audit import StoreBackedAuditSink
    from etf.locking import InMemoryLockProvider
    from etf.stores.memory import InMemoryStateStore

    store = InMemoryStateStore(); await store.initialize()
    cfg = EtfConfig(store=store, audit=StoreBackedAuditSink(store),
                    lock_provider=InMemoryLockProvider(), registry=registry)
    run = await JobLauncher(cfg).launch(LaunchRequest(job_name="...", parameters=...))
"""

from __future__ import annotations

from .audit import (
    AuditEvent,
    AuditQuery,
    AuditSink,
    CompositeAuditSink,
    LoggingAuditSink,
    StoreBackedAuditSink,
)
from .bridge import AsyncBridge
from .context import ExecutionContext, StepContext
from .exceptions import (
    DefinitionVersionMismatchError,
    EtfError,
    JobAlreadyRunningError,
    JobInstanceAlreadyCompleteError,
    JobInstanceAlreadyExistsError,
    LockAcquisitionError,
    OptimisticLockError,
    PauseForValidation,
    RunNotRestartableError,
    StepExitError,
    StopExecution,
    UnknownJobError,
    ValidationStateError,
    ValidationAuthorizationError,
    ValidationSchemaError,
)
from .ingress import CallableLaunchIngress, Ingress, LaunchRequest, ResumeRequest
from .locking import InMemoryLockProvider, Lock, RunLockProvider
from .model import (
    Actor,
    FailureRecord,
    JobInstance,
    JobParameters,
    JobRun,
    StepRun,
    ValidationDecision,
    ValidationRequest,
)
from .operator import EtfConfig, JobLauncher, JobOperator, JobRunner, OperatorAction, RunStatusView
from .policies import (
    BackoffRetryPolicy,
    Clock,
    DefaultRestartPolicy,
    HashingInstanceResolver,
    IdGenerator,
    JobInstanceResolver,
    LimitedSkipPolicy,
    NeverSkipPolicy,
    RestartPolicy,
    RetryDecision,
    RetryPolicy,
    SkipPolicy,
    SystemClock,
    UuidGenerator,
)
from .serialization import JsonSerializer, Serializer
from .status import (
    ActorKind,
    AuditEventType,
    BatchStatus,
    ExitStatus,
    ValidationDecisionType,
    ValidationStatus,
)
from .steps import (
    BlockingStep,
    FlowTransition,
    JobDefinition,
    JobRegistry,
    Step,
    StepResult,
    run_blocking_cancellation_safe,
)
from .store import InstancePage, InstanceQuery, StateStore

__version__ = "0.1.0"

__all__ = [
    # config / orchestration
    "EtfConfig", "JobLauncher", "JobRunner", "JobOperator", "OperatorAction", "RunStatusView",
    # definitions / work
    "Step", "BlockingStep", "run_blocking_cancellation_safe",
    "StepResult", "StepContext", "ExecutionContext",
    "JobDefinition", "JobRegistry", "FlowTransition",
    # ingress
    "Ingress", "LaunchRequest", "ResumeRequest", "CallableLaunchIngress",
    # synchronous hosts
    "AsyncBridge",
    # model
    "Actor", "JobParameters", "JobInstance", "JobRun", "StepRun",
    "ValidationRequest", "ValidationDecision", "FailureRecord",
    # contracts (pluggable)
    "StateStore", "InstanceQuery", "InstancePage", "AuditSink", "RunLockProvider", "Lock", "Serializer",
    "JobInstanceResolver", "RetryPolicy", "SkipPolicy", "RestartPolicy",
    "Clock", "IdGenerator", "RetryDecision",
    # default impls
    "StoreBackedAuditSink", "CompositeAuditSink", "LoggingAuditSink",
    "InMemoryLockProvider", "JsonSerializer", "HashingInstanceResolver",
    "BackoffRetryPolicy", "NeverSkipPolicy", "LimitedSkipPolicy",
    "DefaultRestartPolicy", "SystemClock", "UuidGenerator",
    # audit
    "AuditEvent", "AuditQuery", "AuditEventType",
    # enums
    "BatchStatus", "ExitStatus", "ActorKind",
    "ValidationStatus", "ValidationDecisionType",
    # errors / signals
    "EtfError", "DefinitionVersionMismatchError", "PauseForValidation", "StopExecution",
    "JobInstanceAlreadyExistsError", "JobInstanceAlreadyCompleteError",
    "JobAlreadyRunningError", "RunNotRestartableError", "StepExitError", "UnknownJobError",
    "OptimisticLockError", "LockAcquisitionError", "ValidationStateError",
    "ValidationAuthorizationError", "ValidationSchemaError",
    "__version__",
]
