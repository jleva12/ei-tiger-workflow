"""The assistant's workflow and background task tools, against a stand-in for
the admin API.

The stand-in records each call and answers as the routes would, so these check
what the tools send (as whom, where, with what) and what the model gets back.
What a change sends is checked against the route's own request model.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any, get_args
from unittest.mock import MagicMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from google.adk.agents import LlmAgent
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import SecretStr
from scripted_llm import ScriptedLlm

from forge_admin.agents import access_tools, admin_tools, event_tools, workflow_tools
from forge_admin.agents.person_api import PersonApi
from forge_admin.agents.workflow_tools import WorkflowToolset
from forge_admin.api.routes import background_tasks as task_routes
from forge_admin.api.routes.background_tasks import DecisionCreate
from forge_admin.api.routes.workflows import RunCreate
from forge_admin.auth.tokens import verify_token
from forge_admin.config import Settings
from forge_admin.workflows import ID_PATTERN

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e03"
WORKFLOW = "wf_k3j9x2m1qa"
TASK = "01JB8Z3W0Q9V6K4T2R7N5M1P8S"
OTHER_TASK = "01JB8Z3W0Q9V6K4T2R7N5M1P8T"
APPROVAL = "approval-7"
PREFIX = "/api/v1"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


class Api:
    """Answers each call with ``handler``, and records it."""

    def __init__(self, settings: Settings, handler: Handler) -> None:
        self.calls: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(record), base_url="http://admin"
        )
        self.person_api = PersonApi(client, settings)

    def sent(self, index: int = -1) -> tuple[str, str, list[tuple[str, str]]]:
        """A call's method, path and query."""
        request = self.calls[index]
        return request.method, request.url.path, list(request.url.params.multi_items())

    def body(self, index: int = -1) -> Any:
        return json.loads(self.calls[index].content or b"null")


