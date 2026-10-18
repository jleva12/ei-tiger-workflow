"""
What a scenario works with: the suite's MySQL and Redis (:class:`Services`),
worker processes (:class:`WorkerProcess`), and, for one test, the run store
and queue as the admin API has them, the maintenance as a worker runs it,
the mock server, and a timeline (:class:`Scenario`).

Workers are real processes (``worker_main.py``), each in a folder of its own,
with an environment of the suite's only: nothing of the developer's shell,
.env or .env.common reaches them. The admin API's part (starting runs,
deciding, abandoning, queueing their jobs) is played by the test, through the
same RunStore and SAQ queue the admin API uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from saq import Queue
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from forge_async_worker.jobs import AdkRunJobs
from forge_async_worker.queue import QUEUE, UPKEEP, RunQueue
from forge_task_adk_workflows.run_store import FINISHED, RunStore, events, runs, utcnow
from forge_task_adk_workflows.usage_store import calls, invocations

from .mock_server import MockServer

HARNESS = Path(__file__).resolve().parent
WORKER_MAIN = HARNESS / "worker_main.py"
#: The suite's defaults for every worker: a lease of seconds, not minutes.
LEASE_SECONDS = 6.0
#: ADK's own session tables (DatabaseSessionService creates them on first use).
ADK_TABLES = ("events", "sessions", "app_states", "user_states")


@dataclass(frozen=True)
class Services:
    """The suite's MySQL (migrated) and Redis, and how to stop and start Redis."""

    mysql_url: str
    redis_url: str
    #: ``docker compose ...`` for the suite's project, to stop and start its Redis.
    compose: tuple[str, ...]
    redis_service: str = "it-redis"


# ----------------------------------------------------------------------------- the timeline


@dataclass
class Timeline:
    """What happened, from every side, by time: the test's actions, the
    mock server's requests, the runs' activity and the workers' lives."""

    started: float = field(default_factory=time.time)
    lines: list[tuple[float, str, str]] = field(default_factory=list)

    def add(self, source: str, text: str, at: float | None = None) -> None:
        self.lines.append((time.time() if at is None else at, source, text))

    def render(self, extra: Iterable[tuple[float, str, str]] = ()) -> str:
        merged = sorted([*self.lines, *extra], key=lambda line: line[0])
        return "\n".join(f"{at - self.started:8.3f}s  {source:<14} {text}" for at, source, text in merged)


def epoch(when: datetime | None) -> float:
    """A run store time (aware or naive UTC) as time.time() has it."""
    if when is None:
        return 0.0
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


# ----------------------------------------------------------------------------- worker processes


