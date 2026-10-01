"""Two toy task types for the worker's tests, so they exercise the worker, not
any real task package: ``notes`` (a lock per note, a fan-out, schedules) and
``alerts`` (its own queue, no schedules)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from forge_tasks.errors import TaskError, TransientError
from forge_tasks.runner import ok
from forge_tasks.tasks import JobResult, JobSpec, Schedule, TaskContext, TaskRegistry


class NotePayload(BaseModel):
    tenant_id: str
    note_id: str
    text: str = ""
    fail: str | None = None  # "transient", "permanent" or "bug"


class StoreNote:
    name = "store"
    payload_model = NotePayload

    def __init__(self, task: NotesTask) -> None:
        self.task = task

    def lock_key(self, p: NotePayload) -> str:
        return f"{p.tenant_id}:{p.note_id}"

    def describe(self, p: NotePayload) -> str:
        return f"note {p.note_id}"

    async def run(self, p: NotePayload) -> JobResult:
        if p.fail == "transient":
            raise TransientError("try later")
        if p.fail == "permanent":
            raise TaskError("bad note")
        if p.fail == "bug":
            raise RuntimeError("oops")
        self.task.notes.append(p.text)
        return ok(stored=len(self.task.notes))


class SplitPayload(BaseModel):
    tenant_id: str
    text: str


class SplitNotes:
    """Fans out: one ``store`` per ``|``-separated part."""

    name = "split"
    payload_model = SplitPayload

    def __init__(self, task: NotesTask) -> None:
        self.task = task

    def lock_key(self, p: SplitPayload) -> None:
        return None

    async def run(self, p: SplitPayload) -> JobResult:
        parts = p.text.split("|")
        return JobResult(
            detail={"parts": len(parts)},
            followups=[
                JobSpec(task_type="notes", kind="store", payload={"tenant_id": p.tenant_id, "note_id": t, "text": t})
                for t in parts
            ],
        )


class NotesTask:
    name = queue = "notes"

    def __init__(self, ctx: TaskContext) -> None:
        self.notes: list[str] = []
        self.jobs: dict[str, Any] = {"store": StoreNote(self), "split": SplitNotes(self)}

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None


class NotesFactory:
    name = queue = "notes"
    schedules = [
        Schedule(
            name="notes.nightly",
            job=JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "a|b"}),
            cron="17 3 * * *",
        ),
        Schedule(
            name="notes.sweep",
            job=JobSpec(task_type="notes", kind="split", payload={"tenant_id": "t", "text": "c"}),
            every_seconds=900,
        ),
    ]

    def build(self, ctx: TaskContext) -> NotesTask:
        return NotesTask(ctx)


class AlertsTask:
    name = queue = "alerts"

    def __init__(self, ctx: TaskContext) -> None:
        self.jobs: dict[str, Any] = {}

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None


class AlertsFactory:
    name = queue = "alerts"
    schedules: list[Schedule] = []

    def build(self, ctx: TaskContext) -> AlertsTask:
        return AlertsTask(ctx)


def toy_registry() -> TaskRegistry:
    return TaskRegistry([NotesFactory(), AlertsFactory()])