def answer(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


def context(user_id: str | None = "ada") -> Any:
    return MagicMock(user_id=user_id, actions=EventActions())


def call(toolset: WorkflowToolset, name: str, user: str | None = "ada", **args: Any):
    async def main() -> Any:
        tools = {tool.name: tool for tool in await toolset.get_tools()}
        return await tools[name].run_async(args=args, tool_context=context(user))

    return asyncio.run(main())


def tools(signing: Settings, handler: Handler) -> tuple[WorkflowToolset, Api]:
    api = Api(signing, handler)
    return WorkflowToolset(api.person_api, confirm_changes=False), api


def document(**over: Any) -> dict[str, Any]:
    """A workflow document: a start step taking a ticket, then an approval."""
    return {
        "format": "forge.workflow/v1",
        "id": WORKFLOW,
        "name": "Triage a ticket",
        "description": "Reads a ticket and asks a lead",
        "entry": "start",
        "nodes": [
            {
                "id": "start",
                "kind": "entry",
                "name": "Start",
                "config": {
                    "trigger": "manual",
                    "event_types": [],
                    "cron": "",
                    "timezone": "UTC",
                    "input_schema": {
                        "type": "object",
                        "properties": {"ticket": {"type": "string"}},
                        "required": ["ticket"],
                    },
                },
                "outputs": ["next"],
            },
            {
                "id": "ask",
                "kind": "approval",
                "name": "Ask a lead",
                "config": {
                    "message": "Ship it?",
                    "approvers": "org:admin",
                    "timeout_hours": 0,
                },
                "outputs": ["approved", "rejected"],
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "source_output": "next", "target": "ask"}
        ],
        "layout": {"start": {"x": 0, "y": 0}, "ask": {"x": 200, "y": 0}},
        **over,
    }


def workflow_record() -> dict[str, Any]:
    return {
        "id": WORKFLOW,
        "organization_id": ORG,
        "revision": 4,
        "document": document(),
        "created_at": "2026-09-01T10:00:00Z",
        "created_by": "ada",
        "updated_at": "2026-09-20T10:00:00Z",
        "updated_by": "grace",
        "updated_by_name": "Grace Hopper",
    }


def task_summary(task_id: str = TASK, **over: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "job_name": "workflows.run",
        "task_type": "workflows",
        "kind": "run",
        "description": "Triage a ticket",
        "status": "AWAITING_VALIDATION",
        "outcome": None,
        "attempts": 1,
        "created_at": "2026-09-27T09:00:00Z",
        "updated_at": "2026-09-27T09:01:00Z",
        "started_at": "2026-09-27T09:00:01Z",
        "ended_at": None,
        "duration_ms": None,
        "failure": None,
        "waiting_until": None,
        "waiting_reason": None,
        "awaiting_approval": True,
        **over,
    }


def task_detail(task_id: str = TASK, **over: Any) -> dict[str, Any]:
    return {
        **task_summary(task_id),
        "payload": {
            "tenant_id": ORG,
            "workflow_id": WORKFLOW,
            "revision": 4,
            "name": "Triage a ticket",
            "document": document(),
            "input": {"ticket": "FORGE-12"},
            "run_as": "ada",
            "run_as_name": "Ada Lovelace",
            "trigger": {"type": "manual", "by": "ada"},
            "depth": 0,
            "parent": None,
        },
        "labels": {"tenant": ORG, "workflow": WORKFLOW, "task_type": "workflows"},
        "delivery": {"queue": "workflows", "key": "workflows.run:wf:1"},
        "requested_by": {"kind": "user", "id": "ada", "display_name": "Ada Lovelace"},
        "correlation_id": "c-1",
        "result": None,
        "runs": [
            {
                "id": "run-1",
                "attempt": 1,
                "status": "AWAITING_VALIDATION",
                "exit_code": None,
                "exit_description": "",
                "requested_by": {"kind": "user", "id": "ada", "display_name": ""},
                "failures": [
                    {
                        "type": "TimeoutError",
                        "message": "The ticket service timed out",
                        "category": "transient",
                        "retryable": True,
                        "permanent": False,
                        "step": "job",
                        "stack_trace": "Traceback\n" + "  at x\n" * 2000,
                        "cause_chain": ["a", "b", "c", "d"],
                    }
                ],
                "steps": [{"name": "job", "status": "RUNNING", "attempt": 1}],
            }
        ],
        "events": [
            {
                "sequence": index,
                "run_id": "run-1",
                "at": f"2026-09-27T09:{index:02d}:00Z",
                "type": "STATUS_CHANGED",
                "actor": {"kind": "system", "id": "worker", "display_name": ""},
                "from_status": "PENDING",
                "to_status": "RUNNING",
                "message": f"event {index}",
            }
            for index in range(40)
        ],
        "approval": {
            "id": APPROVAL,
            "reason": "Ship it?",
            "details": {
                "step": "ask",
                "step_name": "Ask a lead",
                "approvers": "org:admin",
                "workflow_id": WORKFLOW,
                "workflow_name": "Triage a ticket",
            },
            "requested_at": "2026-09-27T09:01:00Z",
            "deadline": None,
        },
        "actions": {
            "resubmit": False,
            "restart": False,
            "abandon": False,
            "decide": True,
        },
        **over,
    }


def test_every_call_is_the_conversations_person_with_the_api_key(
    signing: Settings,
) -> None:
    toolset, api = tools(signing, answer([]))
    got = call(toolset, "list_workflows", user="grace", organization_id=ORG)
    assert got["status"] == "success"

    sent = api.calls[0]
    token = sent.headers["authorization"].removeprefix("Bearer ")
    assert verify_token(signing, token) == "grace"
    assert sent.headers["x-api-key"] == "k" * 32

    # Without a person there's nobody to act as, and nothing is called.
    refused = call(toolset, "list_workflows", user=None, organization_id=ORG)
    assert refused["status"] == "failed"
    assert len(api.calls) == 1


def test_the_tool_names_are_the_toolsets_and_nobody_elses() -> None:
    assert set(workflow_tools.READ_TOOLS).isdisjoint(workflow_tools.CHANGE_TOOLS)
    assert WorkflowToolset.tool_names() == workflow_tools.TOOL_NAMES
    assert workflow_tools.TOOL_NAMES.isdisjoint(
        access_tools.TOOL_NAMES | admin_tools.TOOL_NAMES | event_tools.TOOL_NAMES
    )


def test_the_id_formats_and_statuses_are_the_routes() -> None:
    assert workflow_tools.WORKFLOW_ID.pattern == ID_PATTERN
    assert get_args(workflow_tools.BackgroundTaskStatus) == get_args(
        task_routes.TaskStatus
    )


def test_listing_workflows_leaves_out_their_documents(signing: Settings) -> None:
    toolset, api = tools(signing, answer([workflow_record()]))
    got = call(toolset, "list_workflows", organization_id=ORG)["payload"]

    assert api.sent() == ("GET", f"{PREFIX}/organizations/{ORG}/workflows", [])
    assert got == [
        {
            "id": WORKFLOW,
            "name": "Triage a ticket",
            "description": "Reads a ticket and asks a lead",
            "revision": 4,
            "updated_at": "2026-09-20T10:00:00Z",
            "updated_by_name": "Grace Hopper",
            "start": {
                "trigger": "manual",
                "timezone": "UTC",
                "input_fields": ["ticket"],
                "required_fields": ["ticket"],
            },
            "step_count": 2,
        }
    ]


def test_a_workflow_comes_with_its_steps_and_its_document_only_when_asked(
    signing: Settings,
) -> None:
    toolset, api = tools(signing, answer(workflow_record()))
    got = call(toolset, "get_workflow", organization_id=ORG, workflow_id=WORKFLOW)

    assert api.sent() == (
        "GET",
        f"{PREFIX}/organizations/{ORG}/workflows/{WORKFLOW}",
        [],
    )
    detail = got["payload"]
    assert "document" not in detail
    assert detail["start"]["input_schema"]["required"] == ["ticket"]
    assert detail["entry"] == "start"
    assert detail["steps"] == [
        {"id": "start", "kind": "entry", "name": "Start"},
        {
            "id": "ask",
            "kind": "approval",
            "name": "Ask a lead",
            "approvers": "org:admin",
            "message": "Ship it?",
            "timeout_hours": 0,
        },
    ]
    assert detail["ways"] == [{"from": "start", "way": "next", "to": "ask"}]

    whole = call(
        toolset,
        "get_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        include_document=True,
    )["payload"]
    assert whole["document"] == document()


def test_a_workflows_runs_are_paged_and_summarised(signing: Settings) -> None:
    toolset, api = tools(signing, answer({"items": [task_detail()], "total": 1}))
    got = call(
        toolset,
        "list_workflow_runs",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        limit=500,
        offset=20,
    )["payload"]

    assert api.sent() == (
        "GET",
        f"{PREFIX}/organizations/{ORG}/workflows/{WORKFLOW}/runs",
        [("limit", "100"), ("offset", "20")],
    )
    # A page's items are summaries: none of a detail's payload or history.
    assert got["total"] == 1
    assert got["items"] == [
        {
            "id": TASK,
            "task_type": "workflows",
            "kind": "run",
            "description": "Triage a ticket",
            "status": "AWAITING_VALIDATION",
            "attempts": 1,
            "created_at": "2026-09-27T09:00:00Z",
            "updated_at": "2026-09-27T09:01:00Z",
            "started_at": "2026-09-27T09:00:01Z",
            "awaiting_approval": True,
        }
    ]


def test_listing_background_tasks_sends_only_the_filters_given(
    signing: Settings,
) -> None:
    failure = {
        "type": "ValueError",
        "message": "No such file",
        "category": "permanent",
        "occurred_at": "2026-09-27T09:00:00Z",
    }
    toolset, api = tools(
        signing,
        answer(
            {
                "items": [
                    task_summary(
                        status="FAILED", awaiting_approval=False, failure=failure
                    )
                ],
                "total": 7,
            }
        ),
    )
    got = call(
        toolset,
        "list_background_tasks",
        organization_id=ORG,
        statuses=["FAILED", "STOPPED"],
        task_types=["documents", "workflows"],
        limit=0,
    )["payload"]

    assert api.sent() == (
        "GET",
        f"{PREFIX}/organizations/{ORG}/background-tasks",
        [
            ("status", "FAILED"),
            ("status", "STOPPED"),
            ("task_type", "documents"),
            ("task_type", "workflows"),
            ("limit", "1"),
            ("offset", "0"),
        ],
    )
    assert got["total"] == 7
    assert got["items"][0]["failure"] == failure
    assert got["items"][0]["awaiting_approval"] is False

    call(toolset, "list_background_tasks", organization_id=ORG)
    assert api.sent()[2] == [("limit", "20"), ("offset", "0")]

    call(
        toolset,
        "list_background_tasks",
        organization_id=ORG,
        exclude_task_types=["workflows"],
    )
    assert api.sent()[2] == [
        ("exclude_task_type", "workflows"),
        ("limit", "20"),
        ("offset", "0"),
    ]


def test_a_task_is_read_without_its_document_stack_traces_or_older_history(
    signing: Settings,
) -> None:
    runs = [
        {**task_detail()["runs"][0], "id": f"run-{n}", "attempt": n}
        for n in range(8, 0, -1)
    ]
    toolset, api = tools(
        signing, answer(task_detail(runs=runs, result={"log": "x" * 5000}))
    )
    got = call(toolset, "get_background_task", organization_id=ORG, task_id=TASK)[
        "payload"
    ]

    assert api.sent() == (
        "GET",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{TASK}",
        [],
    )
    # What a run was asked, without the workflow's whole document.
    assert got["payload"] == {
        "workflow_id": WORKFLOW,
        "revision": 4,
        "name": "Triage a ticket",
        "input": {"ticket": "FORGE-12"},
        "run_as": "ada",
        "run_as_name": "Ada Lovelace",
        "trigger": {"type": "manual", "by": "ada"},
        "depth": 0,
    }
    assert got["approval"] == {
        "reason": "Ship it?",
        "requested_at": "2026-09-27T09:01:00Z",
        "request_id": APPROVAL,
        "details": task_detail()["approval"]["details"],
    }
    assert got["actions"]["decide"] is True
    assert got["requested_by"] == "Ada Lovelace"
    assert "delivery" not in got
    # Its newest attempts, their failures without stack traces.
    assert [a["attempt"] for a in got["attempt_history"]] == [8, 7, 6, 5, 4]
    assert got["attempts_left_out"] == 3
    assert got["attempt_history"][0]["failures"] == [
        {
            "type": "TimeoutError",
            "message": "The ticket service timed out",
            "category": "transient",
            "step": "job",
            "retryable": True,
            "causes": ["a", "b", "c"],
        }
    ]
    assert got["attempt_history"][0]["requested_by"] == "ada"
    # Its latest events.
    assert len(got["events"]) == workflow_tools.MAX_EVENTS
    assert got["events"][-1]["message"] == "event 39"
    assert got["events_left_out"] == 10
    # Long text is cut.
    assert len(got["result"]["log"]) < 1100
    assert "stack_trace" not in json.dumps(got)


def test_another_tasks_payload_keeps_all_but_a_document(signing: Settings) -> None:
    payload = {"repo_id": "r-1", "shas": [f"{n:040x}" for n in range(60)]}
    toolset, _ = tools(
        signing,
        answer(task_detail(task_type="commits", payload=payload, approval=None)),
    )
    got = call(toolset, "get_background_task", organization_id=ORG, task_id=TASK)[
        "payload"
    ]

    assert got["payload"]["repo_id"] == "r-1"
    assert len(got["payload"]["shas"]) == workflow_tools.MAX_ITEMS + 1
    assert got["payload"]["shas"][-1] == "… 35 more"
    assert "approval" not in got


def test_approvals_are_read_from_the_tasks_waiting_for_them(
    signing: Settings,
) -> None:
    gone = "01JB8Z3W0Q9V6K4T2R7N5M1P8V"
    member_approval = {
        **task_detail()["approval"],
        "id": "approval-9",
        "details": {**task_detail()["approval"]["details"], "approvers": "org:member"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/background-tasks"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        task_summary(TASK),
                        task_summary(gone),
                        task_summary(OTHER_TASK),
                        task_summary("../../me"),
                    ],
                    "total": 4,
                },
            )
        if path.endswith(gone):
            return httpx.Response(404, json={"detail": "No such task"})
        if path.endswith(OTHER_TASK):
            return httpx.Response(
                200, json=task_detail(OTHER_TASK, approval=member_approval)
            )
        return httpx.Response(200, json=task_detail(TASK))

    toolset, api = tools(signing, handler)
    got = call(toolset, "list_workflow_approvals", organization_id=ORG, limit=99)[
        "payload"
    ]

    assert api.sent(0) == (
        "GET",
        f"{PREFIX}/organizations/{ORG}/background-tasks",
        [("status", "AWAITING_VALIDATION"), ("limit", "20")],
    )
    # One read per task; an ID that isn't one never reaches a path.
    assert [request.url.path for request in api.calls[1:]] == [
        f"{PREFIX}/organizations/{ORG}/background-tasks/{TASK}",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{gone}",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{OTHER_TASK}",
    ]
    assert got == {
        "items": [
            {
                "task_id": TASK,
                "request_id": APPROVAL,
                "question": "Ship it?",
                "workflow_id": WORKFLOW,
                "workflow_name": "Triage a ticket",
                "step": "Ask a lead",
                "approvers": "org:admin",
                "requested_at": "2026-09-27T09:01:00Z",
                "run_as": "Ada Lovelace",
                "decidable": True,
            },
            {
                "task_id": OTHER_TASK,
                "request_id": "approval-9",
                "question": "Ship it?",
                "workflow_id": WORKFLOW,
                "workflow_name": "Triage a ticket",
                "step": "Ask a lead",
                "approvers": "org:member",
                "requested_at": "2026-09-27T09:01:00Z",
                "run_as": "Ada Lovelace",
                "decidable": True,
            },
        ],
        "total": 4,
    }


