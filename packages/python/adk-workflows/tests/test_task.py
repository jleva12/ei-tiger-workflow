"""
The adk_workflows task's run job, as the worker runs it: re-entrant, carried
on from its ADK session (ADK's DatabaseSessionService, on SQLite here) each
time the worker runs it again, with its control kept in memory
(LocalJobControl), a fake HTTP transport, a clock the tests move and scripted
models. The worker's own tests run it on the run store.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from forge_agent_runtime import http_client
from forge_codegraph_query import CodeGraph
from forge_common.adk.models import ModelCall, ProviderModels
from forge_common.adk.usage import UsageCall, recording
from forge_common.model_provider import Cost, ModelProviderConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import errors, types

from forge_task_adk_workflows import runs
from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunServices
from forge_task_adk_workflows.graph.agent_tools import AgentServices
from forge_task_adk_workflows.models import NOT_SET_UP, NoModels
from forge_task_adk_workflows.runs import APP_NAME, MAX_NOTES, STATE_KEY, AdkRun, RunPayload
from forge_task_adk_workflows.steps import run_steps
from forge_task_adk_workflows.task import (
    NO_SESSIONS,
    SERVICES,
    SESSION_ISOLATION,
    SESSIONS,
    AdkWorkflowsTask,
    AdkWorkflowsTaskFactory,
    build_sessions,
)
from forge_tasks.control import (
    AwaitingDecision,
    Decision,
    Gate,
    LocalJobControl,
    WaitingUntil,
    controlling,
)
from forge_tasks.errors import TransientError
from forge_tasks.settings import load_section
from forge_tasks.tasks import JobResult, JobStatus, TaskContext, TaskRegistry

from .scripted_llm import ScriptedLlm, blocked_for_safety
from .test_graph import (
    START_NODE,
    Clock,
    agent,
    approval_agent,
    end,
    llm_config,
    loop_agent,
    node,
    sub_config,
    transform,
)

ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"
MEMBER = "member-1"
SESSION = "5b1e0c4e-0000-4000-8000-00000000c0de"
REVIEW = "review@test_agent@1/review@1"


# ---------------------------------------------------------------------- helpers


@dataclass
class Control(LocalJobControl):
    """LocalJobControl, recording what each gate was asked with, and whose
    decisions the run read."""

    gates: list[dict[str, Any]] = field(default_factory=list)
    read: list[str] = field(default_factory=list)

    def decided(self, key: str) -> Decision | None:
        found = super().decided(key)
        if found is not None:
            self.read.append(key)
        return found

    async def wait_for(self, gates: Sequence[Gate], *, until: datetime | None = None, reason: str = "") -> NoReturn:
        self.gates.extend(
            {"key": gate.key, "reason": gate.reason, "details": gate.details, "timeout_seconds": gate.timeout_seconds}
            for gate in gates
        )
        await super().wait_for(gates, until=until, reason=reason)

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

    async def steps(self, run: RunPayload) -> list[dict[str, Any]]:
        """The run's steps, as its run page shows them."""
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=run.run_as, session_id=run.session_id)
        return run_steps(session, run.document)

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
    artifacts: Any = None,
    **settings: Any,
) -> Worker:
    """The task, with HTTP answered by ``answer`` (200 ``{"ok": true}`` by
    default) through the worker's kind of client, recording each request, and
    runs' artifacts in ``artifacts`` (none by default)."""
    clock = Clock()
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request) if answer else httpx.Response(200, json={"ok": True})

    options = AdkWorkflowsSettings(http_allowed_hosts=["api.example.com"], **settings)
    services = RunServices.of(
        options,
        http=http_client(transport=httpx.MockTransport(handle)),
        clock=clock,
        sleep=clock.sleep,
        model=model or NoModels(),
    )
    task = AdkWorkflowsTask(options, services, sessions, artifacts=artifacts)
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


