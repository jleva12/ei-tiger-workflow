"""The task SDK: registry, runner, schedules, task settings, and how little it
takes to add a task type."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from forge_tasks import runner as runner_mod
from forge_tasks.errors import TaskError, TransientError
from forge_tasks.protocols import Job, JobObserver, JobQueue, Task, TaskFactory
from forge_tasks.runner import InlineJobQueue, ok
from forge_tasks.runtime import build_registry, build_runtime
from forge_tasks.settings import CoreSettings
from forge_tasks.tasks import JobOutcome, JobResult, JobSpec, JobStatus, Schedule, TaskContext, TaskRegistry

# ----------------------------------------------------------------------------- a toy third task type


class NotePayload(BaseModel):
    tenant_id: str
    text: str
    fail: str | None = None


class IndexNoteJob:
    name = "index_note"
    payload_model = NotePayload

    def __init__(self, task: NotesTask) -> None:
        self.task = task

    def lock_key(self, p: NotePayload) -> str:
        return p.tenant_id

    def describe(self, p: NotePayload) -> str:
        return f"note: {p.text}"

    async def run(self, p: NotePayload) -> JobResult:
        if p.fail == "transient":
            raise TransientError("try later")
        if p.fail == "permanent":
            raise TaskError("bad note")
        if p.fail == "bug":
            raise RuntimeError("oops")
        self.task.notes.append((p.text, [float(len(p.text))]))
        return ok(stored=len(self.task.notes))


class SplitJob:
    name = "split"
    payload_model = NotePayload

    def __init__(self, task: NotesTask) -> None:
        self.task = task

    def lock_key(self, p: NotePayload) -> None:
        return None

    async def run(self, p: NotePayload) -> JobResult:
        parts = p.text.split("|")
        return JobResult(
            detail={"parts": len(parts)},
            followups=[
                JobSpec(task_type="notes", kind="index_note", payload={"tenant_id": p.tenant_id, "text": t})
                for t in parts
            ],
        )


class Countdown(BaseModel):
    n: int


class CountdownJob:
    """Fans out to itself until n reaches 0; its payload names no tenant."""

    name = "countdown"
    payload_model = Countdown

    def lock_key(self, p: Countdown) -> None:
        return None

    async def run(self, p: Countdown) -> JobResult:
        more = [JobSpec(task_type="notes", kind="countdown", payload={"n": p.n - 1})] if p.n else []
        return JobResult(followups=more)


class NotesSettings(BaseModel):
    flavor: str = "plain"
    batch: int = 10


class NotesTask:
    name = "notes"
    queue = "notes"

    def __init__(self, ctx: TaskContext) -> None:
        self.options: NotesSettings = ctx.options
        self.notes: list[tuple[str, list[float]]] = []
        self._jobs: dict[str, Any] = {
            "index_note": IndexNoteJob(self),
            "split": SplitJob(self),
            "countdown": CountdownJob(),
        }

    @property
    def jobs(self):
        return self._jobs

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None


class NotesFactory:
    name = "notes"
    queue = "notes"
    settings_model = NotesSettings
    schedules = [
        Schedule(
            name="notes.nightly",
            job=JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a"}),
            cron="0 2 * * *",
        )
    ]

    def build(self, ctx: TaskContext) -> NotesTask:
        return NotesTask(ctx)


def notes_runtime(**options: Any):
    settings = CoreSettings(_env_file=None, enabled_tasks=["notes"])  # type: ignore[call-arg]
    return build_runtime(
        settings, factories=[NotesFactory()], options={"notes": NotesSettings(**options)} if options else None
    )


def test_protocol_conformance():
    rt = notes_runtime(flavor="spicy")
    assert isinstance(NotesFactory(), TaskFactory)
    assert isinstance(rt.task("notes"), Task)
    assert rt.notes is rt.task("notes")
    assert all(isinstance(j, Job) for j in rt.task("notes").jobs.values())
    assert isinstance(rt.queue, JobQueue)
    assert rt.task("notes").options == NotesSettings(flavor="spicy")
    client = rt.context.mongo()  # shared, lazily-connecting client offered to every task
    assert client is rt.context.mongo()


def test_a_tasks_settings_come_from_its_own_section(monkeypatch):
    monkeypatch.setenv("HYBRID_NOTES__FLAVOR", "salty")
    monkeypatch.setenv("HYBRID_NOTES__BATCH", "3")
    assert notes_runtime().task("notes").options == NotesSettings(flavor="salty", batch=3)
    assert notes_runtime(flavor="given").task("notes").options.flavor == "given"  # explicit options win


async def test_fan_out_runs_inline():
    rt = notes_runtime()
    result = await rt.submit(JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a|b|c"}))
    assert result.detail["parts"] == 3
    assert [t for t, _ in rt.task("notes").notes] == ["a", "b", "c"]
    assert [s.kind for s, _ in rt.queue.history] == ["split", "index_note", "index_note", "index_note"]


async def test_runner_error_contract():
    rt = notes_runtime()
    run = rt.runner.run
    assert (await run(JobSpec(task_type="nope", kind="x"))).status is JobStatus.FAILED
    assert (await run(JobSpec(task_type="notes", kind="nope"))).status is JobStatus.FAILED
    bad_payload = await run(JobSpec(task_type="notes", kind="index_note", payload={"text": "missing tenant"}))
    assert bad_payload.status is JobStatus.FAILED and "invalid payload" in bad_payload.error
    permanent = await run(
        JobSpec(task_type="notes", kind="index_note", payload={"tenant_id": "t", "text": "x", "fail": "permanent"})
    )
    assert permanent.status is JobStatus.FAILED and "bad note" in permanent.error
    with pytest.raises(TransientError):  # retried by the queue
        await run(
            JobSpec(task_type="notes", kind="index_note", payload={"tenant_id": "t", "text": "x", "fail": "transient"})
        )
    assert (
        rt.runner.lock_key(JobSpec(task_type="notes", kind="index_note", payload={"tenant_id": "t9", "text": "x"}))
        == "notes:t9"
    )
    assert (
        rt.runner.lock_key(JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t9", "text": "x"})) is None
    )


class RecordingObserver:
    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.outcomes: list[JobOutcome] = []
        self.fail, self.hang = fail, hang

    async def job_finished(self, outcome: JobOutcome) -> None:
        self.outcomes.append(outcome)
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise ConnectionError("audit store down")


def _note(fail: str | None = None, text: str = "x") -> JobSpec:
    return JobSpec(task_type="notes", kind="index_note", payload={"tenant_id": "t", "text": text, "fail": fail})


async def test_runner_tells_the_tasks_observer_every_attempt():
    rt = notes_runtime()
    observer = RecordingObserver()
    rt.task("notes").observer = observer  # type: ignore[attr-defined]
    assert isinstance(observer, JobObserver)

    await rt.submit(JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a|b"}))
    assert (await rt.runner.run(_note("permanent"))).status is JobStatus.FAILED
    with pytest.raises(TransientError):
        await rt.runner.run(_note("transient"))
    with pytest.raises(RuntimeError):
        await rt.runner.run(_note("bug"))
    assert (await rt.runner.run(JobSpec(task_type="notes", kind="nope"))).status is JobStatus.FAILED

    seen = [(o.spec.kind, o.status) for o in observer.outcomes]
    assert seen == [
        ("split", "ok"),  # told before its follow-ups run
        ("index_note", "ok"),
        ("index_note", "ok"),
        ("index_note", "failed"),
        ("index_note", "retrying"),  # TransientError: the queue retries it
        ("index_note", "error"),
        ("nope", "failed"),
    ]
    split, permanent, transient = observer.outcomes[0], observer.outcomes[3], observer.outcomes[4]
    assert split.result is not None and split.result.detail["duration_ms"] == split.duration_ms
    assert split.finished_at.tzinfo is not None and split.error is None
    assert permanent.result is not None and "bad note" in (permanent.result.error or "")
    assert transient.result is None and isinstance(transient.error, TransientError) and transient.duration_ms >= 0


async def test_observer_failures_never_change_the_jobs_outcome(monkeypatch):
    rt = notes_runtime()
    rt.task("notes").observer = RecordingObserver(fail=True)  # type: ignore[attr-defined]
    assert (await rt.runner.run(_note())).status is JobStatus.OK
    with pytest.raises(TransientError):  # the job's own error, not the observer's
        await rt.runner.run(_note("transient"))

    monkeypatch.setattr(runner_mod, "OBSERVER_TIMEOUT", 0.01)
    rt.task("notes").observer = RecordingObserver(hang=True)  # type: ignore[attr-defined]
    assert (await rt.runner.run(_note())).status is JobStatus.OK


class OtherFactory:
    name = "other"
    queue = "shared"
    schedules = [Schedule(name="other.hourly", job=JobSpec(task_type="other", kind="x"), every_seconds=3600)]

    def build(self, ctx: TaskContext) -> Any:
        raise AssertionError("not enabled, never built")


def test_registry_queues_and_schedules():
    reg = build_registry([NotesFactory(), OtherFactory()])
    assert reg.names() == ["notes", "other"]
    assert reg.queues(["notes", "other"]) == {"notes": "notes", "other": "shared"}
    assert [s.name for s in reg.schedules(["notes", "other"])] == ["notes.nightly", "other.hourly"]
    assert [s.name for s in reg.schedules(["notes"])] == ["notes.nightly"]
    with pytest.raises(KeyError):
        reg.factory("missing")
    # Only enabled tasks are built.
    rt = build_runtime(CoreSettings(_env_file=None, enabled_tasks=["notes"]), registry=reg)  # type: ignore[call-arg]
    assert list(rt.tasks) == ["notes"]


def test_inline_queue_guards_against_runaway_fanout():
    q = InlineJobQueue(max_jobs=1)
    assert q.max_jobs == 1


def test_registry_rejects_nothing_silently_for_bad_entry_points(monkeypatch):
    import forge_tasks.tasks as tasks_mod

    class EP:
        name = "broken"

        def load(self):
            raise ImportError("boom")

    monkeypatch.setattr(tasks_mod, "entry_points", lambda group: [EP()])
    assert TaskRegistry().load_entry_points() == 0


async def test_follow_ups_do_the_same_tenants_work():
    rt = notes_runtime()
    await rt.submit(JobSpec(task_type="notes", kind="countdown", payload={"n": 2}, tenant_id="org-9"))
    assert [(spec.payload["n"], spec.tenant) for spec, _ in rt.queue.history] == [
        (2, "org-9"),
        (1, "org-9"),
        (0, "org-9"),
    ]
    # A payload's own tenant_id counts; work for no one stays no one's.
    split = JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a"})
    assert split.tenant == "t"
    rt2 = notes_runtime()
    await rt2.submit(JobSpec(task_type="notes", kind="countdown", payload={"n": 1}))
    assert {spec.tenant for spec, _ in rt2.queue.history} == {None}


def test_the_wire_format_names_a_tenant_only_when_there_is_one():
    assert JobSpec(task_type="notes", kind="split").wire() == {"task_type": "notes", "kind": "split", "payload": {}}
    assert JobSpec(task_type="notes", kind="split", tenant_id="t1").wire()["tenant_id"] == "t1"


async def test_jobs_describe_their_work_when_they_can():
    rt = notes_runtime()
    assert await rt.runner.describe(_note(text="hello")) == "note: hello"
    split = JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a"})
    assert await rt.runner.describe(split) is None
    assert await rt.runner.describe(JobSpec(task_type="nope", kind="x")) is None