@dataclass
class WorkerProcess:
    """One worker process: the real worker, timings shortened (worker_main.py)."""

    name: str
    process: subprocess.Popen[bytes]
    log: Path
    timeline: Timeline
    concurrency: int
    #: When the suite saw it gone (time.time()).
    exited_at: float | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    def signal(self, signum: int, said: str | None = None) -> None:
        self.timeline.add("test", said or f"{signal.Signals(signum).name} to worker {self.name} (pid {self.pid})")
        self.process.send_signal(signum)

    def kill(self) -> None:
        self.signal(signal.SIGKILL, f"kill -9 worker {self.name} (pid {self.pid})")

    def text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def records(self) -> list[dict[str, Any]]:
        """Its log, each line as JSON (a line that isn't, as ``{"event": line}``)."""
        found = []
        for line in self.text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                record = {"event": line}
            found.append(record if isinstance(record, dict) else {"event": line})
        return found

    def said(self, words: str) -> bool:
        return words in self.text()

    async def ready(self, seconds: float = 90) -> WorkerProcess:
        """Waits until it serves its queues."""
        deadline = time.monotonic() + seconds
        while not self.said("serving queue(s)"):
            if not self.alive:
                raise AssertionError(
                    f"worker {self.name} exited ({self.returncode}) before serving:\n{self.text()[-4000:]}"
                )
            if time.monotonic() > deadline:
                raise AssertionError(f"worker {self.name} didn't serve in {seconds:g}s:\n{self.text()[-4000:]}")
            await asyncio.sleep(0.1)
        self.timeline.add(f"worker {self.name}", f"serving (pid {self.pid}, concurrency {self.concurrency})")
        return self

    async def exited(self, seconds: float = 60) -> int:
        """:return: Its exit status, once it has exited (a signal's is negative)."""
        deadline = time.monotonic() + seconds
        while self.alive:
            if time.monotonic() > deadline:
                raise AssertionError(f"worker {self.name} still runs after {seconds:g}s")
            await asyncio.sleep(0.05)
        code = self.process.returncode
        self.exited_at = time.time()
        self.timeline.add(f"worker {self.name}", f"exited with {code}")
        return code

    def end(self) -> None:
        """Gone, whatever it was doing (a test's teardown)."""
        if self.alive:
            try:
                os.kill(self.pid, signal.SIGCONT)  # a paused one can't die otherwise
            except ProcessLookupError:
                pass
            self.process.kill()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            pass
        self.exited_at = self.exited_at or time.time()

    def interesting(self) -> list[tuple[float, str, str]]:
        """Its log lines a timeline shows: what became of runs, warnings, errors,
        the watchdog's and faulthandler's words (those without a time of their
        own go where the line before them was)."""
        found: list[tuple[float, str, str]] = []
        last = 0.0
        records = self.records()
        # Its last words without a time of their own (faulthandler's, the watchdog's): as it went.
        timed = max((n for n, record in enumerate(records) if record.get("timestamp")), default=-1)
        for n, record in enumerate(records):
            if n > timed and self.exited_at is not None:
                last = self.exited_at
            event = str(record.get("event") or "")
            level = str(record.get("level") or "")
            when = record.get("timestamp")
            try:
                at = datetime.fromisoformat(str(when)).timestamp() if when else 0.0
            except ValueError:
                at = 0.0
            last = at or last
            if any(noise in event for noise in _NOISE):
                continue
            if not level:
                if "watchdog" in event or "Fatal Python" in event or "Timeout" in event or "Traceback" in event:
                    found.append((last, f"worker {self.name}", f"[stderr] {event[:300]}"))
                continue
            if level in ("warning", "error", "critical") or event.startswith("run ") or "stopping" in event:
                found.append((at or last, f"worker {self.name}", f"[{level}] {event[:300]}"))
        return found


#: Log lines every worker writes as it starts, which no timeline needs.
_NOISE = (
    "MCP servers and credentials can't be used",
    "runs started with files will fail",
)


# ----------------------------------------------------------------------------- one scenario