def test_approvals_fail_when_a_task_cant_be_read(signing: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/background-tasks"):
            return httpx.Response(200, json={"items": [task_summary()], "total": 1})
        return httpx.Response(503, json={"detail": "The async worker is unavailable"})

    toolset, _ = tools(signing, handler)
    got = call(toolset, "list_workflow_approvals", organization_id=ORG)
    assert got["status"] == "failed"
    assert "try again later" in got["suggested_fixes"][0]


def test_running_a_workflow_sends_what_the_route_takes(signing: Settings) -> None:
    started = {
        "queue": "workflows",
        "key": f"workflows.run:{WORKFLOW}:1",
        "workflow_id": WORKFLOW,
        "revision": 4,
    }
    toolset, api = tools(signing, answer(started, 202))

    got = call(
        toolset,
        "run_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        input={"ticket": "FORGE-12"},
    )
    assert got["payload"] == started
    assert api.sent() == (
        "POST",
        f"{PREFIX}/organizations/{ORG}/workflows/{WORKFLOW}/runs",
        [],
    )
    assert api.body() == {"input": {"ticket": "FORGE-12"}}
    assert RunCreate.model_validate(api.body()).input == {"ticket": "FORGE-12"}

    # Without an input, the route's default.
    call(toolset, "run_workflow", organization_id=ORG, workflow_id=WORKFLOW)
    assert api.body() == {}
    assert RunCreate.model_validate(api.body()).input is None


def test_deciding_an_approval_sends_what_the_route_takes(signing: Settings) -> None:
    toolset, api = tools(signing, answer({"queue": "workflows", "key": "k"}, 202))

    call(
        toolset,
        "decide_workflow_approval",
        organization_id=ORG,
        task_id=TASK,
        request_id=APPROVAL,
        approved=False,
        comment="Not before the release",
    )
    assert api.sent() == (
        "POST",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{TASK}/decisions",
        [],
    )
    assert api.body() == {
        "request_id": APPROVAL,
        "approved": False,
        "comment": "Not before the release",
    }
    DecisionCreate.model_validate(api.body())

    call(
        toolset,
        "decide_workflow_approval",
        organization_id=ORG,
        task_id=TASK,
        request_id=APPROVAL,
        approved=True,
    )
    decision = DecisionCreate.model_validate(api.body())
    assert (decision.approved, decision.comment) == (True, "")


@pytest.mark.parametrize("action", ["resubmit", "restart"])
def test_resubmitting_and_restarting_send_no_body(
    signing: Settings, action: str
) -> None:
    toolset, api = tools(signing, answer({"queue": "workflows", "key": "k"}, 202))
    got = call(toolset, f"{action}_background_task", organization_id=ORG, task_id=TASK)

    assert got["payload"] == {"queue": "workflows", "key": "k"}
    assert api.sent() == (
        "POST",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{TASK}/{action}",
        [],
    )
    # The routes take no body: who acts is the person's token.
    assert api.calls[-1].content == b""


def test_abandoning_answers_with_the_task_summarised(signing: Settings) -> None:
    toolset, api = tools(
        signing,
        answer(task_detail(status="ABANDONED", awaiting_approval=False, approval=None)),
    )
    got = call(toolset, "abandon_background_task", organization_id=ORG, task_id=TASK)

    assert api.sent() == (
        "POST",
        f"{PREFIX}/organizations/{ORG}/background-tasks/{TASK}/abandon",
        [],
    )
    assert api.calls[-1].content == b""
    assert got["payload"]["status"] == "ABANDONED"
    assert "payload" not in got["payload"]
    assert "runs" not in got["payload"]


@pytest.mark.parametrize(
    ("status", "body", "fix"),
    [
        (403, {"detail": "Requires workflows:approve"}, "lacks the permission"),
        (
            404,
            {"detail": "The organization has no such background task"},
            "Check the IDs",
        ),
        (
            409,
            {"detail": "That approval isn't open any more"},
            "get_background_task",
        ),
        (
            422,
            {"detail": [{"loc": ["body", "comment"], "msg": "at most 4000"}]},
            "Fix the arguments",
        ),
        (503, {"detail": "The async worker is unavailable"}, "try again later"),
    ],
)
def test_refusals_come_back_failed_with_the_reason_and_how_to_go_on(
    signing: Settings, status: int, body: dict[str, Any], fix: str
) -> None:
    toolset, _ = tools(signing, answer(body, status))
    got = call(
        toolset,
        "decide_workflow_approval",
        organization_id=ORG,
        task_id=TASK,
        request_id=APPROVAL,
        approved=True,
    )

    assert got["status"] == "failed"
    reason = body["detail"]
    assert got["reason"] == (
        reason if isinstance(reason, str) else "comment: at most 4000"
    )
    assert fix in got["suggested_fixes"][0]


def test_a_bad_run_input_points_at_the_start_steps_fields(
    signing: Settings,
) -> None:
    toolset, _ = tools(
        signing,
        answer({"detail": "The input doesn't fit the start step: ticket"}, 422),
    )
    got = call(toolset, "run_workflow", organization_id=ORG, workflow_id=WORKFLOW)
    assert got["status"] == "failed"
    assert "get_workflow" in got["suggested_fixes"][0]


@pytest.mark.parametrize(
    ("tool", "argument", "value"),
    [
        ("get_workflow", "organization_id", "not-an-organization"),
        ("get_workflow", "workflow_id", f"{WORKFLOW}/runs"),
        ("get_workflow", "workflow_id", "../../me"),
        ("get_workflow", "workflow_id", "wf_UPPER123"),
        ("run_workflow", "workflow_id", "wf_ab"),
        ("list_workflow_runs", "workflow_id", f"{WORKFLOW}?limit=1"),
        ("get_background_task", "task_id", f"{TASK}/abandon"),
        ("get_background_task", "task_id", "../../me"),
        ("abandon_background_task", "task_id", "x" * 65),
        ("restart_background_task", "task_id", "a b"),
        ("decide_workflow_approval", "task_id", f"{TASK}%2Fabandon"),
        ("list_background_tasks", "organization_id", f"{ORG}/../x"),
        ("list_workflow_approvals", "organization_id", "../../me"),
    ],
)
def test_no_argument_reaches_another_route(
    signing: Settings, tool: str, argument: str, value: str
) -> None:
    toolset, api = tools(signing, answer({}))
    args: dict[str, Any] = {"organization_id": ORG}
    if tool in ("get_workflow", "run_workflow", "list_workflow_runs"):
        args["workflow_id"] = WORKFLOW
    if tool in (
        "get_background_task",
        "abandon_background_task",
        "restart_background_task",
        "decide_workflow_approval",
    ):
        args["task_id"] = TASK
    if tool == "decide_workflow_approval":
        args |= {"request_id": APPROVAL, "approved": True}
    got = call(toolset, tool, **{**args, argument: value})

    assert got["status"] == "failed"
    assert argument in got["reason"]
    assert api.calls == []


def test_without_a_jwt_secret_there_are_no_workflow_tools(
    settings: Settings,
) -> None:
    app = FastAPI()
    app.state.settings = settings.model_copy(update={"jwt_secret": None})
    assert workflow_tools.toolset(app) is None
    app.state.settings = settings.model_copy(update={"jwt_secret": SecretStr("s" * 32)})
    assert isinstance(workflow_tools.toolset(app), WorkflowToolset)


def test_a_decision_waits_for_the_person_to_confirm_it(signing: Settings) -> None:
    api = Api(signing, answer({"queue": "workflows", "key": "k"}, 202))
    toolset = WorkflowToolset(api.person_api)
    llm = ScriptedLlm(
        turns=[
            [
                types.Part.from_function_call(
                    name="decide_workflow_approval",
                    args={
                        "organization_id": ORG,
                        "task_id": TASK,
                        "request_id": APPROVAL,
                        "approved": True,
                    },
                )
            ],
            [types.Part.from_text(text="Waiting for you to confirm.")],
        ]
    )
    agent = LlmAgent(name="forge", model=llm, tools=[toolset])
    runner = InMemoryRunner(agent=agent, app_name="forge")

    async def main() -> list[Any]:
        session = await runner.session_service.create_session(
            app_name="forge", user_id="ada"
        )
        message = types.Content(
            role="user", parts=[types.Part.from_text(text="Approve it")]
        )
        return [
            event
            async for event in runner.run_async(
                user_id="ada", session_id=session.id, new_message=message
            )
        ]

    events = asyncio.run(main())
    asked = [
        call.name
        for event in events
        for call in event.get_function_calls()
        if call.name == "adk_request_confirmation"
    ]
    assert asked == ["adk_request_confirmation"]
    # Nothing is decided until the person says so.
    assert api.calls == []


# -- Building workflows ------------------------------------------------------

OTHER_WORKFLOW = "wf_p8r2t5v7wz"


# The assistant's models, as the models route answers (camelCase).
MODELS = {
    "defaultModel": "openai/gpt-5.2",
    "models": [
        {
            "id": "openai/gpt-5.2",
            "provider": "openai",
            "providerName": "OpenAI",
            "model": "gpt-5.2",
            "name": "GPT-5.2",
            "api": "openai-responses",
            "reasoning": True,
            "input": ["text", "image"],
            "contextWindow": 400000,
            "maxTokens": 128000,
            "thinkingLevels": ["off", "minimal", "low", "medium", "high"],
        },
        {
            "id": "anthropic/claude-opus-5",
            "provider": "anthropic",
            "providerName": "Anthropic",
            "model": "claude-opus-5",
            "name": "Claude Opus 5",
            "api": "anthropic-messages",
            "reasoning": True,
            "input": ["text", "image"],
            "contextWindow": 200000,
            "maxTokens": 64000,
            "thinkingLevels": ["off", "minimal", "low", "medium", "high", "xhigh"],
        },
    ],
}


def organization_routes(request: httpx.Request) -> httpx.Response:
    """The routes the building tools read, as the organization's would answer."""
    path = request.url.path.removeprefix(PREFIX)
    if path == f"/organizations/{ORG}/event-types":
        schema = {
            "type": "object",
            "properties": {
                "request": {"type": "object", "properties": {"id": {"type": "string"}}}
            },
        }
        return httpx.Response(
            200,
            json=[
                {
                    "key": "purchase.requested",
                    "name": "Purchase requested",
                    "status": "active",
                    "payload_schema": schema,
                }
            ],
        )
    if path == "/agents/apps/forge/models":
        return httpx.Response(200, json=MODELS)
    if request.method == "GET" and path == f"/organizations/{ORG}/workflows":
        return httpx.Response(
            200, json=[{"id": OTHER_WORKFLOW, "revision": 2, "name": "Notify"}]
        )
    if request.method == "POST" and path == f"/organizations/{ORG}/workflows":
        document = json.loads(request.content)["document"]
        return httpx.Response(
            201, json={"id": WORKFLOW, "revision": 1, "document": document}
        )
    if request.method == "GET" and path == f"/organizations/{ORG}/workflows/{WORKFLOW}":
        current = {
            "nodes": [],
            "layout": {"start": {"x": 1, "y": 2}, "gone": {"x": 3, "y": 4}},
        }
        return httpx.Response(
            200, json={"id": WORKFLOW, "revision": 7, "document": current}
        )
    if request.method == "PUT" and path == f"/organizations/{ORG}/workflows/{WORKFLOW}":
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": WORKFLOW,
                "revision": body["revision"] + 1,
                "document": body["document"],
            },
        )
    return httpx.Response(404, json={"detail": "Not Found"})


