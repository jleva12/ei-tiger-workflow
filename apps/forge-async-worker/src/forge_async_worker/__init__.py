"""Generic async task worker: every installed task package's jobs, off SAQ
queues on Redis, each one a run tracked by the enhanced task framework.

    from forge_async_worker import WorkerSettings, build_runtime, JobSpec
    rt = build_runtime(WorkerSettings())
    await rt.submit(JobSpec(task_type="workflows", kind="run", payload={...}))

Task types live in their own packages (``packages/python/tasks``) and register
under the ``forge_async_worker.tasks`` entry point group; the contract between
them and the worker is ``forge_tasks``.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.3.0"

_LAZY = {
    "WorkerSettings": "forge_async_worker.config",
    "build_runtime": "forge_async_worker.runtime",
    "default_registry": "forge_async_worker.runtime",
    "Runtime": "forge_async_worker.runtime",
    "SaqJobQueue": "forge_async_worker.queue",
    "JobSpec": "forge_tasks.tasks",
    "JobResult": "forge_tasks.tasks",
    "JobStatus": "forge_tasks.tasks",
    "Schedule": "forge_tasks.tasks",
    "TaskContext": "forge_tasks.tasks",
    "TaskRegistry": "forge_tasks.tasks",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:  # lazy so importing a submodule stays cheap
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(name)
