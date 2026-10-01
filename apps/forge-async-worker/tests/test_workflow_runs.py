"""Workflow runs in the worker: a run pauses at an approval and carries on
after a decision taken through the API; a long delay stops the run and a
scheduled resume carries it on; runs are found by their labels. The workflows
task, the task framework in memory, fake SAQ queues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from forge_async_worker.api import create_app
from forge_async_worker.config import WorkerSettings
from forge_async_worker.queue import SaqJobQueue
from forge_async_worker.saq_worker import JOB_OPTIONS, close_state, decide_run, resume_run, run_job, start_state
from forge_task_workflows.config import WorkflowsSettings
from forge_task_workflows.services.base import Services
from forge_task_workflows.task import WorkflowsTaskFactory
from forge_tasks.tasks import TaskRegistry

from .conftest import FakeJob, FakeLocks, FakeQueue, FakeWorker

TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def document(nodes: list[tuple[str, str, dict]], edges: list[tuple[str, str, str]]) -> dict:
    return {
        "format": "forge.workflow/v1",
        "id": "wf_test",
        "name": "Triage",
        "organization_id": ORGANIZATION,
        "entry": nodes[0][0],
        "nodes": [{"id": i, "kind": k, "name": i.title(), "config": c, "outputs": []} for i, k, c in nodes],
        "edges": [{"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t} for s, o, t in edges],
    }


def run_spec(doc: dict, *, input: dict | None = None) -> dict:
    return {
        "task_type": "workflows",
        "kind": "run",
        "tenant_id": ORGANIZATION,
        "labels": {"workflow": doc["id"]},
        "requested_by": {"id": "member-1", "display_name": "Mia Member"},
        "payload": {
            "tenant_id": ORGANIZATION,
            "workflow_id": doc["id"],
            "name": doc["name"],
            "document": doc,
            "input": input or {},
            "run_as": "member-1",
        },
    }


class Harness:
    def __init__(self, state, queue: FakeQueue, clock: Clock) -> None:
        self.state, self.queue, self.clock = state, queue, clock
        self.locks = FakeLocks()

    def context(self, job: FakeJob | None = None) -> dict:
        return {"worker": FakeWorker(self.queue), "job": job or FakeJob(), "forge": self.state, "redis": self.locks}

    def sent(self, function: str) -> list[dict]:
        return [kwargs for name, kwargs in self.queue.sent if name == function]


@pytest.fixture
async def harness():
    clock = Clock()
    queue = FakeQueue("workflows")
    services = Services(
        settings=WorkflowsSettings(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        clock=clock,
        sleep=clock.sleep,
    )
    settings = WorkerSettings(
        _env_file=None, enabled_tasks=["workflows"], etf={"store": "memory"}, api={"token": TOKEN}
    )  # type: ignore[call-arg]
    state = await start_state(
        settings,
        registry=TaskRegistry([WorkflowsTaskFactory()]),
        queue=SaqJobQueue({"workflows": queue}, job_options=JOB_OPTIONS),
        redis_url=None,
        extras={"workflows.services": services},
    )
    yield Harness(state, queue, clock)
    await close_state(state)


@pytest.fixture
async def api(harness):
    app = create_app(harness.state.settings, runs=harness.state.runs, queue=harness.state.queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        yield client


APPROVAL = document(
    [
        ("start", "entry", {}),
        ("shape", "transform", {"expression": '{"key": input.key}'}),
        ("review", "approval", {"message": "Ship {{ input.key }}?", "approvers": "org:admin"}),
        ("shipped", "end", {"outcome": "succeeded", "result": '"shipped: " & steps.review.output.comment'}),
        ("held", "end", {"outcome": "succeeded", "result": '"held by " & steps.review.output.decided_by'}),
    ],
    [
        ("start", "next", "shape"),
        ("shape", "next", "review"),
        ("review", "approved", "shipped"),
        ("review", "rejected", "held"),
    ],
)


async def test_a_run_pauses_for_approval_and_carries_on_after_the_decision(harness, api):
    out = await run_job(harness.context(), spec=run_spec(APPROVAL, input={"key": "XMEN-12"}))
    assert out["status"] == "skipped" and out["detail"]["reason"] == "run awaiting_validation"

    page = (
        await api.get("/v1/tasks", params={"tenant": ORGANIZATION, "label": "workflow:wf_test"}, headers=AUTH)
    ).json()
    [task] = page["items"]
    assert task["awaiting_approval"] is True and task["description"] == "Triage"
    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    assert detail["labels"]["workflow"] == "wf_test"
    assert detail["requested_by"] == {"kind": "HUMAN", "id": "member-1", "display_name": "Mia Member"}
    assert detail["actions"]["decide"] is True
    approval = detail["approval"]
    assert approval["reason"] == "Ship XMEN-12?"
    assert approval["details"]["approvers"] == "org:admin" and approval["details"]["key"] == "review#1"

    decided = await api.post(
        f"/v1/tasks/{task['id']}/decisions",
        json={
            "request_id": approval["id"],
            "approved": True,
            "comment": "go",
            "actor": {"id": "lead-1", "display_name": "Lee Lead"},
        },
        headers=AUTH,
    )
    assert decided.status_code == 202
    [job] = harness.sent("decide_run")
    job = {k: v for k, v in job.items() if k in ("instance_id", "request_id", "gate", "approved", "comment", "actor")}
    result = await decide_run(harness.context(), **job)
    assert result["status"] == "ok" and result["detail"]["result"] == "shipped: go"

    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    assert detail["status"] == "COMPLETED" and detail["approval"] is None
    notes = [e["message"] for e in detail["events"] if e["type"] == "ANNOTATION"]
    assert "Review (review): approved by Lee Lead" in notes
    # Shape ran once, before the pause; the run carried on after it.
    assert sum(1 for n in notes if n.startswith("Shape (shape) finished")) == 1


async def test_a_rejection_carries_the_run_down_its_rejected_way(harness, api):
    await run_job(harness.context(), spec=run_spec(APPROVAL, input={"key": "XMEN-12"}))
    [task] = (await api.get("/v1/tasks", params={"tenant": ORGANIZATION}, headers=AUTH)).json()["items"]
    approval = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()["approval"]
    await api.post(
        f"/v1/tasks/{task['id']}/decisions",
        json={
            "request_id": approval["id"],
            "approved": False,
            "comment": "not yet",
            "actor": {"id": "lead-1", "display_name": "Lee Lead"},
        },
        headers=AUTH,
    )
    [job] = harness.sent("decide_run")
    job = {k: v for k, v in job.items() if k in ("instance_id", "request_id", "gate", "approved", "comment", "actor")}
    result = await decide_run(harness.context(), **job)
    assert result["detail"]["result"] == "held by Lee Lead"


async def test_a_long_delay_lets_the_worker_go_until_its_resume(harness, api):
    doc = document(
        [
            ("start", "entry", {}),
            ("wait", "delay", {"amount": 3, "unit": "hours"}),
            ("done", "end", {"outcome": "succeeded", "result": '"woke"'}),
        ],
        [("start", "next", "wait"), ("wait", "next", "done")],
    )
    out = await run_job(harness.context(), spec=run_spec(doc))
    assert out["detail"]["reason"] == "run stopped"
    [resume] = harness.sent("resume_run")
    assert resume["scheduled"] == int((harness.clock.now + timedelta(hours=3)).timestamp())
    [task] = (await api.get("/v1/tasks", params={"tenant": ORGANIZATION}, headers=AUTH)).json()["items"]
    assert task["status"] == "STOPPED" and task["waiting_until"].startswith("2026-09-26T15:00:00")
    assert "a delay of 3 hours" in task["waiting_reason"]

    harness.clock.now += timedelta(hours=3)
    result = await resume_run(harness.context(), instance_id=resume["instance_id"])
    assert result["status"] == "ok" and result["detail"]["result"] == "woke"
    # Every checkpoint on the task's activity names the step it was saved at,
    # the wait with why it waits.
    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    saved = [e["message"] for e in detail["events"] if e["type"] == "CHECKPOINT_SAVED"]
    assert "Wait (wait): a delay of 3 hours" in saved
    assert "waiting until 2026-09-26T15:00:00+00:00: Wait (wait): a delay of 3 hours" in saved
    assert {"Start (start)", "Wait (wait)", "Done (done)", "The run succeeded"} <= set(saved)
    # A second resume of the same run does nothing: it isn't waiting.
    again = await resume_run(harness.context(), instance_id=resume["instance_id"])
    assert again["status"] == "skipped"


async def test_a_decision_for_an_approval_no_longer_open_does_nothing(harness, api):
    await run_job(harness.context(), spec=run_spec(APPROVAL, input={"key": "XMEN-12"}))
    [task] = (await api.get("/v1/tasks", params={"tenant": ORGANIZATION}, headers=AUTH)).json()["items"]
    result = await decide_run(harness.context(), instance_id=task["id"], request_id="someone-else", approved=True)
    assert result["status"] == "skipped"
    refused = await api.post(
        f"/v1/tasks/{task['id']}/decisions",
        json={"request_id": "someone-else", "approved": True, "actor": {"id": "lead-1"}},
        headers=AUTH,
    )
    assert refused.status_code == 409