def a_draft(workflow: str = OTHER_WORKFLOW) -> dict[str, Any]:
    return {
        "name": "Review requests",
        "nodes": [
            {
                "id": "start",
                "kind": "entry",
                "config": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                    }
                },
            },
            {
                "id": "review",
                "kind": "approval",
                "name": "Review it",
                "config": {"message": "Approve {{ input.title }}?"},
            },
            {
                "id": "notify",
                "kind": "subworkflow",
                "name": "Tell them",
                "config": {"workflow": workflow},
            },
        ],
        "edges": [
            {"source": "start", "target": "review"},
            {"source": "review", "source_output": "approved", "target": "notify"},
        ],
    }


def building(signing: Settings) -> tuple[WorkflowToolset, Api]:
    api = Api(signing, organization_routes)
    toolset = WorkflowToolset(
        api.person_api, confirm_changes=False, web_url="https://forge.test/"
    )
    return toolset, api


def test_the_building_blocks_are_the_format_and_the_organizations_own_pieces(
    signing: Settings,
) -> None:
    toolset, _ = building(signing)
    got = call(toolset, "get_workflow_building_blocks", organization_id=ORG)
    assert got["status"] == "success" and "truncated" not in got, got
    blocks = got["payload"]
    assert blocks["how_to_write_one"] == workflow_tools.GUIDE
    assert blocks["step_kinds"]["approval"]["outputs"] == ["approved", "rejected"]
    assert "coding_task" not in blocks["step_kinds"]
    assert "integration" not in blocks["step_kinds"]
    assert blocks["organization"] == {
        "event_types": [
            {
                "key": "purchase.requested",
                "name": "Purchase requested",
                "status": "active",
                "payload": {"request": {"id": "string"}},
            }
        ]
    }
    # The models an agent may run on, as its config.model names them.
    assert blocks["models"] == {
        "default": "openai/gpt-5.2",
        "offered": [
            {
                "model": {"provider": "openai", "name": "gpt-5.2"},
                "label": "GPT-5.2",
                "thinking_levels": ["off", "minimal", "low", "medium", "high"],
            },
            {
                "model": {"provider": "anthropic", "name": "claude-opus-5"},
                "label": "Claude Opus 5",
                "thinking_levels": ["off", "minimal", "low", "medium", "high", "xhigh"],
            },
        ],
    }
    assert blocks["step_kinds"]["agent"]["defaults"]["thinking_level"] == ""


