"""Task-type framework: job envelopes, results, schedules, context, registry."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, Field

from forge_tasks.errors import TaskError

if TYPE_CHECKING:
    from forge_tasks.protocols import Task, TaskFactory
    from forge_tasks.settings import CoreSettings

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "forge_async_worker.tasks"

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobSpec(BaseModel):
    """Serializable envelope for one unit of work. This is all a queue carries.

    ``tenant_id`` is who the work is for (a Forge organization), among whose
    background tasks it shows. Without it, a payload's own ``tenant_id``
    counts; follow-ups without one inherit their parent's (the runner sees to
    it). Work for no one in particular (a schedule's sweep) has none.

    ``labels`` are extra names the tracked run carries, to find it by (the
    workflow a run is of, the run that started it). The worker's own labels
    (task type, kind, tenant) win over them.

    ``requested_by`` is the person who asked for it (``{"id", "display_name"}``),
    which its tracked run records; without it, the worker itself.
    """

    task_type: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    requested_by: dict[str, str] | None = None

    def label(self) -> str:
        return f"{self.task_type}.{self.kind}"

    @property
    def tenant(self) -> str | None:
        if self.tenant_id:
            return self.tenant_id
        owner = self.payload.get("tenant_id")
        return owner if isinstance(owner, str) and owner else None

    def wire(self) -> dict[str, Any]:
        """What a queue carries: ``{"task_type", "kind", "payload"}``, and
        ``tenant_id``, ``labels`` and ``requested_by`` when set (other
        services send specs without them)."""
        data = self.model_dump(mode="json")
        for optional in ("tenant_id", "labels", "requested_by"):
            if not data.get(optional):
                data.pop(optional, None)
        return data


class JobStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"  # nothing to do (unchanged input, already indexed)
    SUPERSEDED = "superseded"  # a newer run owns the target
    FAILED = "failed"  # permanent failure; retrying won't help


class JobResult(BaseModel):
    status: JobStatus = JobStatus.OK
    detail: dict[str, Any] = Field(default_factory=dict)
    followups: list[JobSpec] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def failed(cls, error: str, **detail: Any) -> JobResult:
        return cls(status=JobStatus.FAILED, error=error, detail=detail)


@dataclass(frozen=True)
class JobOutcome:
    """One finished attempt of a job, as the runner saw it: the result it
    returned (or made of a permanent error), or the exception it raised. What
    a task's observer is told (protocols.JobObserver)."""

    spec: JobSpec
    result: JobResult | None  # None when the job raised
    error: Exception | None = None
    duration_ms: float = 0.0
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def status(self) -> str:
        """The result's status; "retrying" when the job raised a TransientError
        (the queue retries it), "error" when it raised anything else."""
        if self.result is not None:
            return self.result.status.value
        return "retrying" if isinstance(self.error, TaskError) and not self.error.permanent else "error"


class Schedule(BaseModel):
    """Periodic job. Exactly one of ``every_seconds`` / ``cron`` (5-field)."""

    name: str
    job: JobSpec
    every_seconds: float | None = None
    cron: str | None = None


@dataclass
class TaskContext:
    """Everything a task factory may use to build itself.

    ``settings`` is the shared infrastructure (Mongo, the queue's Redis);
    ``options`` the task's own settings (its factory's ``settings_model``).
    ``resources`` is shared by every task of the process: clients built once
    through :meth:`shared` (model clients, a Mongo pool) live there.
    """

    settings: CoreSettings
    options: Any = None
    mongo_client: Any = None  # an injected client (tests); otherwise :meth:`mongo` builds the shared one
    # The Redis the job queue runs on (the worker's), for tasks that publish
    # state for other services; None when jobs run inline (CLI, tests).
    redis_url: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # test doubles, third-party deps
    resources: dict[str, Any] = field(default_factory=dict)

    def shared(self, key: str, factory: Callable[[], T]) -> T:
        """The process-wide resource ``key``, built by ``factory`` on first use."""
        if key not in self.resources:
            self.resources[key] = factory()
        return self.resources[key]  # type: ignore[no-any-return]

    def mongo(self) -> Any:
        """The shared ``AsyncMongoClient`` (settings.mongo), created on first use."""
        if self.mongo_client is not None:
            return self.mongo_client

        def connect() -> Any:
            from pymongo import AsyncMongoClient

            return AsyncMongoClient(
                self.settings.mongo.uri.get_secret_value(), appname="forge-async-worker", tz_aware=True
            )

        return self.shared(MONGO_RESOURCE, connect)


MONGO_RESOURCE = "forge_tasks.mongo_client"


class TaskRegistry:
    def __init__(self, factories: Iterable[TaskFactory] = ()) -> None:
        self._factories: dict[str, TaskFactory] = {}
        for f in factories:
            self.register(f)

    def register(self, factory: TaskFactory) -> None:
        self._factories[factory.name] = factory

    def load_entry_points(self) -> int:
        n = 0
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
                self.register(obj() if isinstance(obj, type) else obj)
                n += 1
            except Exception:
                log.exception("failed to load task entry point %s", ep.name)
        return n

    def names(self) -> list[str]:
        return sorted(self._factories)

    def factory(self, name: str) -> TaskFactory:
        try:
            return self._factories[name]
        except KeyError:
            raise KeyError(f"unknown task type {name!r}; registered: {self.names()}") from None

    def queues(self, enabled: Iterable[str]) -> dict[str, str]:
        return {n: self.factory(n).queue for n in enabled}

    def schedules(self, enabled: Iterable[str]) -> list[Schedule]:
        return [s for n in enabled for s in self.factory(n).schedules]

    def build(
        self,
        ctx: TaskContext,
        enabled: Iterable[str],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Task]:
        """Build each enabled task with its own options: ``options[name]`` when
        given, else its factory's ``settings_model`` read from the environment."""
        from dataclasses import replace

        from forge_tasks.settings import load_section

        built: dict[str, Task] = {}
        for name in enabled:
            factory = self.factory(name)
            task_options = (options or {}).get(name)
            model = getattr(factory, "settings_model", None)
            if task_options is None and model is not None:
                task_options = load_section(name, model)
            built[name] = factory.build(replace(ctx, options=task_options))
        return built