@dataclass
class Scenario:
    """One test's world: its own tables and queue (cleared before it), its workers, its timeline."""

    services: Services
    engine: AsyncEngine
    store: RunStore
    queue: RunQueue
    mock: MockServer
    folder: Path
    model_provider: Path
    timeline: Timeline = field(default_factory=Timeline)
    workers: list[WorkerProcess] = field(default_factory=list)
    #: Run IDs the timeline shows the activity of.
    watched: list[str] = field(default_factory=list)
    #: What the scenario saw, by what it is: the report's summary.
    observations: dict[str, Any] = field(default_factory=dict)
    #: Its report, once it's over (``opened``).
    report: str = ""

    # ------------------------------------------------------------------ workers

    def worker_environment(self, **settings: Any) -> dict[str, str]:
        """The environment of a worker: the suite's settings only, then ``settings`` (HYBRID_* or FORGE_IT_*)."""
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("HYBRID_", "FORGE_", "OPENAI", "ANTHROPIC", "GOOGLE", "SPANNER"))
        }
        workspaces = self.folder / "workspaces"
        workspaces.mkdir(exist_ok=True)
        env.update(
            {
                "FORGE_ENV_COMMON_FILE": "",
                "HYBRID_ENABLED_TASKS": '["adk_workflows"]',
                "HYBRID_REDIS_URL": self.services.redis_url,
                "HYBRID_LOGGING__FORMAT": "json",
                "HYBRID_LOGGING__LEVEL": "INFO",
                "HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL": self.services.mysql_url,
                "HYBRID_ADK_WORKFLOWS__HTTP_ALLOW_PRIVATE": "true",
                "HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG": str(self.model_provider),
                "HYBRID_ADK_WORKFLOWS__COMMANDS": "local",
                "HYBRID_ADK_WORKFLOWS__WORKSPACES": str(workspaces),
                "HYBRID_ADK_WORKFLOWS__INLINE_DELAY_SECONDS": "0",
                "FORGE_IT_LEASE_SECONDS": str(LEASE_SECONDS),
                "FORGE_IT_MAINTENANCE": "off",
            }
        )
        env.update({name: str(value) for name, value in settings.items()})
        return env

    async def spawn(
        self,
        name: str,
        *,
        concurrency: int = 4,
        lease: float = LEASE_SECONDS,
        maintenance: float | str = "off",
        grace: float | None = None,
        stalled_after: float | None = None,
        skew: float | None = None,
        ready: bool = True,
        **settings: Any,
    ) -> WorkerProcess:
        """A worker process, serving once it's ready (``ready``)."""
        extra: dict[str, Any] = {
            "FORGE_IT_CONCURRENCY": concurrency,
            "FORGE_IT_LEASE_SECONDS": lease,
            "FORGE_IT_MAINTENANCE": maintenance,
            **settings,
        }
        if grace is not None:
            extra["FORGE_IT_GRACE_SECONDS"] = grace
        if stalled_after is not None:
            extra["FORGE_IT_STALLED_AFTER"] = stalled_after
        if skew is not None:
            extra["FORGE_IT_CLOCK_SKEW_SECONDS"] = skew
        home = self.folder / f"worker-{name}"
        home.mkdir(exist_ok=True)
        log = home / "worker.log"
        with log.open("wb") as out:
            process = subprocess.Popen(
                [sys.executable, str(WORKER_MAIN)],
                cwd=home,
                env=self.worker_environment(**extra),
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        worker = WorkerProcess(name, process, log, self.timeline, concurrency)
        self.workers.append(worker)
        self.timeline.add(
            "test", f"started worker {name} (pid {process.pid}, concurrency {concurrency}, lease {lease:g}s)"
        )
        return await worker.ready() if ready else worker

    async def spawn_all(self, *names: str, **options: Any) -> list[WorkerProcess]:
        """Several workers, started together (each takes seconds to start), once they're all ready."""
        started = [await self.spawn(name, ready=False, **options) for name in names]
        return list(await asyncio.gather(*(worker.ready() for worker in started)))

    def owner_of(self, run: dict[str, Any]) -> WorkerProcess:
        """:return: The worker process whose attempt holds the run (by the pid in its lease's owner)."""
        owner = str(run.get("lease_owner") or "")
        pid = owner.split("/")[0].rsplit(".", 1)[-1]
        for worker in self.workers:
            if str(worker.pid) == pid:
                return worker
        raise AssertionError(f"no worker of this scenario holds run {run['id'][:8]} (its owner is {owner or 'nobody'})")

    @staticmethod
    def owner_pid(event: dict[str, Any]) -> int | None:
        """:return: The pid of the worker a run's claim event (``started``, ``resumed``) names."""
        owner = str((event.get("attributes") or {}).get("worker") or "")
        pid = owner.split("/")[0].rsplit(".", 1)[-1]
        return int(pid) if pid.isdigit() else None

    def worker_of(self, pid: int | None) -> str:
        """:return: The name of this scenario's worker process ``pid``, or the pid."""
        return next((worker.name for worker in self.workers if worker.pid == pid), str(pid))

    # ------------------------------------------------------------------ the admin API's part

    def say(self, text: str) -> None:
        self.timeline.add("test", text)

    def observe(self, what: str, value: Any) -> Any:
        """Records what the scenario saw (the report's summary), and hands it back."""
        self.observations[what] = value
        self.say(f"observed {what}: {value}")
        return value

    def observed(self) -> str:
        return "\n".join(f"{what}: {value}" for what, value in self.observations.items()) or "(nothing recorded)"

    async def enqueue(self, run_id: str, *, key: str | None = None) -> None:
        """Queue a job that takes the run, as the admin API does once it started or moved it on."""
        await self.queue.run_adk(run_id, key=key)

    async def start(self, workflow: dict[str, Any], value: Any = None, *, queue: bool = True, **more: Any) -> str:
        from .workflows import start_run

        run = await start_run(self.store, workflow, value, **more)
        self.watched.append(run["id"])
        self.say(f"started run {run['id'][:8]} ({workflow['name']})")
        if queue:
            await self.enqueue(run["id"])
        return str(run["id"])

    async def warm_sessions(self, *members: str) -> None:
        """ADK's app and user state rows for the members' runs, made before
        any run's (``create_session`` makes them the first time), so a
        scenario about something else doesn't race for them too (they once
        failed the runs: FINDINGS.md IT-1)."""
        from google.adk.sessions import DatabaseSessionService

        from forge_task_adk_workflows.runs import APP_NAME

        from .workflows import MEMBER

        sessions = DatabaseSessionService(db_url=self.services.mysql_url)
        try:
            for member in members or (MEMBER.id,):
                await sessions.create_session(app_name=APP_NAME, user_id=member)
        finally:
            await sessions.close()

    async def maintain(self, *, skew: float = 0.0) -> dict[str, list[str]]:
        """The maintenance, once, as a worker runs it (on the run store's clock, ``skew`` seconds ahead)."""
        store = self.store
        if skew:
            store = RunStore(self.engine, clock=lambda: utcnow() + timedelta(seconds=skew))
        jobs = AdkRunJobs(store=store, queue=self.queue, runner=None)  # type: ignore[arg-type]  # it runs no job
        done = await jobs.maintain()
        said = {name: [run[:8] for run in ids] for name, ids in done.items() if ids}
        self.say(f"maintenance{f' (clock {skew:+g}s)' if skew else ''}: {said or 'nothing to do'}")
        return done

    # ------------------------------------------------------------------ reading

    async def run(self, run_id: str) -> dict[str, Any]:
        found = await self.store.get(run_id)
        assert found is not None, f"no run {run_id}"
        return found

    async def until(
        self,
        run_id: str,
        check: str | Iterable[str] | Callable[[dict[str, Any]], bool],
        *,
        seconds: float = 60,
        every: float = 0.1,
    ) -> dict[str, Any]:
        """:return: The run, once it's in a status (one of them), or ``check`` holds of it."""
        if isinstance(check, str):
            statuses = {check}
            holds: Callable[[dict[str, Any]], bool] = lambda run: run["status"] in statuses  # noqa: E731
        elif callable(check):
            holds = check
        else:
            statuses = set(check)
            holds = lambda run: run["status"] in statuses  # noqa: E731
        deadline = time.monotonic() + seconds
        while True:
            found = await self.run(run_id)
            if holds(found):
                return found
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"run {run_id[:8]} didn't get there in {seconds:g}s: it's {found['status']} "
                    f"(attempt {found['attempt']}, epoch {found['lease_epoch']})\n{await self.render()}"
                )
            await asyncio.sleep(every)

    async def finished(self, run_id: str, *, seconds: float = 60) -> dict[str, Any]:
        return await self.until(run_id, FINISHED, seconds=seconds)

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.store.events(run_id)

    async def kinds(self, run_id: str) -> list[str]:
        return [event["kind"] for event in await self.events(run_id)]

    async def usage(self, run_id: str) -> list[dict[str, Any]]:
        """The run's model and tool calls, as the overview counts them (``usage_calls``)."""
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(sa.select(calls).where(calls.c.run_id == run_id).order_by(calls.c.id))
            ).mappings()
            return [dict(row) for row in rows]

    async def session_events(self, session_id: str) -> list[dict[str, Any]]:
        """The ADK session's events, oldest first (ADK's own ``events`` table)."""
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                sa.text(
                    "SELECT id, invocation_id, timestamp, event_data FROM events WHERE session_id = :s ORDER BY timestamp"
                ),
                {"s": session_id},
            )
            return [dict(row._mapping) for row in rows]

    async def render(self) -> str:
        """The timeline so far: the test's, the mock server's, the watched runs' and the workers'."""
        extra: list[tuple[float, str, str]] = []
        for hit in list(self.mock.hits):
            held = " (held)" if hit.held else ""
            extra.append((hit.at, "mock", f"{hit.method} {hit.name}{held}"))
        for run_id in self.watched:
            try:
                activity = await self.events(run_id)
            except Exception as error:  # the database may be what failed
                extra.append((time.time(), "run", f"{run_id[:8]}: can't read its activity: {error}"))
                continue
            for event in activity:
                attributes = event.get("attributes") or {}
                worker = attributes.get("worker")
                more = (
                    f" [{worker.rsplit('.', 1)[-1] if isinstance(worker, str) else ''} epoch {attributes.get('epoch')}]"
                    if worker
                    else ""
                )
                extra.append(
                    (epoch(event["at"]), f"run {run_id[:8]}", f"{event['kind']}: {event['message'][:200]}{more}")
                )
        for worker in self.workers:
            extra.extend(worker.interesting())
        return self.timeline.render(extra)

    # ------------------------------------------------------------------ the scenario's end

    def end_workers(self) -> None:
        for worker in self.workers:
            worker.end()
        # What a killed worker's Bash steps left running (each in a session of its own).
        subprocess.run(["pkill", "-f", f"{self.mock.url}/effect/"], check=False, capture_output=True)


