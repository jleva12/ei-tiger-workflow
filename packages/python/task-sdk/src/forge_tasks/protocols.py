"""The contract between the worker and a task type.

A *task type* (adk_workflows, ...) is a self-contained domain with its own
sources, storage and logic. The worker only needs it to expose named *jobs*; the
queue, retries and keeping a run's state are the worker's. A task type
is a package with a :class:`TaskFactory` registered under the
``forge_async_worker.tasks`` entry point group.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from forge_tasks.tasks import JobOutcome, JobResult, JobSpec, Schedule, TaskContext

# --------------------------------------------------------------------------- jobs and tasks


@runtime_checkable
class Job(Protocol):
    """One kind of work a task type performs (run an ADK workflow...).

    Jobs must be idempotent: the worker may run a job twice (redelivery,
    retries). Return follow-up JobSpecs instead of enqueueing directly, so the
    runner decides how they're scheduled (SAQ in prod, inline in tests).
    """

    @property
    def name(self) -> str: ...

    @property
    def payload_model(self) -> type[BaseModel]: ...

    def lock_key(self, payload: Any) -> str | None:
        """Key that serializes conflicting runs (e.g. one per record), or None."""
        ...

    async def run(self, payload: Any) -> JobResult: ...

    # Optional: ``describe(payload) -> str | None`` (or async), a few words on
    # what a run of the job works on (an ADK workflow's name). Not part of the
    # protocol, so jobs without it still conform.


@runtime_checkable
class Task(Protocol):
    """A task type, built. It may also have an ``observer`` (a JobObserver)
    that the runner tells about every attempt of its jobs."""

    name: str
    queue: str

    @property
    def jobs(self) -> Mapping[str, Job]: ...

    async def ensure_schema(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class JobObserver(Protocol):
    """A task's optional ``observer``: told about every attempt of the task's
    jobs once it has finished, whatever the outcome, before its follow-ups are
    enqueued. Keep it quick; the runner bounds it, and logs what it raises."""

    async def job_finished(self, outcome: JobOutcome) -> None: ...


@runtime_checkable
class TaskFactory(Protocol):
    """The unit you register. Static metadata (its name, queue and schedules)
    is readable without building the task. The worker runs no schedules.

    Optional attributes the worker also reads:

    ``settings_model``
        A pydantic model for the task's own options, read from the
        ``HYBRID_<NAME>__*`` environment variables (and ``.env``) and handed to
        :meth:`build` as ``ctx.options``.
    ``cli_name``, ``add_cli(parser)`` and ``async run_cli(args, runtime)``
        Commands the task adds to the worker's CLI, under ``cli_name``.
    ``prepare()``
        Downloads what the task would otherwise fetch at runtime (grammars,
        tokenizer files) into its caches; ``forge-async-worker prepare`` calls
        it for every installed task when the image is built.
    """

    @property
    def name(self) -> str: ...

    @property
    def queue(self) -> str: ...

    @property
    def schedules(self) -> Sequence[Schedule]: ...

    def build(self, ctx: TaskContext) -> Task: ...


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, spec: JobSpec, *, countdown: float | None = None) -> None: ...
