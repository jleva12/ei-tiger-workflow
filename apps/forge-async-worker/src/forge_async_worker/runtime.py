"""Composition root: the task types come from the installed task packages
(``forge_async_worker.tasks`` entry points); the worker only supplies the shared
infrastructure. Jobs run inline here (the CLI, tests); under ``worker`` each one
runs through the enhanced task framework instead (etf_jobs.py).

    rt = build_runtime()
    await rt.submit(JobSpec(task_type="commits", kind="backfill", payload={"repo_id": ...}))
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from forge_async_worker.config import WorkerSettings
from forge_tasks.runtime import Runtime, build_registry
from forge_tasks.runtime import build_runtime as build_task_runtime

if TYPE_CHECKING:
    from forge_tasks.protocols import JobQueue
    from forge_tasks.tasks import TaskRegistry

__all__ = ["Runtime", "build_runtime", "default_registry"]


def default_registry() -> TaskRegistry:
    """Every installed task package's factory."""
    return build_registry()


def build_runtime(
    settings: WorkerSettings | None = None,
    *,
    queue: JobQueue | None = None,
    registry: TaskRegistry | None = None,
    options: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    mongo_client: Any = None,
    extras: Mapping[str, Any] | None = None,
    redis_url: str | None = None,
) -> Runtime:
    """The enabled tasks, built. ``redis_url``: the Redis the jobs' queue runs
    on, where tasks publish state for other services (the worker passes its
    own); without it, tasks keep that state in memory."""
    return build_task_runtime(
        settings or WorkerSettings(),
        registry=registry or default_registry(),
        queue=queue,
        options=options,
        resources=resources,
        mongo_client=mongo_client,
        extras=extras,
        redis_url=redis_url,
    )