def starting_over(document: dict[str, Any]) -> dict[str, Any]:
    """The document, its start set to start a run over in a new session when
    ADK can't carry it on (``start_over``)."""
    document["nodes"] = [
        {**each, "config": {**each["config"], "start_over": True}} if each["kind"] == "start" else each
        for each in document["nodes"]
    ]
    return document


def refusing_to_carry_on(carry_on: Callable[..., Any]) -> Callable[..., Any]:
    """``Runner.run_async``, but ADK refuses to carry an invocation on."""

    def run_async(self: Runner, **options: Any) -> Any:
        if options.get("invocation_id") and options.get("new_message") is None:

            async def refuse() -> AsyncGenerator[Any]:
                raise ValueError("that invocation can't be resumed")
                yield

            return refuse()
        return carry_on(self, **options)

    return run_async


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


async def test_a_way_that_ended_waits_for_one_still_waiting_on_a_person(sessions: DatabaseSessionService) -> None:
    # Two ways at once: one ends straight away, the other waits for an
    # approval. The run ends once both have, with both endings.
    w = worker(sessions)
    document = agent(
        [
            START_NODE,
            node(
                "route",
                "match",
                {
                    "mode": "all",
                    "arms": [
                        {"id": "log_it", "label": "", "condition": "true"},
                        {"id": "ask", "label": "", "condition": "true"},
                    ],
                },
            ),
            end("logged", '"logged"'),
            node("review", "approval", {"message": "Ship?", "approvers": "org:admin", "timeout_hours": 24}),
            end("shipped", "steps.review.output.approved"),
        ],
        [
            ("start", "next", "route"),
            ("route", "log_it", "logged"),
            ("route", "ask", "review"),
            ("review", "approved", "shipped"),
        ],
    )
    run = payload(document, {})

    with pytest.raises(AwaitingDecision):
        await w.run(run)

    w.control.decide(REVIEW, Decision(approved=True, comment="go", actor_id="u-7", actor_name="Lead"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == [
        {"step": "logged", "name": "Logged", "outcome": "succeeded", "result": "logged"},
        {"step": "shipped", "name": "Shipped", "outcome": "succeeded", "result": True},
    ]


async def test_an_end_that_fails_the_run_fails_it_with_its_result(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = line(end("stop", '{"why": "no"}', outcome="failed"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == 'Stop (stop) failed the run: {"why": "no"}'
    assert result.detail["result"] == {"why": "no"}
    assert result.detail["step"] == "stop"
    assert w.control.said()[-1] == 'The run failed: Stop (stop) failed the run: {"why": "no"}'


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

    # The worker's timeout: a rejection by no one, at the deadline.
    w.clock.advance(hours=24, seconds=1)
    w.control.decide(REVIEW, Decision(approved=False, comment="Nobody decided by its deadline"))
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


async def test_an_approval_decided_in_time_stands_though_its_run_reads_it_after_the_deadline(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    # Decided 6 hours in; its job waited in the queue past the 24-hour deadline.
    in_time = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    w.clock.advance(hours=25)
    w.control.decide(REVIEW, Decision(approved=True, actor_id="u-7", actor_name="Lead", decided_at=in_time))
    result = await w.run(run)

    assert result.detail["result"] == {
        "approved": True,
        "decided_by": "Lead",
        "comment": "",
        "decided_at": "2026-09-26T18:00:00+00:00",  # when it was decided, not read
    }
    assert "Review (review): approved by Lead" in w.control.said()
    assert "Review (review): no decision in time" not in w.control.said()


async def test_an_approval_decided_once_its_deadline_passed_is_the_timer_s_whenever_its_run_reads_it(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision):
        await w.run(run)

    late = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)  # the deadline itself
    w.control.decide(REVIEW, Decision(approved=True, actor_id="u-7", actor_name="Lead", decided_at=late))
    result = await w.run(run)  # read at once, its clock still before the deadline

    assert (result.detail["result"]["approved"], result.detail["result"]["decided_by"]) == (False, "Forge")
    assert "Review (review): no decision in time" in w.control.said()


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
    assert [gate["details"]["approvers"] for gate in w.control.gates] == ["org:member", "org:admin"]
    assert w.control.read == [first]
    w.control.decide(second, Decision(approved=True, actor_id="u-2", actor_name="Admin"))

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["first"]["output"]["decided_by"] == "Member"
    assert result.detail["result"]["second"]["output"]["decided_by"] == "Admin"


async def test_approvals_on_ways_at_once_are_asked_together_and_decided_in_any_order(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    document = agent(
        [
            START_NODE,
            node("legal", "approval", {"message": "Legal?", "timeout_hours": 0}),
            node("money", "approval", {"message": "Budget?", "timeout_hours": 0}),
            transform("signed", '{"legal": previous.decided_by}'),
            transform("paid", '{"money": previous.decided_by}'),
        ],
        [
            ("start", "next", "legal"),
            ("start", "next", "money"),
            ("legal", "approved", "signed"),
            ("money", "approved", "paid"),
        ],
    )
    run = payload(document, {})
    legal, money = "legal@test_agent@1/legal@1", "money@test_agent@1/money@1"

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.keys == [legal, money]
    # The second decided first carries its way on; the first still waits.
    w.control.decide(money, Decision(approved=True, actor_id="u-2", actor_name="Fin"))
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.keys == [legal]
    assert [step["status"] for step in await w.steps(run) if step["id"] in ("signed", "paid")] == [
        "not_reached",
        "done",
    ]
    w.control.decide(legal, Decision(approved=True, actor_id="u-1", actor_name="Lex"))

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert [ended["result"] for ended in result.detail["result"]] == [{"money": "Fin"}, {"legal": "Lex"}]


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


async def test_a_saved_workflow_started_with_its_inputs_pauses_and_resumes(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    nesting = line(
        node("nested", "saved", {"agent": "ag_inner00002", "inputs": {"who": "input.who"}}, name="Ask inside"),
        end("done", "steps.nested.output"),
    )
    run = payload(nesting, {"who": "Ada"}, saved={"ag_inner00002": CONFIRM})

    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    [gate] = w.control.gates
    assert gate["details"]["step"] == "confirm"
    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-2", actor_name="Admin"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["decided_by"] == "Admin"
    assert "Ask inside (nested) finished" in w.control.said()
    assert "Confirm (confirm) finished" not in w.control.said()


ORDERS = agent(
    [
        START_NODE,
        node("fetch", "http", {"url": "https://api.example.com/orders/1"}, name="Fetch"),
        end("done", "previous"),
    ],
    [("start", "next", "fetch"), ("fetch", "success", "done")],
    agent_id="ag_inner00003",
    name="Orders",
)


@pytest.mark.parametrize("inputs", [{}, {"order": '"A1"'}], ids=["handed-on", "with-inputs"])
async def test_a_step_failing_in_a_saved_workflow_fails_the_run_at_the_node_that_runs_it(
    sessions: DatabaseSessionService, inputs: dict[str, str]
) -> None:
    w = worker(sessions, answer=lambda request: httpx.Response(404, json={"message": "No such order"}))
    nesting = line(
        node("nested", "saved", {"agent": "ag_inner00003", "inputs": inputs}, name="Ask inside"),
        end("done", "steps.nested.output"),
    )

    result = await w.run(payload(nesting, {}, saved={"ag_inner00003": ORDERS}))

    assert result.status is JobStatus.FAILED
    # The run's own step, not the workflow's (whose IDs aren't the run's), and where in it, and why.
    assert result.detail["step"] == "nested"
    assert result.error == (
        "Ask inside (nested) failed in its workflow Orders (ag_inner00003): Fetch (fetch) failed: "
        "GET https://api.example.com/orders/1 answered 404 Not Found: No such order"
    )


async def test_an_error_a_step_raises_fails_the_run_naming_the_step_and_its_status(
    sessions: DatabaseSessionService,
) -> None:
    def missing() -> errors.ClientError:
        return errors.ClientError(404, {"error": {"message": "models/gemini-9 is not found"}})

    w = worker(sessions, model=ScriptedLlm(turns=[missing()]))
    document = line(node("triage", "llm", llm_config()), end("done"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.detail["step"] == "triage"
    assert (
        result.error == "The run failed: Triage (triage) failed: ClientError (HTTP 404): models/gemini-9 is not found"
    )


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


async def test_a_run_whose_session_lost_the_race_for_its_members_state_row_tries_again(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    # Another run of the member's made the row first, where this one can't see it yet (task.SESSION_ISOLATION).
    duplicate = IntegrityError(
        "INSERT INTO user_states ...", {}, Exception("Duplicate entry for key 'user_states.PRIMARY'")
    )
    create, raced = sessions.create_session, []

    async def racing(**options: Any) -> Any:
        if not raced:
            raced.append(options["user_id"])
            raise duplicate
        return await create(**options)

    monkeypatch.setattr(sessions, "create_session", racing)
    w = worker(sessions)
    run = payload(line(transform("shape", "1"), end("done", "steps.shape.output")), {})

    with pytest.raises(TransientError, match="IntegrityError"):
        await w.run(run)
    result = await w.run(run)

    assert result.status is JobStatus.OK and raced == [MEMBER]
    assert await w.session_ids() == [SESSION]
    # Anywhere but opening the session, an IntegrityError is no hiccup.
    assert not isinstance(runs._classified(duplicate), TransientError)


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
    assert (len(w.control.gates), w.control.read) == (1, [waiting.value.key])
    assert w.control.said().count("Review (review) finished") == 1


async def test_an_interrupted_run_adk_cant_carry_on_fails_to_be_resubmitted(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Hi!")]])
    w = worker(sessions, model=llm)
    run = payload(calling_then_asking(), {})
    with pytest.raises(TransientError):
        await w.run(run)

    monkeypatch.setattr(Runner, "run_async", refusing_to_carry_on(Runner.run_async))
    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "The run can't carry on where it stopped (ValueError: that invocation can't be resumed). "
        "Resubmit it to run it again from the start"
    )
    # Nothing ran again.
    assert await w.session_ids() == [SESSION]
    assert len(w.sent) == 1


async def test_an_interrupted_run_adk_cant_carry_on_starts_over_in_a_new_session_when_its_workflow_says_so(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Hi!")]])
    w = worker(sessions, model=llm)
    run = payload(starting_over(calling_then_asking()), {})
    with pytest.raises(TransientError):
        await w.run(run)

    monkeypatch.setattr(Runner, "run_async", refusing_to_carry_on(Runner.run_async))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"said": "Hi!"}
    started_over = f"{SESSION}-r1"
    assert result.detail["session_id"] == started_over
    assert await w.session_ids() == [SESSION, started_over]
    # Starting over repeats what the run did before.
    assert len(w.sent) == 2
    assert w.control.state["adk_workflow"]["session_id"] == started_over
    assert w.control.state["adk_workflow"]["earlier_sessions"] == [SESSION]
    [(said, attributes)] = [(m, a) for m, a in w.control.notes if "starts over" in m]
    assert said.startswith("The run can't carry on where it stopped (ValueError: that invocation can't be resumed)")
    assert attributes == {"session_id": started_over}


@pytest.mark.parametrize("hiccup_first", [True, False])
async def test_a_step_that_fails_once_the_run_carried_on_fails_it_without_starting_over(
    sessions: DatabaseSessionService, hiccup_first: bool
) -> None:
    # Its model is refused for good (permission denied): no new session, and
    # the request before it isn't sent again, even in a workflow that starts over.
    def denied() -> errors.ClientError:
        return errors.ClientError(403, {"error": {"message": "permission denied"}})

    llm = ScriptedLlm(turns=[overloaded() if hiccup_first else denied(), denied()])
    w = worker(sessions, model=llm)
    run = payload(starting_over(calling_then_asking()), {})
    if hiccup_first:
        with pytest.raises(TransientError):
            await w.run(run)
    else:
        assert (await w.run(run)).status is JobStatus.FAILED  # and someone retries it

    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    # Named by the step it happened in, and the HTTP status the model's API answered.
    assert result.error == "The run failed: Triage (triage) failed: ClientError (HTTP 403): permission denied"
    assert result.detail["step"] == "triage"
    assert await w.session_ids() == [SESSION]
    assert len(w.sent) == 1


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
    assert failed.error == 'B (b) failed: POST https://api.example.com/b answered 404 Not Found: {"ok": true}'

    # The task restarted (the same task: its control, its session).
    down[0] = False
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == 200
    assert [request.url.path for request in w.sent] == ["/a", "/b", "/b"]
    assert await w.session_ids() == [SESSION]


def racing(slow: dict[str, Any]) -> dict[str, Any]:
    """Two ways from the start: one ends at once, the other through ``slow``."""
    way = "success" if slow["kind"] == "http" else "next"
    return agent(
        [START_NODE, end("fast", '"fast result"'), slow, end("done", '"slow result"')],
        [("start", "next", "fast"), ("start", "next", slow["id"]), (slow["id"], way, "done")],
    )


#: A racing run's result once both its ways have ended.
BOTH_ENDED = [
    {"step": "fast", "name": "Fast", "outcome": "succeeded", "result": "fast result"},
    {"step": "done", "name": "Done", "outcome": "succeeded", "result": "slow result"},
]


async def test_a_way_with_a_hiccup_is_carried_on_after_another_way_ended(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="ok")]], delay=0.1)
    w = worker(sessions, model=llm)
    run = payload(racing(node("slow", "llm", llm_config())), {})
    with pytest.raises(TransientError):
        await w.run(run)

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == BOTH_ENDED
    assert len(llm.requests) == 2


async def test_a_failed_way_runs_again_when_its_run_is_retried_after_another_way_ended(
    sessions: DatabaseSessionService,
) -> None:
    down = [True]

    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)  # the fast way ends first
        return httpx.Response(404 if down[0] else 200, json={"ok": True})

    w = worker(sessions, answer=answer)
    run = payload(racing(node("slow", "http", {"method": "POST", "url": "https://api.example.com/b"})), {})
    failed = await w.run(run)
    assert failed.status is JobStatus.FAILED
    assert failed.error == 'Slow (slow) failed: POST https://api.example.com/b answered 404 Not Found: {"ok": true}'

    down[0] = False  # fixed, then the run is retried
    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == BOTH_ENDED
    assert len(w.sent) == 2


async def test_a_way_cut_off_by_a_stop_is_carried_on_after_another_way_ended(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[types.Part(text="ok")], [types.Part(text="ok")]], delay=0.5)
    w = worker(sessions, model=llm)
    run = payload(racing(node("slow", "llm", llm_config())), {})
    stopping = asyncio.create_task(w.run(run))
    while not llm.requests:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)  # the fast way has ended; the slow one waits on its model
    stopping.cancel()  # the worker stops
    with pytest.raises(asyncio.CancelledError):
        await stopping

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == BOTH_ENDED
    assert len(llm.requests) == 2


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


async def test_a_runs_steps_are_its_own_in_a_session_another_run_shares(sessions: DatabaseSessionService) -> None:
    values = iter(["first", "second"])
    w = worker(sessions, answer=lambda request: httpx.Response(200, json={"value": next(values)}))
    document = line(
        node("call", "http", {"method": "GET", "url": "https://api.example.com/state"}),
        end("done", "steps.call.output.body.value"),
    )
    run = payload(document, {})
    original, resubmitted = Control(instance="original"), Control(instance="resubmitted")
    for control in (original, resubmitted):  # resubmitted before resubmissions had sessions of their own
        w.control = control
        await w.run(run)
    session = await sessions.get_session(app_name=APP_NAME, user_id=MEMBER, session_id=SESSION)

    own = run_steps(session, document, invocation_id=original.state["adk_workflow"]["invocation_id"])
    assert own[-1]["output"] == {"outcome": "succeeded", "result": "first"}
    assert run_steps(session, document)[-1]["output"] == {"outcome": "succeeded", "result": "second"}  # the latest


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
    assert (len(w.control.gates), w.control.read) == (1, [waiting.value.key])
    assert len(w.sent) == 2


def deploy_approval() -> dict[str, Any]:
    return agent(
        [
            START_NODE,
            node("review", "approval", {"message": "Deploy?", "timeout_hours": 0}),
            end("yes", '"approved"'),
            end("no", '"rejected"'),
        ],
        [("start", "next", "review"), ("review", "approved", "yes"), ("review", "rejected", "no")],
    )


@pytest.mark.parametrize("older_approves", [True, False])
async def test_a_decision_never_reaches_another_run_of_the_session_waiting_at_the_same_step(
    sessions: DatabaseSessionService, older_approves: bool
) -> None:
    # Two resubmissions of one run wait at the same approval, and are decided
    # each its own way. ADK would hand the older one's answer to the newer.
    w = worker(sessions)
    run = payload(deploy_approval(), {})
    older, newer = Control(instance="task-1"), Control(instance="task-2")
    for control, approved in ((older, older_approves), (newer, not older_approves)):
        w.control = control
        with pytest.raises(AwaitingDecision) as waiting:
            await w.run(run)
        control.decide(waiting.value.key, Decision(approved=approved, actor_id="u-7", actor_name="Lead"))

    w.control = older
    failed = await w.run(run)

    assert failed.status is JobStatus.FAILED
    assert str(failed.error).startswith("Review (review): another run of this session waits at the same step")
    w.control = newer
    result = await w.run(run)
    assert result.status is JobStatus.OK
    assert result.detail["result"] == ("rejected" if older_approves else "approved")  # its own decision


async def test_an_answer_that_doesnt_reach_the_run_fails_it_instead_of_being_sent_again(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = worker(sessions)
    run = payload(approval_agent(), ISSUE)
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    w.control.decide(waiting.value.key, Decision(approved=True, actor_id="u-7", actor_name="Lead"))

    async def lost(self: AdkRun, message: Any) -> None:
        return None  # the answer goes nowhere

    monkeypatch.setattr(AdkRun, "_drive", lost)
    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert "its answer didn't reach the run, so the run stopped instead of answering again" in str(result.error)


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


class MeteredGemini(FakeGemini):
    """FakeGemini, saying what each answer used."""

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[Any]:
        async for reply in super().generate_content_async(llm_request, stream):
            reply.usage_metadata = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1_000, candidates_token_count=200, total_token_count=1_200
            )
            yield reply


async def test_a_runs_model_calls_are_recorded_for_its_workflow_at_its_models_price(
    sessions: DatabaseSessionService,
) -> None:
    priced = PROVIDERS.model_copy(deep=True)
    priced.providers["google"].models[0] = (
        priced.providers["google"]
        .models[0]
        .model_copy(update={"cost": Cost.model_validate({"input": 2, "output": 10})})
    )
    gemini = MeteredGemini()
    w = worker(sessions, model=ProviderModels(priced, build=gemini.build))
    calls: list[UsageCall] = []

    async def sink(call: UsageCall) -> None:
        calls.append(call)

    with recording(sink):
        result = await w.run(payload(line(node("triage", "llm", llm_config()), end("done")), {}))

    assert result.status is JobStatus.OK
    [call] = calls
    assert call.attribution.organization_id == ORGANIZATION
    assert (call.attribution.kind, call.attribution.subject_name) == ("workflow", "Test agent")
    assert call.user_id == MEMBER
    assert (call.call, call.name) == ("model", "google/gemini-test")
    assert (call.input_tokens, call.output_tokens) == (1_000, 200)
    assert call.cost == pytest.approx((1_000 * 2 + 200 * 10) / 1_000_000)


async def test_a_run_outside_a_recording_worker_records_nothing(sessions: DatabaseSessionService) -> None:
    w = worker(sessions, model=ProviderModels(PROVIDERS, build=MeteredGemini().build))
    result = await w.run(payload(line(node("triage", "llm", llm_config()), end("done")), {}))
    assert result.status is JobStatus.OK


async def test_an_llm_node_on_a_worker_without_models_fails_the_run_saying_so(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    document = line(node("triage", "llm", llm_config()), end("done"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == NOT_SET_UP


async def test_a_model_that_answers_with_an_error_fails_its_step_and_nothing_after_it_runs(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions, model=ScriptedLlm(turns=[blocked_for_safety()]))
    document = line(
        node("triage", "llm", llm_config(instruction="Triage it")),
        node("call", "http", {"method": "POST", "url": "https://api.example.com/x"}),
        end("done", "steps.triage.output"),
    )

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == "Triage (triage) failed: its model answered with an error (SAFETY): Blocked for safety"
    assert result.detail["step"] == "triage"
    assert w.sent == []  # the step after it never ran


async def test_a_sub_agent_whose_model_answers_with_an_error_fails_its_team(sessions: DatabaseSessionService) -> None:
    w = worker(sessions, model=ScriptedLlm(turns=[blocked_for_safety()]))
    team = {"description": "", "sub_agents": [{"id": "first", "kind": "llm", "name": "First", "config": sub_config()}]}
    document = line(node("team", "sequential", team), end("done"))

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "Team (team), sub-agent First (first) failed: its model answered with an error (SAFETY): Blocked for safety"
    )


# ------------------------------------------------------------------ closing what a run opened


async def test_a_runner_that_never_finishes_closing_doesnt_hold_the_run(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "CLOSE_SECONDS", 0.05)

    async def never(self: Runner) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Runner, "close", never)
    w = worker(sessions)

    result = await asyncio.wait_for(w.run(payload(line(end("done", '"shipped"')), {})), 5)

    assert result.status is JobStatus.OK
    assert result.detail["result"] == "shipped"


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


def test_an_attempt_may_take_twice_the_run_timeout_unless_set() -> None:
    assert AdkWorkflowsSettings().attempt_seconds == 7200
    assert AdkWorkflowsSettings(run_timeout=600).attempt_seconds == 1200
    assert AdkWorkflowsSettings(run_timeout=600, attempt_timeout=900).attempt_seconds == 900
    assert AdkWorkflowsSettings(attempt_timeout="").attempt_timeout is None  # type: ignore[arg-type]


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


async def test_the_workers_one_http_client_keeps_no_cookies() -> None:
    # One client serves every organization's runs: their HTTP steps and their agents' HTTP tools.
    task = AdkWorkflowsTaskFactory().build(TaskContext(settings=None, options=AdkWorkflowsSettings()))  # type: ignore[arg-type]
    try:
        http = task.services.http
        assert http is not None and task.services.agents is not None and task.services.agents.http is http
        login = http.build_request("GET", "https://api.example.com/login")
        http.cookies.extract_cookies(
            httpx.Response(200, headers={"Set-Cookie": "session=org-a; Path=/; Secure"}, request=login)
        )
        assert "cookie" not in http.build_request("GET", "https://api.example.com/orders").headers
    finally:
        await task.close()


async def test_a_cookie_set_in_one_organizations_run_never_goes_out_with_anothers(
    sessions: DatabaseSessionService,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": "session=org-a; Path=/; Secure"})

    w = worker(sessions, answer=answer)
    calling = line(node("call", "http", {"method": "GET", "url": "https://api.example.com/x"}), end("done"))
    await w.run(payload(calling, {}).model_copy(update={"tenant_id": "org-a", "session_id": "org-a-run"}))
    w.control = Control(instance="org-b-task")
    await w.run(payload(calling, {}).model_copy(update={"tenant_id": "org-b", "session_id": "org-b-run"}))

    assert [request.headers.get("cookie") for request in w.sent] == [None, None]


async def test_closing_the_task_closes_its_code_graph() -> None:
    # A real CodeGraph, so a renamed close method fails here, not on a worker's shutdown.
    closed: list[bool] = []

    async def on_close() -> None:
        closed.append(True)

    code = CodeGraph(store=None, on_close=on_close)  # type: ignore[arg-type]
    services = RunServices(model=NoModels(), agents=AgentServices(models=NoModels(), code=code))
    task = AdkWorkflowsTask(AdkWorkflowsSettings(), services, None)

    await task.close()

    assert closed == [True]


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


def test_runs_keep_their_sessions_at_read_committed_but_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.adk.sessions

    made: list[dict[str, Any]] = []

    class Recorded:
        def __init__(self, **options: Any) -> None:
            made.append(options)

    monkeypatch.setattr(google.adk.sessions, "DatabaseSessionService", Recorded)
    mysql, sqlite = "mysql+aiomysql://forge:secret@db:3306/forge_admin", "sqlite+aiosqlite:///sessions.db"
    for url in (mysql, sqlite):
        build_sessions(AdkWorkflowsSettings(session_database_url=url))

    assert made == [{"db_url": mysql, "isolation_level": SESSION_ISOLATION}, {"db_url": sqlite}]
    assert SESSION_ISOLATION == "READ COMMITTED"


async def test_the_factory_takes_its_services_and_sessions_from_the_context(
    sessions: DatabaseSessionService,
) -> None:
    services = RunServices(model=NoModels())
    task = AdkWorkflowsTaskFactory().build(
        TaskContext(settings=None, extras={SERVICES: services, SESSIONS: sessions})  # type: ignore[arg-type]
    )
    assert task.services is services
    assert task.sessions is sessions


# ------------------------------------------------------------------ the activity


async def test_a_run_notes_so_many_lines_then_one_that_says_the_rest_arent_noted(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    run = AdkRun(
        payload(line(transform("shape", "1"))),
        control=w.control,
        services=w.task.services,
        sessions=sessions,
        settings=w.task.settings,
    )

    for index in range(MAX_NOTES + 5):
        await run._note(f"step {index} finished", step=index)

    said = w.control.said()
    assert len(said) == MAX_NOTES + 1 and said[MAX_NOTES - 1] == f"step {MAX_NOTES - 1} finished"
    assert said[-1] == f"{MAX_NOTES} notes so far: what the run does next isn't noted"
    assert w.control.notes[-1][1] == {}
    assert (run.state["notes"], run.state["notes_lost"]) == (MAX_NOTES + 1, 0)


async def test_a_note_that_cant_be_written_is_counted_with_what_the_run_keeps(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, tried = worker(sessions), []

    async def unwritable(message: str, **attributes: Any) -> None:
        tried.append(message)
        raise ConnectionError("the database is gone")

    monkeypatch.setattr(w.control, "note", unwritable)

    result = await w.run(payload(line(transform("shape", "1"))))

    assert result.detail["outcome"] == "succeeded" and len(tried) >= 2
    # Its last note too, after its last checkpoint: what the run keeps as it ends has it.
    assert w.control.load(STATE_KEY)["notes_lost"] == len(tried)


def test_an_error_adk_wraps_is_named_by_the_one_the_step_raised() -> None:
    from google.adk.workflow._errors import DynamicNodeFailError

    raised = errors.ClientError(404, {"error": {"message": "no such model"}})
    wrapped = DynamicNodeFailError(message="Dynamic node steps failed", error=raised, error_node_path="steps@1")

    refused = runs._classified(wrapped)

    assert str(refused) == "ClientError (HTTP 404): no such model"
    assert isinstance(runs._classified(errors.ServerError(503, {"error": {"message": "busy"}})), TransientError)