@contextlib.asynccontextmanager
async def opened(
    services: Services,
    mock: MockServer,
    folder: Path,
    *,
    name: str,
    reports: Path,
    outcome: Callable[[], str] = lambda: "done",
) -> AsyncIterator[Scenario]:
    """
    A scenario: its tables and Redis cleared, the mock server reset, a model
    provider configuration naming the mock. Once it's over, its workers are
    gone, and its report (what it observed, its timeline) and its workers'
    logs are in ``reports`` as ``<name>.txt`` and ``<name>.workers.log``; the
    report's text is ``Scenario.report``.
    """
    engine = mysql_engine(services.mysql_url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names())
    if "adk_runs" not in tables:
        await engine.dispose()
        raise RuntimeError("the suite's database isn't migrated: run make async-worker-test-integration")
    await clear(engine, services.redis_url)
    mock.reset()
    folder.mkdir(parents=True, exist_ok=True)
    provider = folder / "model_provider.yaml"
    provider.write_text(model_provider_yaml(f"{mock.url}/v1"))
    world = Scenario(
        services=services,
        engine=engine,
        store=RunStore(engine),
        queue=run_queue(services.redis_url),
        mock=mock,
        folder=folder,
        model_provider=provider,
    )
    try:
        yield world
    finally:
        world.end_workers()
        mock.release_all()
        try:
            timeline = await world.render()
        except Exception as error:  # the database may be what the scenario broke
            timeline = f"(the timeline can't be read: {error})\n{world.timeline.render()}"
        world.report = f"== observed\n{world.observed()}\n\n== timeline\n{timeline}\n"
        logs = "\n\n".join(f"== worker {w.name} (exit {w.returncode})\n{w.text()[-30000:]}" for w in world.workers)
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{name}.txt").write_text(f"{name}: {outcome()}\n\n{world.report}")
        (reports / f"{name}.workers.log").write_text(logs)
        await world.queue.close()
        await engine.dispose()


