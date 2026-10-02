"""
The adk_workflows task's run job, as the worker runs it: re-entrant, carried
on from its ADK session (ADK's DatabaseSessionService, on SQLite here) each
time the task framework runs it again, with the task framework's control
kept in memory (LocalJobControl), a fake HTTP transport, a clock the tests
move and scripted models.
"""

import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_common.adk.models import ModelCall, ProviderModels
from forge_common.model_provider import ModelProviderConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import errors, types

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunServices
from forge_task_adk_workflows.models import NOT_SET_UP, NoModels
from forge_task_adk_workflows.runs import APP_NAME, RunPayload
from forge_task_adk_workflows.task import (
    NO_SESSIONS,
    SERVICES,
    SESSIONS,
    AdkWorkflowsTask,
    AdkWorkflowsTaskFactory,
)
from forge_tasks.control import (
    AwaitingDecision,
    Decision,
    LocalJobControl,
    WaitingUntil,
    controlling,
)
from forge_tasks.errors import TransientError
from forge_tasks.settings import load_section
from forge_tasks.tasks import JobResult, JobStatus, TaskContext, TaskRegistry

from .scripted_llm import ScriptedLlm
from .test_graph import (
    START_NODE,
    Clock,
    agent,
    approval_agent,
    end,
    llm_config,
    loop_agent,
    node,
    transform,
)

ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"
MEMBER = "member-1"
SESSION = "5b1e0c4e-0000-4000-8000-00000000c0de"
REVIEW = "review@test_agent@1/review@1"


# ---------------------------------------------------------------------- helpers


@dataclass
class Control(LocalJobControl):
    """LocalJobControl, recording what each gate was asked with."""

    gates: list[dict[str, Any]] = field(default_factory=list)

    async def approval(
        self,
        *,
        key: str,
        reason: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Decision:
        self.gates.append({"key": key, "reason": reason, "details": details, "timeout_seconds": timeout_seconds})
        return await super().approval(key=key, reason=reason, details=details, timeout_seconds=timeout_seconds)

    def said(self) -> list[str]:
        return [message for message, _ in self.notes]


@dataclass
class Worker:
    """The task, as a worker process builds it once, and one run's control."""

    task: AdkWorkflowsTask
    sessions: DatabaseSessionService
    clock: Clock
    sent: list[httpx.Request]
    control: Control = field(default_factory=Control)

    async def run(self, run: RunPayload) -> JobResult:
        """One attempt of the run's job (to its end, or a wait, which raises)."""
        with controlling(self.control):
            return await self.task.jobs["run"].run(run)

    async def session_ids(self) -> list[str]:
        listed = await self.sessions.list_sessions(app_name=APP_NAME, user_id=MEMBER)
        return sorted(session.id for session in listed.sessions)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


def worker(
    sessions: DatabaseSessionService,
    *,
    model: Any = None,
    answer: Callable[[httpx.Request], httpx.Response] | None = None,
    **settings: Any,
) -> Worker:
    """The task, with HTTP answered by ``answer`` (200 ``{"ok": true}`` by
    default), recording each request."""
    clock = Clock()
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request) if answer else httpx.Response(200, json={"ok": True})

    options = AdkWorkflowsSettings(http_allowed_hosts=["api.example.com"], **settings)
    services = RunServices.of(
        options,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        clock=clock,
        sleep=clock.sleep,
        model=model or NoModels(),
    )
    task = AdkWorkflowsTask(options, services, sessions)
    return Worker(task, sessions, clock, sent)


def payload(document: dict[str, Any], value: Any = None, **more: Any) -> RunPayload:
    return RunPayload(
        tenant_id=ORGANIZATION,
        agent_id=document["id"],
        revision=3,
        name="Test agent",
        document=document,
        input=value,
        session_id=SESSION,
        run_as=MEMBER,
        run_as_name="Ada",
        trigger={"kind": "manual"},
        **more,
    )


def line(*steps: dict[str, Any]) -> dict[str, Any]:
    """An agent whose steps run one after another from its start."""
    ids = ["start", *(step["id"] for step in steps)]
    return agent([START_NODE, *steps], [(a, _way(a, steps), b) for a, b in zip(ids, ids[1:], strict=False)])


