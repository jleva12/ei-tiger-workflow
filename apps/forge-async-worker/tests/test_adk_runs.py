"""
ADK workflow runs end to end on the worker: the real ADK workflows task
(Google ADK's graph engine, its sessions in SQLite), run by run_adk on a
SQLite run store, through an approval, its timeout, a question and a failed
step. The task's own tests (packages/python/tasks/adk-workflows) cover the
graph; these, that the worker's control carries a run through it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.adk.sessions import DatabaseSessionService

from forge_async_worker import saq_worker
from forge_async_worker.queue import RunQueue
from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunServices
from forge_task_adk_workflows.models import NoModels
from forge_task_adk_workflows.run_store import FAILED, PAUSED, QUEUED, SUCCEEDED, RunStore
from forge_task_adk_workflows.task import SERVICES, SESSIONS, AdkWorkflowsTaskFactory
from forge_tasks.tasks import TaskRegistry

from .conftest import ALICE, BOB, START, Clock, FakeSaqQueue, Worker, at, worker_settings

SESSION = "5b1e0c4e-0000-4000-8000-00000000c0de"
REVIEW = "review@test_agent@1/review@1"


def node(node_id: str, kind: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "kind": kind, "name": node_id.title(), "config": config or {}, "outputs": []}


def agent(nodes: list[dict[str, Any]], edges: list[tuple[str, str, str]]) -> dict[str, Any]:
    """A forge.agent/v1 document: its nodes, and edges (source, output, target)."""
    return {
        "format": "forge.agent/v1",
        "id": "ag_test000001",
        "name": "Test agent",
        "description": "",
        "organization_id": "org-1",
        "nodes": [node("start", "start", {"input_schema": {}}), *nodes],
        "edges": [{"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t} for s, o, t in edges],
        "layout": {},
    }


def end(node_id: str, result: str, outcome: str = "succeeded") -> dict[str, Any]:
    return node(node_id, "end", {"outcome": outcome, "result": result})


APPROVAL = agent(
    [
        node(
            "review", "approval", {"message": "Ship {{ input.issue }}?", "approvers": "org:admin", "timeout_hours": 1}
        ),
        end("yes", "steps.review.output"),
        end("no", "steps.review.output"),
    ],
    [("start", "next", "review"), ("review", "approved", "yes"), ("review", "rejected", "no")],
)
QUESTION = agent(
    [
        node(
            "ask",
            "human_input",
            {
                "message": "How many?",
                "response_schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
            },
        ),
        end("done", '{"answer": steps.ask.output}'),
    ],
    [("start", "next", "ask"), ("ask", "next", "done")],
)
FAILING = agent(
    [node("bad", "transform", {"expression": "$error('boom')"}), end("done", "")],
    [("start", "next", "bad"), ("bad", "next", "done")],
)


@pytest.fixture
async def adk(tmp_path: Path, store: RunStore, clock: Clock) -> AsyncIterator[Worker]:
    """The worker, with the real ADK workflows task: its sessions in SQLite, the tests' clock."""
    sessions = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    settings = AdkWorkflowsSettings()
    services = RunServices.of(
        settings,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        clock=clock,
        sleep=clock.sleep,
        model=NoModels(),
    )
    saq = FakeSaqQueue()
    state = await saq_worker.start_state(
        worker_settings(),
        registry=TaskRegistry([AdkWorkflowsTaskFactory()]),
        store=store,
        queue=RunQueue(saq),
        options={"adk_workflows": settings},
        extras={SERVICES: services, SESSIONS: sessions},
        redis_url=None,
    )
    yield Worker(state, saq, clock)
    await saq_worker.close_state(state)


async def started(worker: Worker, document: dict[str, Any], value: Any) -> dict[str, Any]:
    """A run as the admin API starts it."""
    return await worker.store.create(
        organization_id="org-1",
        agent_id=document["id"],
        agent_name="Test agent",
        revision=3,
        session_id=SESSION,
        payload={
            "tenant_id": "org-1",
            "agent_id": document["id"],
            "revision": 3,
            "name": "Test agent",
            "document": document,
            "input": value,
            "session_id": SESSION,
            "run_as": ALICE.id,
            "run_as_name": ALICE.name,
            "trigger": {"kind": "manual"},
        },
        requested_by=ALICE,
    )