def test_a_draft_runs_only_the_organizations_workflows(signing: Settings) -> None:
    toolset, api = building(signing)
    good = call(
        toolset, "check_workflow_draft", organization_id=ORG, document=a_draft()
    )
    assert good["payload"] == {
        "valid": True,
        "errors": [],
        "warnings": [],
        "steps": 3,
        "connections": 2,
    }

    bad = call(
        toolset,
        "check_workflow_draft",
        organization_id=ORG,
        document=a_draft("wf_elsewhere01"),
    )
    assert bad["payload"]["valid"] is False
    assert bad["payload"]["errors"] == [
        "Tell them (notify): its workflow isn't another of the organization's; "
        "list_workflows has them"
    ]
    # Nothing was saved.
    assert all(request.method == "GET" for request in api.calls)


def an_agent_draft(model: dict[str, str], thinking_level: str = "") -> dict[str, Any]:
    return {
        "name": "Triage",
        "nodes": [
            {"id": "start", "kind": "entry"},
            {
                "id": "triage",
                "kind": "agent",
                "name": "Triage it",
                "config": {
                    "instructions": "Say how bad it is.",
                    "model": model,
                    "thinking_level": thinking_level,
                },
            },
            {"id": "done", "kind": "end", "config": {"result": "previous"}},
        ],
        "edges": [
            {"source": "start", "target": "triage"},
            {"source": "triage", "target": "done"},
        ],
    }


