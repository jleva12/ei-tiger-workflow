"""The background tasks API over what the worker recorded: an organization's tasks, one
task's attempts and audit trail, and resubmit / restart / abandon (toy tasks,
the task framework in memory, fake SAQ queues)."""

from __future__ import annotations

import httpx
import pytest

from forge_async_worker.api import create_app
from forge_async_worker.saq_worker import restart_run, run_job
from forge_tasks.errors import StorageError

from .conftest import FakeJob, worker_settings

TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def note(tenant: str = "t1", note_id: str = "n1", **extra: object) -> dict:
    return {
        "task_type": "notes",
        "kind": "store",
        "payload": {"tenant_id": tenant, "note_id": note_id, "text": "hi", **extra},
    }


@pytest.fixture
async def api(worker):
    app = create_app(worker_settings(api={"token": TOKEN}), runs=worker.state.runs, queue=worker.state.queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        yield client


async def _run(worker, spec: dict, job: FakeJob | None = None) -> None:
    try:
        await run_job(worker.context(job), spec=spec)
    except Exception:
        pass  # a failure the worker records; the test reads it back


def test_the_api_needs_a_token():
    with pytest.raises(ValueError, match="HYBRID_API__TOKEN"):
        create_app(worker_settings())


async def test_every_call_but_health_needs_the_bearer_token(api):
    assert (await api.get("/health")).json() == {"status": "ok"}
    assert (await api.get("/v1/tasks")).status_code == 401
    assert (await api.get("/v1/tasks", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await api.get("/v1/tasks", headers=AUTH)).status_code == 200


async def test_an_organizations_tasks_newest_first_with_their_outcome(worker, api):
    await _run(worker, note(note_id="ok"))
    await _run(worker, note(note_id="bad", fail="permanent"))
    await _run(worker, note(tenant="t2", note_id="theirs"))
    await _run(worker, {"task_type": "notes", "kind": "split", "payload": {"tenant_id": "t1", "text": "a"}})

    page = (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()
    assert page["total"] == 3
    split, bad, ok = page["items"]
    assert (ok["job_name"], ok["task_type"], ok["kind"], ok["description"]) == (
        "notes.store",
        "notes",
        "store",
        "note ok",
    )
    assert (ok["status"], ok["outcome"], ok["attempts"], ok["failure"]) == ("COMPLETED", "ok", 1, None)
    assert ok["duration_ms"] is not None and ok["started_at"] and ok["ended_at"]
    assert (bad["status"], bad["failure"]["category"]) == ("FAILED", "permanent")
    assert "bad note" in bad["failure"]["message"]
    assert split["job_name"] == "notes.split" and split["description"] is None

    failed = (await api.get("/v1/tasks", params={"tenant": "t1", "status": "FAILED"}, headers=AUTH)).json()
    assert [t["id"] for t in failed["items"]] == [bad["id"]]
    other = (await api.get("/v1/tasks", params={"tenant": "t1", "task_type": "alerts"}, headers=AUTH)).json()
    assert other == {"items": [], "total": 0}
    rest = (await api.get("/v1/tasks", params={"tenant": "t1", "exclude_task_type": "notes"}, headers=AUTH)).json()
    assert rest == {"items": [], "total": 0}
    kept = (await api.get("/v1/tasks", params={"tenant": "t1", "exclude_task_type": "alerts"}, headers=AUTH)).json()
    assert kept["total"] == 3
    paged = (await api.get("/v1/tasks", params={"tenant": "t1", "limit": 1, "offset": 1}, headers=AUTH)).json()
    assert [t["id"] for t in paged["items"]] == [bad["id"]] and paged["total"] == 3


async def test_a_tasks_detail_tells_how_it_ran(worker, api, monkeypatch):
    runner = worker.runtime.runner
    real_run = runner.run

    async def boom(spec):
        raise StorageError("primary stepped down")

    monkeypatch.setattr(runner, "run", boom)
    job = FakeJob(queued=1_760_000_000_123)
    await _run(worker, note(), job)
    monkeypatch.setattr(runner, "run", real_run)
    await _run(worker, note(), job.redelivered())  # SAQ's retry: the second attempt

    [task] = (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()["items"]
    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    assert (detail["status"], detail["attempts"], detail["outcome"]) == ("COMPLETED", 2, "ok")
    assert detail["payload"] == note()["payload"]
    assert detail["labels"] == {"task_type": "notes", "kind": "store", "tenant": "t1"}
    assert detail["delivery"] == {"queue": "notes", "key": job.key, "enqueued_at": "2025-10-09T08:53:20.123000Z"}
    assert detail["result"]["status"] == "ok" and detail["result"]["detail"]["stored"] == 1
    second, first = detail["runs"]
    assert (second["attempt"], second["status"], second["restart_of"]) == (2, "COMPLETED", first["id"])
    [failure] = first["failures"]
    assert (failure["type"], failure["category"], failure["retryable"]) == (
        "forge_tasks.errors.StorageError",
        "transient",
        True,
    )
    assert "primary stepped down" in failure["message"] and "Traceback" in failure["stack_trace"]
    assert [s["name"] for s in second["steps"]] == ["run"] and second["steps"][0]["status"] == "COMPLETED"
    types = [e["type"] for e in detail["events"]]
    assert types[0] == "RUN_CREATED" and "FAILED" in types and types[-1] == "COMPLETED"
    assert detail["actions"] == {"resubmit": True, "restart": False, "abandon": False, "decide": False}
    assert (await api.get("/v1/tasks/nope", headers=AUTH)).status_code == 404


async def test_resubmit_sends_the_same_job_as_a_new_task(worker, api):
    await _run(worker, note())
    [task] = (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()["items"]
    worker.queues["notes"].sent.clear()

    answer = await api.post(f"/v1/tasks/{task['id']}/resubmit", json={"actor": {"id": "lead-1"}}, headers=AUTH)
    assert answer.status_code == 202
    queued = answer.json()
    ((function, kwargs),) = worker.queues["notes"].sent
    assert function == "run_job" and kwargs["key"] == queued["key"] and queued["queue"] == "notes"
    assert kwargs["spec"] == {**note(), "tenant_id": "t1"}
    assert queued["key"].startswith(f"resubmit:{task['id']}:")

    # The worker runs it: a task of its own.
    await _run(worker, kwargs["spec"], FakeJob(key=queued["key"]))
    assert (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()["total"] == 2


async def test_restart_queues_the_next_attempt_for_a_worker(worker, api):
    await _run(worker, note(fail="bug"))
    [task] = (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()["items"]
    assert (task["status"], task["failure"]["category"]) == ("FAILED", "error")
    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    assert detail["actions"] == {"resubmit": True, "restart": True, "abandon": True, "decide": False}
    worker.queues["notes"].sent.clear()

    body = {"actor": {"id": "lead-1", "display_name": "Lee"}}
    answer = await api.post(f"/v1/tasks/{task['id']}/restart", json=body, headers=AUTH)
    assert answer.status_code == 202 and answer.json() == {"queue": "notes", "key": f"restart:{task['id']}:1"}
    ((function, kwargs),) = worker.queues["notes"].sent
    assert (function, kwargs["instance_id"], kwargs["actor"]) == ("restart_run", task["id"], body["actor"])

    # A worker runs it (the bug fixed): attempt 2, restarted by the person who asked.
    store = worker.runtime.notes

    async def fixed(spec):
        store.notes.append("fixed")
        from forge_tasks.runner import ok

        return ok(stored=len(store.notes))

    worker.runtime.runner.run = fixed  # type: ignore[method-assign]
    out = await restart_run(worker.context(), instance_id=task["id"], actor=body["actor"])
    assert out["status"] == "ok"
    detail = (await api.get(f"/v1/tasks/{task['id']}", headers=AUTH)).json()
    assert (detail["status"], detail["attempts"]) == ("COMPLETED", 2)
    [restarted] = [e for e in detail["events"] if e["type"] == "RESTARTED"]
    assert restarted["actor"] == {"kind": "HUMAN", "id": "lead-1", "display_name": "Lee"}

    again = await api.post(f"/v1/tasks/{task['id']}/restart", json=body, headers=AUTH)
    assert again.status_code == 409 and "completed" in again.json()["detail"]


async def test_abandon_gives_up_on_a_failed_task(worker, api):
    await _run(worker, note(fail="permanent"))
    [task] = (await api.get("/v1/tasks", params={"tenant": "t1"}, headers=AUTH)).json()["items"]

    answer = await api.post(f"/v1/tasks/{task['id']}/abandon", json={"actor": {"id": "lead-1"}}, headers=AUTH)
    assert answer.status_code == 200
    detail = answer.json()
    assert detail["status"] == "ABANDONED"
    assert detail["actions"] == {"resubmit": True, "restart": False, "abandon": False, "decide": False}
    [abandoned] = [e for e in detail["events"] if e["to_status"] == "ABANDONED"]
    assert abandoned["actor"]["id"] == "lead-1"
    again = await api.post(f"/v1/tasks/{task['id']}/abandon", headers=AUTH)
    assert again.status_code == 409
    assert (await api.post(f"/v1/tasks/{task['id']}/restart", headers=AUTH)).status_code == 409
