"""Dev/ops CLI. Jobs run inline (no worker needed), except under ``worker``.

    forge-async-worker worker --ensure-schema                 # the SAQ worker, every enabled queue
    forge-async-worker worker --queues commits --concurrency 4
    forge-async-worker worker --check                         # health: this host serves each queue
    forge-async-worker api                                    # the background tasks API (HYBRID_API__*)
    forge-async-worker tasks                                  # installed task types and their jobs
    forge-async-worker ensure-schema
    forge-async-worker prepare                                # image build: what the tasks fetch at runtime
    forge-async-worker run commits sync_all                   # any job, any task type
    forge-async-worker run commits backfill '{"repo_id": "r1"}'

Each task package adds its own commands under its name (``forge-async-worker
docs ...``, ``forge-async-worker commits ...``); ``--help`` lists them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from forge_common.logging import configure_logging

from forge_async_worker.config import WorkerSettings
from forge_async_worker.runtime import build_runtime, default_registry
from forge_tasks.tasks import JobSpec


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _parser(task_commands: dict[str, Any]) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="forge-async-worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tasks", help="installed task types, their queues, jobs and schedules")
    sub.add_parser("ensure-schema", help="create the enabled tasks' collections and search indexes")
    sub.add_parser("prepare", help="download what every installed task fetches at runtime (for image builds)")
    wk = sub.add_parser("worker", help="run the SAQ worker")
    wk.add_argument("--queues", default="", help="comma-separated queue names (default: every enabled task type's)")
    wk.add_argument("--concurrency", type=int, default=4, help="jobs each queue runs at once (default 4)")
    wk.add_argument(
        "--grace-period", type=int, default=30, help="seconds running jobs get at shutdown before they are re-queued"
    )
    wk.add_argument(
        "--ensure-schema",
        action="store_true",
        help="create the served task types' collections and search indexes first",
    )
    wk.add_argument("--check", action="store_true", help="health check: exit 1 unless this host serves each queue")
    api = sub.add_parser("api", help="serve the background tasks API (for the admin API)")
    api.add_argument("--host", help="interface to listen on (default HYBRID_API__HOST, 127.0.0.1)")
    api.add_argument("--port", type=int, help="port to listen on (default HYBRID_API__PORT, 8104)")
    run = sub.add_parser("run", help="run one job inline")
    run.add_argument("task_type")
    run.add_argument("kind")
    run.add_argument("payload", nargs="?", default="{}")
    for cli_name, factory in task_commands.items():
        factory.add_cli(sub.add_parser(cli_name, help=f"the {factory.name} task's commands"))
    return ap


def main(argv: list[str] | None = None) -> None:
    registry = default_registry()
    task_commands = {
        factory.cli_name: factory
        for factory in (registry.factory(name) for name in registry.names())
        if getattr(factory, "cli_name", None) and hasattr(factory, "add_cli")
    }
    args = _parser(task_commands).parse_args(argv)
    settings = WorkerSettings()

    if args.cmd == "worker":
        from forge_async_worker import saq_worker

        queues = [q.strip() for q in args.queues.split(",") if q.strip()]
        # The health check answers with its exit status; it logs only problems.
        configure_logging(settings.logging.model_copy(update={"level": "WARNING"}) if args.check else settings.logging)
        if args.check:
            raise SystemExit(0 if asyncio.run(saq_worker.check(settings, queues)) else 1)
        asyncio.run(
            saq_worker.serve(
                settings,
                queues,
                concurrency=args.concurrency,
                grace_period=args.grace_period,
                ensure_schema=args.ensure_schema,
            )
        )
        return
    if args.cmd == "api":
        import uvicorn

        from forge_async_worker.api import create_app

        configure_logging(settings.logging)
        uvicorn.run(
            create_app(settings, registry=registry),
            host=args.host or settings.api.host,
            port=args.port or settings.api.port,
            # Logging is owned by configure_logging(); access lines come from RequestContextMiddleware.
            log_config=None,
            access_log=False,
        )
        return
    if args.cmd == "prepare":
        # Every installed task, enabled or not: the image serves whichever a deployment enables.
        for name in registry.names():
            prepare = getattr(registry.factory(name), "prepare", None)
            if prepare is not None:
                prepare()
                print(f"prepared {name}")
        return
    if args.cmd == "tasks":
        enabled = set(settings.enabled_tasks)
        for name in registry.names():
            factory = registry.factory(name)
            state = "enabled" if name in enabled else "disabled"
            schedules = [s.name for s in factory.schedules]
            print(f"{name} ({state}) queue={factory.queue} schedules={schedules}")
        return

    async def go() -> None:
        if args.cmd in task_commands:
            factory = task_commands[args.cmd]
            # The task's commands need the task built, whether or not this deployment enables it.
            rt = build_runtime(
                settings.model_copy(update={"enabled_tasks": [*settings.enabled_tasks, factory.name]}),
                registry=registry,
            )
        else:
            rt = build_runtime(settings, registry=registry)
        try:
            if args.cmd == "ensure-schema":
                await rt.ensure_schema()
                print("schema ensured for", list(rt.tasks))
            elif args.cmd == "run":
                spec = JobSpec(task_type=args.task_type, kind=args.kind, payload=json.loads(args.payload))
                _print((await rt.submit(spec)).model_dump(mode="json"))
            else:
                await task_commands[args.cmd].run_cli(args, rt)
        finally:
            await rt.close()

    asyncio.run(go())


if __name__ == "__main__":
    main()