def played[T](
    services: Services,
    mock: MockServer,
    folder: Path,
    *,
    name: str,
    reports: Path,
    play: Callable[[Scenario], Awaitable[T]],
) -> T:
    """
    Plays a scenario once, on an event loop of its own (on a thread, so the
    tests' loops are left alone), for several tests to read what it saw: a
    module's fixture, each of whose tests holds one write, or one effect, to
    what it should be. Its report is ``<name>.txt``.
    """

    async def go() -> T:
        async with opened(services, mock, folder, name=name, reports=reports, outcome=lambda: "played") as world:
            return await play(world)

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, go()).result()


async def clear(engine: AsyncEngine, redis_url: str) -> None:
    """Every run, activity, usage row and ADK session gone, and the queue's Redis empty."""
    async with engine.begin() as conn:
        await conn.execute(sa.delete(events))
        await conn.execute(sa.delete(runs))
        await conn.execute(sa.delete(calls))
        await conn.execute(sa.delete(invocations))
        existing = set(await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
        for table in ADK_TABLES:
            if table in existing:
                await conn.execute(sa.text(f"DELETE FROM {table}"))
    queue = Queue.from_url(redis_url, name=QUEUE)
    try:
        await queue.redis.flushdb()
    finally:
        await queue.disconnect()


def run_queue(redis_url: str) -> RunQueue:
    return RunQueue(Queue.from_url(redis_url, name=QUEUE), upkeep=Queue.from_url(redis_url, name=UPKEEP))


def mysql_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


async def eventually(
    check: Callable[[], Awaitable[bool]], *, seconds: float, every: float = 0.2, what: str = ""
) -> None:
    deadline = time.monotonic() + seconds
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what or 'it'} didn't happen in {seconds:g}s")
        await asyncio.sleep(every)


def model_provider_yaml(base_url: str) -> str:
    """The worker's models: the mock server's, an OpenAI-compatible chat API."""
    return f"""version: 1
default:
  provider: mock
  model: mock-1
providers:
  mock:
    name: Mock
    baseUrl: {base_url}
    api: openai-completions
    auth:
      type: apiKey
      key: test-key
    models:
      - id: mock-1
        name: Mock 1
        reasoning: false
        input: [text]
        contextWindow: 100000
        maxTokens: 1000
        cost: {{ input: 1, output: 2, cacheRead: 0, cacheWrite: 0 }}
"""