def _way(step_id: str, steps: tuple[dict[str, Any], ...]) -> str:
    kind = next((step["kind"] for step in steps if step["id"] == step_id), "start")
    return "success" if kind == "http" else "next"


ISSUE = {"issue": {"key": "XMEN-12"}}


# ------------------------------------------------------------------ straight runs


async def test_a_straight_run_succeeds_with_its_end_s_result(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = line(
        transform("shape", '{"hello": input.name}'),
        end("done", '{"said": steps.shape.output.hello}'),
    )

    result = await w.run(payload(document, {"name": "Ada"}))

    assert result.status is JobStatus.OK
    assert result.detail["outcome"] == "succeeded"
    assert result.detail["result"] == {"said": "Ada"}
    assert result.detail["session_id"] == SESSION
    assert result.detail["steps"] == 2
    assert w.control.said() == ["Shape (shape) finished", "Done (done) finished", "The run succeeded"]
    assert w.control.checkpoints >= 3
    # Its session: the adk_workflows app's, the member's, the admin API's ID.
    assert await w.session_ids() == [SESSION]


async def test_a_finished_run_run_again_gives_its_result_without_running_again(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    document = line(node("call", "http", {"method": "POST", "url": "https://api.example.com/x"}), end("done"))

    first = await w.run(payload(document, {}))
    again = await w.run(payload(document, {}))

    assert first.status is again.status is JobStatus.OK
    assert again.detail["result"] == first.detail["result"]
    assert len(w.sent) == 1


async def test_input_that_doesnt_fit_the_start_fails_the_run(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    [start, *rest] = approval_agent()["nodes"]
    document = line(end("done"))
    document["nodes"][0] = start

    result = await w.run(payload(document, {"issue": "not an object"}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("The run's input doesn't fit its start: issue")
    assert result.detail["outcome"] == "failed"


async def test_a_failing_step_fails_the_run_naming_it(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = line(transform("bad", "$error('boom')"), end("done"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None and result.error.startswith("Bad (bad) failed: ")
    assert "boom" in result.error
    assert result.detail["step"] == "bad"
    assert w.control.said()[-1].startswith("The run failed: Bad (bad) failed")


async def test_an_end_that_fails_the_run_fails_it_with_its_result(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = line(end("stop", '{"why": "no"}', outcome="failed"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.detail["result"] == {"why": "no"}
    assert result.detail["step"] == "stop"


async def test_a_document_that_cant_be_built_fails_without_a_session(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = agent([START_NODE, node("start2", "start", {"input_schema": {}})], [])

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED  # final: the queue doesn't retry it
    assert result.error == "The ADK workflow can't run: The agent has 2 starts; it needs one."
    assert await w.session_ids() == []


# ------------------------------------------------------------------ approvals


async def test_an_approval_pauses_the_run_and_its_decision_carries_it_on(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.key == REVIEW
    [gate] = w.control.gates
    assert gate == {
        "key": REVIEW,
        "reason": "Ship XMEN-12?",
        "details": {
            "kind": "approval",
            "approvers": "org:admin",
            "expires_at": "2026-09-27T12:00:00+00:00",
            "step": "review",
            "step_name": "Review",
            "workflow_name": "Test agent",
            "agent_id": "ag_test000001",
            "message": "Ship XMEN-12?",
            "session_id": SESSION,
            "interrupt_id": REVIEW,
        },
        "timeout_seconds": 24 * 3600 + 1,
    }
    assert w.control.said() == ["Move (move) finished"]

    # Still undecided: asked again, it waits again.
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    w.clock.advance(hours=3)
    w.control.decide(REVIEW, Decision(approved=True, comment="Looks right", actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {
        "approved": True,
        "decided_by": "Lead",
        "comment": "Looks right",
        "decided_at": "2026-09-26T15:00:00+00:00",
    }
    # The request went out once: finished steps don't run again.
    assert [str(request.url) for request in w.sent] == ["https://api.example.com/issues/XMEN-12/moves"]
    assert w.control.said()[1:] == [
        "Review (review): approved by Lead",
        "Review (review) finished",
        "Yes (yes) finished",
        "The run succeeded",
    ]


async def test_a_rejection_takes_the_rejected_way(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    w.control.decide(REVIEW, Decision(approved=False, comment="Not yet", actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.detail["result"] == {
        "approved": False,
        "decided_by": "Lead",
        "comment": "Not yet",
        "decided_at": "2026-09-26T12:00:00+00:00",
    }
    assert "No (no) finished" in w.control.said()
    assert len(w.sent) == 1


async def test_an_approval_nobody_decided_in_time_is_rejected_by_forge(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    # The task framework's timeout: a rejection by no one, at the deadline.
    w.clock.advance(hours=24, seconds=1)
    w.control.decide(REVIEW, Decision(approved=False, comment="Nobody decided within 24 hour(s)", actor_id="etf"))
    result = await w.run(run)

    assert result.detail["result"] == {
        "approved": False,
        "decided_by": "Forge",
        "comment": "No decision within 24 hours",
        "decided_at": "2026-09-27T12:00:01+00:00",
    }
    assert "Review (review): no decision in time" in w.control.said()


async def test_an_approval_decided_after_its_deadline_is_the_timer_s(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    w.clock.advance(hours=25)
    w.control.decide(REVIEW, Decision(approved=True, actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.detail["result"]["approved"] is False
    assert result.detail["result"]["decided_by"] == "Forge"


async def test_an_approval_without_a_time_limit_has_no_timeout(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    with pytest.raises(AwaitingDecision):
        await w.run(payload(approval_agent(timeout_hours=0), ISSUE))

    [gate] = w.control.gates
    assert gate["timeout_seconds"] is None
    assert gate["details"]["expires_at"] is None


async def test_two_approvals_are_asked_one_after_the_other(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = line(
        node("first", "approval", {"message": "One?", "approvers": "org:member", "timeout_hours": 0}),
        node("second", "approval", {"message": "Two?", "approvers": "org:admin", "timeout_hours": 0}),
        end("done", "steps"),
    )
    document["edges"] = [
        {"id": "a", "source": "start", "source_output": "next", "target": "first"},
        {"id": "b", "source": "first", "source_output": "approved", "target": "second"},
        {"id": "c", "source": "second", "source_output": "approved", "target": "done"},
    ]
    run = payload(document, {})
    first, second = "first@test_agent@1/first@1", "second@test_agent@1/second@1"

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.key == first
    w.control.decide(first, Decision(approved=True, actor_id="u-1", actor_name="Member"))
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.key == second
    assert [gate["details"]["approvers"] for gate in w.control.gates] == ["org:member", "org:member", "org:admin"]
    w.control.decide(second, Decision(approved=True, actor_id="u-2", actor_name="Admin"))

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["first"]["output"]["decided_by"] == "Member"
    assert result.detail["result"]["second"]["output"]["decided_by"] == "Admin"


async def test_a_loop_whose_body_pauses_asks_for_each_item_in_turn(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = loop_agent(
        "input.labels",
        [
            transform("shape", '{"label": label}'),
            node("ask", "approval", {"message": "Ship {{ label }}?", "timeout_hours": 0}),
        ],
    )
    run = payload(document, {"labels": ["a", "b"]})
    asked: list[str] = []
    while True:
        try:
            result = await w.run(run)
            break
        except AwaitingDecision as waiting:
            asked.append(waiting.reason)
            w.control.decide(waiting.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))

    assert asked == ["Ship a?", "Ship b?"]
    assert [item["decided_by"] for item in result.detail["result"]["results"]] == ["Lead", "Lead"]
    said = w.control.said()
    assert said.index("Shape (shape), item 1 of Each finished") < said.index(
        "Ask (ask), item 1 of Each: approved by Lead"
    )
    assert "Shape (shape), item 2 of Each finished" in said
    assert said[-3:] == ["Each (each) finished", "Done (done) finished", "The run succeeded"]


CONFIRM = agent(
    [
        START_NODE,
        node("confirm", "approval", {"message": "Sure?", "approvers": "org:admin", "timeout_hours": 0}),
        end("done", "steps.confirm.output"),
    ],
    [("start", "next", "confirm"), ("confirm", "approved", "done")],
    agent_id="ag_inner00002",
    name="Confirm",
)
NESTING = line(
    node("nested", "saved", {"agent": "ag_inner00002"}, name="Ask inside"),
    end("done", "steps.nested.output"),
)


async def test_a_saved_workflow_runs_from_the_runs_snapshot_and_its_pauses_name_its_steps(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(NESTING, {}, saved={"ag_inner00002": CONFIRM})

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [gate] = w.control.gates
    assert gate["details"]["step"] == "confirm"
    assert gate["details"]["step_name"] == "Confirm"
    assert gate["details"]["approvers"] == "org:admin"
    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-2", actor_name="Admin"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["decided_by"] == "Admin"
    # Its own steps aren't the run's; the node that runs it is.
    assert "Ask inside (nested) finished" in w.control.said()
    assert "Confirm (confirm): approved by Admin" in w.control.said()
    assert "Confirm (confirm) finished" not in w.control.said()
    assert w.control.said().count("Done (done) finished") == 1


async def test_a_saved_workflow_the_run_has_no_snapshot_of_fails_it(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)

    result = await w.run(payload(NESTING, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "The ADK workflow can't run: Node 'Ask inside': the organization has no agent ag_inner00002."
    )


# ------------------------------------------------------------------ human input

HOW_MANY = {
    "type": "object",
    "required": ["count"],
    "properties": {"count": {"type": "integer"}, "note": {"type": "string"}},
}


def asking() -> dict[str, Any]:
    return line(
        node("ask", "human_input", {"message": "How many for {{ input.who }}?", "response_schema": HOW_MANY}),
        end("done", '{"answer": steps.ask.output}'),
    )


async def test_human_input_is_asked_and_its_json_answer_carries_the_run_on(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(asking(), {"who": "Ada"})
    key = "ask@test_agent@1/ask@1"

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.key == key
    [gate] = w.control.gates
    assert gate == {
        "key": key,
        "reason": "How many for Ada?",
        "details": {
            "kind": "human_input",
            "response_schema": HOW_MANY,
            "step": "ask",
            "step_name": "Ask",
            "workflow_name": "Test agent",
            "agent_id": "ag_test000001",
            "message": "How many for Ada?",
            "session_id": SESSION,
            "interrupt_id": key,
        },
        "timeout_seconds": None,
    }

    w.control.decide(key, Decision(approved=True, comment=json.dumps({"count": "3"}), actor_id="u-1", actor_name="Ada"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    # What the person gave is coerced to the step's schema.
    assert result.detail["result"] == {"answer": {"count": 3}}
    assert "Ask (ask): answered by Ada" in w.control.said()


@pytest.mark.parametrize(
    ("decision", "error"),
    [
        (
            Decision(approved=False, comment="Not mine", actor_id="u-1", actor_name="Ada"),
            "Ada declined to answer: Not mine",
        ),
        (Decision(approved=True, comment="three", actor_id="u-1"), "its answer isn't JSON"),
        (Decision(approved=True, comment="[3]", actor_id="u-1"), "its answer isn't a JSON object"),
    ],
)
async def test_human_input_declined_or_not_answered_in_json_fails_the_run(
    sessions: DatabaseSessionService, decision: Decision, error: str
) -> None:
    w = worker(sessions)
    run = payload(asking(), {"who": "Ada"})
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    w.control.decide("ask@test_agent@1/ask@1", decision)
    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == f"Ask (ask) failed: {error}"
    assert result.detail["step"] == "ask"


# ------------------------------------------------------------------ delays


def waiting(amount: float, unit: str) -> dict[str, Any]:
    return line(node("wait", "delay", {"amount": amount, "unit": unit}), end("done", "steps.wait.output"))


async def test_a_short_delay_waits_in_place(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)

    result = await w.run(payload(waiting(10, "seconds"), {}))

    assert w.clock.slept == [10]
    assert result.detail["result"] == {"resumed_at": "2026-09-26T12:00:10+00:00"}


async def test_a_long_delay_lets_the_worker_go_until_its_time(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(waiting(2, "hours"), {})
    due = datetime(2026, 9, 26, 14, tzinfo=UTC)

    with pytest.raises(WaitingUntil) as waits:
        await w.run(run)
    assert waits.value.when == due
    # Woken early (a resume, a retry): it waits again, for the same time.
    w.clock.advance(hours=1)
    with pytest.raises(WaitingUntil) as waits:
        await w.run(run)
    assert waits.value.when == due

    w.clock.advance(hours=1)
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"resumed_at": "2026-09-26T14:00:00+00:00"}
    assert w.clock.slept == []
    assert "Wait (wait) waits until 2026-09-26T14:00:00+00:00" in w.control.said()


async def test_a_delay_woken_a_little_early_sleeps_the_rest(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(waiting(2, "hours"), {})
    with pytest.raises(WaitingUntil):
        await w.run(run)

    w.clock.advance(hours=1, minutes=59, seconds=55)
    result = await w.run(run)

    assert w.clock.slept == [5.0]
    assert result.detail["result"] == {"resumed_at": "2026-09-26T14:00:00+00:00"}


# ------------------------------------------------------------------ interruptions


def calling_then_asking() -> dict[str, Any]:
    return line(
        node("call", "http", {"method": "POST", "url": "https://api.example.com/x", "body": '{"a": 1}'}),
        node("triage", "llm", llm_config(instruction="Say hi")),
        end("done", '{"said": steps.triage.output}'),
    )


def overloaded() -> errors.ServerError:
    return errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})


async def test_a_model_hiccup_is_retried_and_the_run_carries_on_where_it_stopped(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Hi!")]])
    w = worker(sessions, model=llm)
    run = payload(calling_then_asking(), {})

    with pytest.raises(TransientError, match="ServerError"):
        await w.run(run)
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"said": "Hi!"}
    # In the same session, and the request before it didn't go out again.
    assert result.detail["session_id"] == SESSION
    assert await w.session_ids() == [SESSION]
    assert len(w.sent) == 1
    assert "The run carries on where it stopped" in w.control.said()


async def test_a_hiccup_after_a_decision_carries_on_with_that_decision(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Shipped")]])
    w = worker(sessions, model=llm)
    document = line(
        node("review", "approval", {"message": "Ship?", "timeout_hours": 0}),
        node("tell", "llm", llm_config(instruction="Say it shipped")),
        end("done", '{"review": steps.review.output.decided_by, "said": steps.tell.output}'),
    )
    document["edges"][1]["source_output"] = "approved"
    run = payload(document, {})
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))

    with pytest.raises(TransientError):
        await w.run(run)
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"review": "Lead", "said": "Shipped"}
    # Asked, then its decision read; carrying on after the hiccup reads it no more.
    assert w.control.asked == [waiting.value.key]
    assert len(w.control.gates) == 2
    assert w.control.said().count("Review (review) finished") == 1


async def test_an_interrupted_run_adk_cant_carry_on_starts_over_in_a_new_session(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Hi!")]])
    w = worker(sessions, model=llm)
    run = payload(calling_then_asking(), {})
    with pytest.raises(TransientError):
        await w.run(run)

    carry_on = Runner.run_async

    def refusing(self: Runner, **options: Any) -> Any:
        if options.get("invocation_id") and options.get("new_message") is None:

            async def refuse() -> AsyncGenerator[Any]:
                raise ValueError("that invocation can't be resumed")
                yield

            return refuse()
        return carry_on(self, **options)

    monkeypatch.setattr(Runner, "run_async", refusing)
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"said": "Hi!"}
    started_over = f"{SESSION}-r1"
    assert result.detail["session_id"] == started_over
    assert await w.session_ids() == [SESSION, started_over]
    # Starting over repeats what the run did before.
    assert len(w.sent) == 2
    assert w.control.state["adk_workflow"]["session_id"] == started_over
    [(said, attributes)] = [(m, a) for m, a in w.control.notes if "starts over" in m]
    assert said.startswith("The run can't carry on where it stopped (ValueError: that invocation can't be resumed)")
    assert attributes == {"session_id": started_over}


async def test_a_failed_run_restarted_carries_on_from_the_step_that_failed(sessions: DatabaseSessionService) -> None:
    down = [True]

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if down[0] and request.url.path == "/b" else 200, json={"ok": True})

    w = worker(sessions, answer=flaky)
    run = payload(
        line(
            node("a", "http", {"method": "POST", "url": "https://api.example.com/a"}),
            node("b", "http", {"method": "POST", "url": "https://api.example.com/b"}),
            end("done", "steps.b.output.status"),
        ),
        {},
    )

    failed = await w.run(run)
    assert failed.status is JobStatus.FAILED
    assert failed.error == "B (b) failed: It answered 404"

    # The task restarted (the same task: its control, its session).
    down[0] = False
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == 200
    assert [request.url.path for request in w.sent] == ["/a", "/b", "/b"]
    assert await w.session_ids() == [SESSION]


async def test_a_resubmitted_run_runs_afresh_as_another_invocation_of_its_session(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(line(node("call", "http", {"method": "POST", "url": "https://api.example.com/x"}), end("done")), {})
    first = await w.run(run)

    # Resubmitted: a new task (another control) with the same payload.
    w.control = Control(instance="task-2")
    again = await w.run(run)

    assert first.status is again.status is JobStatus.OK
    assert again.detail["session_id"] == SESSION
    assert len(w.sent) == 2
    session = await sessions.get_session(app_name=APP_NAME, user_id=MEMBER, session_id=SESSION)
    assert session is not None
    assert len({event.invocation_id for event in session.events}) == 2
    # The session's latest invocation is the latest task's.
    assert session.events[-1].invocation_id != session.events[0].invocation_id


async def test_a_run_resubmitted_while_its_last_task_waited_asks_its_own_questions(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)  # never decided: abandoned

    w.control = Control(instance="task-2")
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["approved"] is True
    assert len(w.control.gates) == 2
    assert len(w.sent) == 2


async def test_a_database_hiccup_is_retried(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)

    async def down(**options: Any) -> None:
        raise ConnectionRefusedError("MySQL isn't there")

    w.task.sessions.get_session = down  # type: ignore[method-assign,union-attr]
    with pytest.raises(TransientError, match="ConnectionRefusedError"):
        await w.run(payload(line(end("done")), {}))


# ------------------------------------------------------------------ models


PROVIDERS = ModelProviderConfig.model_validate(
    {
        "version": 1,
        "default": {"provider": "google", "model": "gemini-test"},
        "providers": {
            "google": {
                "name": "Google Gemini",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
                "api": "google-generative-ai",
                "auth": {"type": "apiKey", "key": "key"},
                "models": [
                    {"id": "gemini-test", "name": "Gemini Test", "reasoning": True, "input": ["text"]},
                    {"id": "gemini-deep", "name": "Gemini Deep", "reasoning": True, "input": ["text"]},
                ],
            }
        },
    }
)


class FakeGemini:
    """The model ProviderModels builds for each call: it answers "Hi", and
    records the call (the model and thinking level) and the request."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []
        self.requests: list[LlmRequest] = []

    def build(self, call: ModelCall) -> Any:
        self.calls.append(call)
        return self

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[Any]:
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Hi")]))


async def test_an_llm_node_runs_on_the_model_and_thinking_level_it_picks(sessions: DatabaseSessionService) -> None:
    gemini = FakeGemini()
    models = ProviderModels(PROVIDERS, build=gemini.build)
    w = worker(sessions, model=models, model_timeout=30)
    document = line(
        node("deep", "llm", llm_config(model={"provider": "google", "name": "gemini-deep"}, thinking_level="high")),
        node("plain", "llm", llm_config()),
        end("done", '{"deep": steps.deep.output, "plain": steps.plain.output}'),
    )

    result = await w.run(payload(document, {}))

    assert result.detail["result"] == {"deep": "Hi", "plain": "Hi"}
    deep, plain = gemini.calls
    assert (deep.model.ref, deep.thinking_level) == ("google/gemini-deep", "high")
    assert (plain.model.ref, plain.thinking_level) == ("google/gemini-test", None)
    asked_deep, asked_plain = gemini.requests
    assert asked_deep.model == "gemini-deep"
    assert asked_deep.config.thinking_config == types.ThinkingConfig(
        include_thoughts=True, thinking_level=types.ThinkingLevel.HIGH
    )
    assert asked_plain.model == "gemini-test"
    assert asked_plain.config.thinking_config == types.ThinkingConfig(include_thoughts=True)
    # Each call has the task's model timeout, in milliseconds.
    assert asked_deep.config.http_options is not None
    assert asked_deep.config.http_options.timeout == 30_000


async def test_an_llm_node_on_a_worker_without_models_fails_the_run_saying_so(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    document = line(node("triage", "llm", llm_config()), end("done"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == NOT_SET_UP


# ------------------------------------------------------------------ the factory


def test_the_task_registers_with_the_worker() -> None:
    registry = TaskRegistry()
    registry.load_entry_points()
    factory = registry.factory("adk_workflows")
    assert isinstance(factory, AdkWorkflowsTaskFactory)
    assert (factory.name, factory.queue, factory.schedules) == ("adk_workflows", "adk_workflows", [])
    assert registry.queues(["adk_workflows"]) == {"adk_workflows": "adk_workflows"}


def test_its_settings_are_read_from_hybrid_adk_workflows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv(
        "HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL", "mysql+aiomysql://forge:secret@db:3306/forge_admin"
    )
    monkeypatch.setenv("HYBRID_ADK_WORKFLOWS__DEFAULT_MODEL", "gpt-5.2")
    monkeypatch.setenv("HYBRID_ADK_WORKFLOWS__HTTP_ALLOWED_HOSTS", '["intranet"]')
    monkeypatch.setenv("HYBRID_ADK_WORKFLOWS__INLINE_DELAY_SECONDS", "5")
    monkeypatch.setenv("HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY", "")
    monkeypatch.setenv("HYBRID_OTHER__INLINE_DELAY_SECONDS", "99")  # another section's

    settings = load_section("adk_workflows", AdkWorkflowsSettings)

    assert settings.session_database_url is not None
    assert settings.session_database_url.get_secret_value() == "mysql+aiomysql://forge:secret@db:3306/forge_admin"
    assert settings.default_model == "gpt-5.2"
    assert settings.http_allowed_hosts == ["intranet"]
    assert settings.inline_delay_seconds == 5
    assert settings.google_api_key is None
    made = RunServices.of(settings)
    assert (made.allowed_hosts, made.inline_delay_seconds) == (("intranet",), 5)


async def test_a_worker_without_a_session_database_fails_runs_saying_so(tmp_path: Path) -> None:
    task = AdkWorkflowsTaskFactory().build(TaskContext(settings=None, options=AdkWorkflowsSettings()))  # type: ignore[arg-type]
    try:
        assert task.sessions is None
        assert isinstance(task.services.model, NoModels)
        result = await task.jobs["run"].run(payload(line(end("done")), {}))
    finally:
        await task.close()

    assert result.status is JobStatus.FAILED
    assert result.error == NO_SESSIONS


async def test_the_factory_keeps_one_session_service_per_process(tmp_path: Path) -> None:
    options = AdkWorkflowsSettings(session_database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    ctx = TaskContext(settings=None, options=options)  # type: ignore[arg-type]
    first = AdkWorkflowsTaskFactory().build(ctx)
    second = AdkWorkflowsTaskFactory().build(ctx)
    try:
        assert isinstance(first.sessions, DatabaseSessionService)
        assert first.sessions is second.sessions
        await first.ensure_schema()
    finally:
        await second.services.http.aclose()  # type: ignore[union-attr]
        await first.close()


async def test_the_factory_takes_its_services_and_sessions_from_the_context(
    sessions: DatabaseSessionService,
) -> None:
    services = RunServices(model=NoModels())
    task = AdkWorkflowsTaskFactory().build(
        TaskContext(settings=None, extras={SERVICES: services, SESSIONS: sessions})  # type: ignore[arg-type]
    )
    assert task.services is services
    assert task.sessions is sessions
