"""An in-process runtime: the enabled tasks, built, with a runner that runs
their jobs inline (breadth-first, follow-ups included). For tests, the CLI and
local development; the worker builds the same tasks but runs each job through
the task framework, off its queues.

    rt = build_runtime(CoreSettings(enabled_tasks=["workflows"]))
    await rt.submit(JobSpec(task_type="workflows", kind="run", payload={"workflow_id": ...}))
    rt.workflows  # a task by name
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from forge_tasks.runner import InlineJobQueue, JobRunner
from forge_tasks.settings import CoreSettings
from forge_tasks.tasks import JobResult, JobSpec, TaskContext, TaskRegistry

if TYPE_CHECKING:
    from forge_tasks.protocols import JobQueue, Task, TaskFactory


@dataclass
class Runtime:
    settings: CoreSettings
    context: TaskContext
    registry: TaskRegistry
    tasks: dict[str, Task]
    runner: JobRunner
    queue: JobQueue

    def task(self, name: str) -> Task:
        return self.tasks[name]

    def __getattr__(self, name: str) -> Any:
        """A built task by name: ``rt.workflows``."""
        tasks = self.__dict__.get("tasks") or {}
        if name in tasks:
            return tasks[name]
        raise AttributeError(name)

    async def submit(self, spec: JobSpec) -> JobResult:
        """Run now (and, with the inline queue, everything it fans out to)."""
        if isinstance(self.queue, InlineJobQueue):
            return await self.queue.submit(spec)
        return await self.runner.run(spec)

    async def enqueue(self, spec: JobSpec) -> None:
        await self.queue.enqueue(spec)

    async def ensure_schema(self) -> None:
        for task in self.tasks.values():
            await task.ensure_schema()

    async def close(self) -> None:
        for task in self.tasks.values():
            await task.close()
        await close_resources(self.context)
        close_queue = getattr(self.queue, "close", None)  # e.g. the worker queue's Redis connections
        if close_queue is not None:
            await close_queue()


async def close_resources(ctx: TaskContext) -> None:
    """Close the shared clients the tasks' context holds (the Mongo pool and
    whatever else offers ``close``/``aclose``), and an injected Mongo client."""
    closables = list(ctx.resources.values())
    if ctx.mongo_client is not None:
        closables.append(ctx.mongo_client)
    seen: set[int] = set()
    for resource in closables:
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if close is None:
            continue
        outcome = close()
        if inspect.isawaitable(outcome):
            await outcome
    ctx.resources.clear()


def build_registry(factories: Iterable[TaskFactory] | None = None) -> TaskRegistry:
    """The given factories, or every installed task package's (entry points)."""
    if factories is not None:
        return TaskRegistry(factories)
    registry = TaskRegistry()
    registry.load_entry_points()
    return registry


def build_runtime(
    settings: CoreSettings | None = None,
    *,
    factories: Iterable[TaskFactory] | None = None,
    registry: TaskRegistry | None = None,
    queue: JobQueue | None = None,
    options: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    mongo_client: Any = None,
    extras: Mapping[str, Any] | None = None,
    redis_url: str | None = None,
) -> Runtime:
    """Build the enabled tasks. ``options`` overrides a task's own settings
    (by task name), ``resources`` pre-seeds shared clients (model clients in
    tests), ``redis_url`` is the Redis the jobs' queue runs on, where tasks
    publish state for other services; without it they keep that in memory."""
    settings = settings or CoreSettings()
    registry = registry or build_registry(factories)
    ctx = TaskContext(
        settings=settings,
        mongo_client=mongo_client,
        redis_url=redis_url,
        extras=dict(extras or {}),
        resources=dict(resources or {}),
    )
    tasks = registry.build(ctx, settings.enabled_tasks, dict(options or {}))
    queue = queue or InlineJobQueue()
    runner = JobRunner(tasks, queue)
    if isinstance(queue, InlineJobQueue):
        queue.bind(runner)
    return Runtime(settings=settings, context=ctx, registry=registry, tasks=tasks, runner=runner, queue=queue)
