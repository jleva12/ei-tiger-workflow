"""The contract between the Forge async worker and its task packages.

A task package registers a :class:`~forge_tasks.protocols.TaskFactory` under the
``forge_async_worker.tasks`` entry point group. Its task exposes named jobs
(a :class:`JobSpec` in, a :class:`JobResult` out) on a queue, plus schedules;
the worker runs every job through the enhanced task framework.
"""

from forge_tasks.control import (
    AwaitingDecision,
    ControlSignal,
    Decision,
    JobControl,
    LocalJobControl,
    RunState,
    WaitingUntil,
    controlling,
    current_control,
)
from forge_tasks.errors import StorageError, TaskError, TransientError
from forge_tasks.runner import InlineJobQueue, JobRunner, ok, skipped
from forge_tasks.settings import CoreSettings, MongoSettings, load_section
from forge_tasks.tasks import (
    JobOutcome,
    JobResult,
    JobSpec,
    JobStatus,
    Schedule,
    TaskContext,
    TaskRegistry,
)

__all__ = [
    "AwaitingDecision",
    "ControlSignal",
    "CoreSettings",
    "Decision",
    "JobControl",
    "LocalJobControl",
    "RunState",
    "WaitingUntil",
    "controlling",
    "current_control",
    "InlineJobQueue",
    "JobOutcome",
    "JobResult",
    "JobRunner",
    "JobSpec",
    "JobStatus",
    "MongoSettings",
    "Schedule",
    "StorageError",
    "TaskContext",
    "TaskError",
    "TaskRegistry",
    "TransientError",
    "load_section",
    "ok",
    "skipped",
]