def test_an_agents_model_is_one_forge_offers(signing: Settings) -> None:
    toolset, _ = building(signing)

    def check(draft: dict[str, Any]) -> dict[str, Any]:
        got = call(toolset, "check_workflow_draft", organization_id=ORG, document=draft)
        return got["payload"]

    # The default, a listed model, and a model only one provider has, named alone.
    for model in (
        {"provider": "", "name": ""},
        {"provider": "anthropic", "name": "claude-opus-5"},
        {"provider": "", "name": "gpt-5.2"},
    ):
        assert check(an_agent_draft(model, "high"))["errors"] == [], model
    gone = check(an_agent_draft({"provider": "google", "name": "gemini-3.5-flash"}))
    assert gone["valid"] is False
    assert gone["errors"] == [
        "Triage it (triage): Forge doesn't offer the model google/gemini-3.5-flash; "
        "leave config.model empty for the default, or use one of: openai/gpt-5.2, "
        "anthropic/claude-opus-5"
    ]
    # A level the model doesn't offer runs at the nearest: a warning, not an error.
    nearest = check(an_agent_draft({"provider": "openai", "name": "gpt-5.2"}, "xhigh"))
    assert nearest["valid"] is True
    assert nearest["warnings"] == [
        "Triage it (triage): GPT-5.2 doesn't offer the thinking level xhigh (it "
        "offers off, minimal, low, medium, high); it runs at the nearest"
    ]
    # One that isn't a level at all is the format's error.
    wrong = check(an_agent_draft({"provider": "", "name": ""}, "lots"))
    assert wrong["valid"] is False
    assert any("thinking_level" in error for error in wrong["errors"]), wrong