async def notes(worker: Worker, run_id: str) -> list[str]:
    return [event["message"] for event in await worker.store.events(run_id) if event["kind"] == "note"]


async def test_an_approval_pauses_the_run_and_its_decision_carries_it_on(adk: Worker) -> None:
    run = await started(adk, APPROVAL, {"issue": "XMEN-12"})

    assert await adk.run(run["id"]) == "paused"

    paused = await adk.get(run["id"])
    pause = paused["pause"]
    assert paused["status"] == PAUSED
    assert (pause["key"], pause["kind"], pause["reason"]) == (REVIEW, "approval", "Ship XMEN-12?")
    assert pause["details"] == {
        "kind": "approval",
        "approvers": "org:admin",
        "expires_at": "2026-10-01T13:00:00+00:00",
        "step": "review",
        "step_name": "Review",
        "workflow_name": "Test agent",
        "agent_id": "ag_test000001",
        "message": "Ship XMEN-12?",
        "session_id": SESSION,
        "interrupt_id": REVIEW,
    }
    # The deadline, a second past the step's (so it's past it on every clock), and its expiry then.
    assert pause["deadline"] == "2026-10-01T13:00:01+00:00"
    [(function, kwargs)] = adk.sent
    assert (function, kwargs["pause_id"], kwargs["scheduled"]) == ("expire_pause", pause["id"], at(START) + 3601)
    assert paused["state"]["adk_workflow"]["session_id"] == SESSION

    adk.clock.advance(600)
    await adk.store.decide(run["id"], request_id=pause["id"], approved=True, comment="Looks right", actor=BOB)
    assert await adk.run(run["id"]) == "succeeded"

    done = await adk.get(run["id"])
    assert done["status"] == SUCCEEDED
    assert done["result"] == {
        "approved": True,
        "decided_by": "Bob",
        "comment": "Looks right",
        "decided_at": "2026-10-01T12:10:00+00:00",
    }
    assert "Review (review): approved by Bob" in await notes(adk, run["id"])
    assert (await notes(adk, run["id"]))[-1] == "The run succeeded"


async def test_an_approval_nobody_decides_in_time_is_rejected_by_forge(adk: Worker) -> None:
    run = await started(adk, APPROVAL, {"issue": "XMEN-12"})
    await adk.run(run["id"])
    pause = (await adk.get(run["id"]))["pause"]

    adk.clock.advance(3601)
    assert (await adk.state.jobs.expire_pause(run["id"], pause["id"]))["outcome"] == "timed_out"
    assert (await adk.get(run["id"]))["status"] == QUEUED
    assert await adk.run(run["id"]) == "succeeded"

    result = (await adk.get(run["id"]))["result"]
    assert (result["approved"], result["decided_by"]) == (False, "Forge")
    assert "Review (review): no decision in time" in await notes(adk, run["id"])


async def test_a_question_waits_for_its_answer(adk: Worker) -> None:
    run = await started(adk, QUESTION, {})

    assert await adk.run(run["id"]) == "paused"

    pause = (await adk.get(run["id"]))["pause"]
    assert pause["kind"] == "human_input" and pause["deadline"] is None
    assert pause["details"]["response_schema"]["properties"] == {"count": {"type": "integer"}}
    assert adk.sent == []

    answer = json.dumps({"count": 3})
    await adk.store.decide(run["id"], request_id=pause["id"], approved=True, comment=answer, actor=BOB)
    assert await adk.run(run["id"]) == "succeeded"
    assert (await adk.get(run["id"]))["result"] == {"answer": {"count": 3}}


async def test_a_failed_step_fails_the_run_naming_it(adk: Worker) -> None:
    run = await started(adk, FAILING, {})

    assert await adk.run(run["id"]) == "failed"

    failed = await adk.get(run["id"])
    assert failed["status"] == FAILED
    assert failed["error"]["category"] == "failed" and failed["error"]["step"] == "bad"
    assert failed["error"]["message"].startswith("Bad (bad) failed: ") and "boom" in failed["error"]["message"]
