"""
One worker process of the integration suite: the real worker
(``saq_worker.serve`` on the ``adk_workflows`` queue and its upkeep's), with
its timings shortened from the environment, so a scenario takes seconds
rather than minutes. Nothing else of it changes: each setting below replaces
a module constant that jobs.py or saq_worker.py reads when it needs it, before
anything has read it.

    FORGE_IT_LEASE_SECONDS       jobs.LEASE_SECONDS: renewals, the stop margins and the watchdog follow it
    FORGE_IT_STOP_SECONDS        jobs.STOP_SECONDS
    FORGE_IT_STALLED_AFTER       jobs.STALLED_AFTER
    FORGE_IT_MAINTENANCE         seconds between the worker's own maintenances (a six-field cron), or "off":
                                 no cron at all (the tests run the maintenance themselves)
    FORGE_IT_CLOCK_SKEW_SECONDS  the run store's clock, this many seconds ahead of the host's
    FORGE_IT_CONCURRENCY         serve()'s concurrency (default 4)
    FORGE_IT_GRACE_SECONDS       serve()'s grace period (default 30, the CLI's)

Everything else is the worker's own settings (HYBRID_*), which the suite sets.
The process starts in a folder of its own, so no .env reaches it.

    python worker_main.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

from forge_common.logging import configure_logging

from forge_async_worker import jobs, saq_worker
from forge_async_worker.config import WorkerSettings
from forge_async_worker.queue import QUEUE
from forge_task_adk_workflows import run_store


def _number(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    return float(value) if value else None


def _shorten() -> None:
    if (lease := _number("FORGE_IT_LEASE_SECONDS")) is not None:
        jobs.LEASE_SECONDS = lease
    if (stop := _number("FORGE_IT_STOP_SECONDS")) is not None:
        jobs.STOP_SECONDS = stop
    if (stalled := _number("FORGE_IT_STALLED_AFTER")) is not None:
        jobs.STALLED_AFTER = stalled
    maintenance = os.environ.get("FORGE_IT_MAINTENANCE", "").strip()
    if maintenance == "off":
        build = saq_worker.build_workers

        def without_cron(*args: Any, **kwargs: Any) -> list[Any]:
            workers = build(*args, **kwargs)
            for worker in workers:
                worker.cron_jobs = []
            return workers

        saq_worker.build_workers = without_cron
    elif maintenance:
        saq_worker.MAINTENANCE_CRON = f"* * * * * */{int(float(maintenance))}"
    if skew := _number("FORGE_IT_CLOCK_SKEW_SECONDS"):
        opened = saq_worker.open_store

        def skewed(settings: Any) -> run_store.RunStore:
            store = opened(settings)
            store.clock = lambda: run_store.utcnow() + timedelta(seconds=skew)
            return store

        saq_worker.open_store = skewed


def main() -> None:
    _shorten()
    settings = WorkerSettings()
    configure_logging(settings.logging)
    asyncio.run(
        saq_worker.serve(
            settings,
            [QUEUE],
            concurrency=int(_number("FORGE_IT_CONCURRENCY") or 4),
            grace_period=int(_number("FORGE_IT_GRACE_SECONDS") or 30),
            ensure_schema=True,
        )
    )


if __name__ == "__main__":
    main()