def test_creating_a_workflow_sends_the_whole_document_and_answers_its_link(
    signing: Settings,
) -> None:
    toolset, api = building(signing)
    got = call(toolset, "create_workflow", organization_id=ORG, document=a_draft())
    assert got["payload"] == {
        "workflow_id": WORKFLOW,
        "name": "Review requests",
        "revision": 1,
        "steps": 3,
        "url": f"https://forge.test/organizations/{ORG}/workflows/{WORKFLOW}",
        "warnings": [],
    }
    posted = [r for r in api.calls if r.method == "POST"]
    [request] = posted
    document = json.loads(request.content)["document"]
    assert document["entry"] == "start" and document["organization_id"] == ORG
    assert document["nodes"][1]["config"]["approvers"] == "org:admin"
    assert document["edges"][1]["id"] == "review:approved->notify"


def test_a_draft_with_errors_is_never_saved(signing: Settings) -> None:
    toolset, api = building(signing)
    broken = a_draft()
    broken["edges"].append(
        {"source": "review", "source_output": "maybe", "target": "notify"}
    )
    got = call(toolset, "create_workflow", organization_id=ORG, document=broken)
    assert got["status"] == "failed"
    assert "Review it (review) has no way out 'maybe'" in got["reason"]
    assert not [r for r in api.calls if r.method in ("POST", "PUT")]


def test_saving_over_a_workflow_keeps_its_steps_places_from_its_revision(
    signing: Settings,
) -> None:
    toolset, api = building(signing)
    got = call(
        toolset,
        "save_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        document=a_draft(),
    )
    assert got["payload"]["revision"] == 8
    [request] = [r for r in api.calls if r.method == "PUT"]
    body = json.loads(request.content)
    assert body["revision"] == 7
    # The start keeps its place; the step that's gone doesn't.
    assert body["document"]["layout"] == {"start": {"x": 1, "y": 2}}


def test_making_a_workflow_waits_for_the_person_to_confirm_it(
    signing: Settings,
) -> None:
    api = Api(signing, organization_routes)
    toolset = WorkflowToolset(api.person_api)
    llm = ScriptedLlm(
        turns=[
            [
                types.Part.from_function_call(
                    name="create_workflow",
                    args={"organization_id": ORG, "document": a_draft()},
                )
            ],
            [types.Part.from_text(text="Waiting for you to confirm.")],
        ]
    )
    agent = LlmAgent(name="forge", model=llm, tools=[toolset])
    runner = InMemoryRunner(agent=agent, app_name="forge")

    async def main() -> list[Any]:
        session = await runner.session_service.create_session(
            app_name="forge", user_id="ada"
        )
        message = types.Content(
            role="user", parts=[types.Part.from_text(text="Make it")]
        )
        return [
            event
            async for event in runner.run_async(
                user_id="ada", session_id=session.id, new_message=message
            )
        ]

    events = asyncio.run(main())
    asked = [
        call.name
        for event in events
        for call in event.get_function_calls()
        if call.name == "adk_request_confirmation"
    ]
    assert asked == ["adk_request_confirmation"]
    assert api.calls == []
