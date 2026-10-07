"""
A run's steps as its run page reads them (``steps.run_steps``): documents built
and run through ADK's runner, their sessions kept in a SQLite database (ADK's
``SqliteSessionService``; for real, its ``DatabaseSessionService`` on the admin
MySQL), then read back as each step's status, output or error, and times.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google.adk import Event
from google.adk.events.event import NodeInfo
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from forge_task_adk_workflows.graph import RunFailed, RunServices, build_agent, pending_pauses
from forge_task_adk_workflows.steps import STATUSES, run_steps

from .scripted_llm import ScriptedLlm

APP = "adk_workflows"
USER = "member-1"


def node(node_id: str, kind: str, config: dict[str, Any] | None = None, name: str = "") -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": name or node_id.replace("_", " ").title(),
        "config": config or {},
        "outputs": [],
    }


def agent(
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str, str]],
    *,
    agent_id: str = "ag_steps00001",
    name: str = "Steps",
) -> dict[str, Any]:
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": name,
        "description": "",
        "organization_id": "3f6c0000-0000-4000-8000-000000000001",
        "nodes": nodes,
        "edges": [{"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t} for s, o, t in edges],
        "layout": {},
    }


START = node(
    "start",
    "start",
    {"input_schema": {"type": "object", "required": ["key"], "properties": {"key": {"type": "string"}}}},
)
ANY_START = node("start", "start", {"input_schema": {}})


def transform(node_id: str, expression: str) -> dict[str, Any]:
    return node(node_id, "transform", {"expression": expression})


def end(node_id: str, result: str = "", outcome: str = "succeeded") -> dict[str, Any]:
    return node(node_id, "end", {"outcome": outcome, "result": result})


APPROVAL = agent(
    [
        START,
        transform("shape", '{"loud": $uppercase(input.key)}'),
        node("review", "approval", {"message": "Ship {{ input.key }}?", "approvers": "org:admin", "timeout_hours": 0}),
        end("yes", "steps.review.output.approved"),
        end("no", '"not shipped"'),
    ],
    [
        ("start", "next", "shape"),
        ("shape", "next", "review"),
        ("review", "approved", "yes"),
        ("review", "rejected", "no"),
    ],
)


class Sessions:
    """Runs of documents, their sessions in a SQLite database of the test's."""

    def __init__(self, tmp_path: Path) -> None:
        self.service = SqliteSessionService(str(tmp_path / "sessions.db"))

    async def run(self, document: dict[str, Any], value: Any = None, **options: Any) -> "Ran":
        graph = build_agent(document, **options)
        runner = Runner(node=graph, session_service=self.service, app_name=APP)
        session = await self.service.create_session(app_name=APP, user_id=USER)
        ran = Ran(self, runner, session.id)
        await ran.send(types.Content(role="user", parts=[types.Part(text=json.dumps(value))]))
        return ran


class Ran:
    def __init__(self, sessions: Sessions, runner: Runner, session_id: str) -> None:
        self.sessions = sessions
        self.runner = runner
        self.session_id = session_id
        self.raised: Exception | None = None

    async def send(self, message: types.Content, invocation_id: str | None = None) -> None:
        try:
            async for _ in self.runner.run_async(
                user_id=USER, session_id=self.session_id, new_message=message, invocation_id=invocation_id
            ):
                pass
        except RunFailed as error:
            self.raised = error

    async def session(self) -> Session:
        # Read back as the admin API does: from the database.
        session = await self.sessions.service.get_session(app_name=APP, user_id=USER, session_id=self.session_id)
        assert session is not None
        return session

    async def answer(self, response: dict[str, Any]) -> None:
        [pause] = pending_pauses(await self.session())
        await self.send(pause.answer(response), invocation_id=pause.invocation_id)


def steps_of(ran: Ran, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    session = asyncio.run(ran.session())
    steps = run_steps(session, document)
    assert [step["id"] for step in steps] == [n["id"] for n in document["nodes"]]
    assert all(step["status"] in STATUSES for step in steps)
    return {step["id"]: step for step in steps}


def statuses(steps: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {step_id: step["status"] for step_id, step in steps.items()}


def test_a_paused_run_shows_what_ran_and_the_step_that_waits(tmp_path: Path) -> None:
    sessions = Sessions(tmp_path)
    ran = asyncio.run(sessions.run(APPROVAL, {"key": "k-1"}))
    steps = steps_of(ran, APPROVAL)
    assert statuses(steps) == {
        "start": "done",
        "shape": "done",
        "review": "waiting",
        "yes": "not_reached",
        "no": "not_reached",
    }
    start, shape, review, yes = steps["start"], steps["shape"], steps["review"], steps["yes"]
    assert (start["name"], start["kind"], start["output"]) == ("Start", "start", {"key": "k-1"})
    assert (shape["kind"], shape["output"], shape["error"]) == ("transform", {"loud": "K-1"}, None)
    assert shape["started_at"] == start["finished_at"] and shape["started_at"] <= shape["finished_at"]
    assert review["output"] is None and review["started_at"] and review["finished_at"] is None
    assert yes == {
        "id": "yes",
        "name": "Yes",
        "kind": "end",
        "status": "not_reached",
        "output": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "calls": [],
    }

    async def approve() -> None:
        await ran.answer({"approved": True, "decided_by": "Lead", "comment": "go"})

    asyncio.run(approve())
    steps = steps_of(ran, APPROVAL)
    assert statuses(steps) == {
        "start": "done",
        "shape": "done",
        "review": "done",
        "yes": "done",
        "no": "not_reached",
    }
    decision = steps["review"]["output"]
    assert (decision["approved"], decision["decided_by"], decision["comment"]) == (True, "Lead", "go")
    assert steps["review"]["started_at"] <= steps["review"]["finished_at"]
    assert steps["yes"]["output"] == {"outcome": "succeeded", "result": True}


def test_the_step_that_failed_the_run_says_why(tmp_path: Path) -> None:
    document = agent(
        [ANY_START, transform("ok", "1"), transform("bad", "$uppercase(1)"), end("done")],
        [("start", "next", "ok"), ("ok", "next", "bad"), ("bad", "next", "done")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    assert ran.raised is not None
    steps = steps_of(ran, document)
    assert statuses(steps) == {"start": "done", "ok": "done", "bad": "failed", "done": "not_reached"}
    error = steps["bad"]["error"]
    assert error["code"] == "RunFailed"
    assert error["message"].startswith("Bad (bad) failed: Its expression failed")
    assert steps["bad"]["started_at"] and steps["bad"]["finished_at"] is None


def test_input_the_graph_refuses_leaves_no_step_reached(tmp_path: Path) -> None:
    # ADK refuses it before the start runs, recording nothing: the run's
    # failure (the task's) says why.
    document = agent([START, end("done")], [("start", "next", "done")])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"nothing": True}))
    assert ran.raised is not None
    assert statuses(steps_of(ran, document)) == {"start": "not_reached", "done": "not_reached"}


def test_a_step_failing_in_a_loops_body_fails_the_loop_too(tmp_path: Path) -> None:
    document = agent(
        [
            ANY_START,
            node("each", "loop", {"items": "[1]", "item_name": "n", "max_iterations": 10, "concurrency": 1}),
            transform("bad", "$uppercase(n)"),
            end("done"),
        ],
        [("start", "next", "each"), ("each", "each", "bad"), ("bad", "next", "each"), ("each", "done", "done")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    steps = steps_of(ran, document)
    assert statuses(steps) == {"start": "done", "each": "failed", "bad": "failed", "done": "not_reached"}
    assert steps["each"]["error"] == steps["bad"]["error"]


def test_a_failed_end_is_failed_with_what_it_ended_with(tmp_path: Path) -> None:
    # The run fails once every way has ended, so the End hands on its
    # ending; it's still the step that failed the run.
    document = agent(
        [ANY_START, end("stop", '"No refund"', outcome="failed")],
        [("start", "next", "stop")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    steps = steps_of(ran, document)
    assert statuses(steps) == {"start": "done", "stop": "failed"}
    assert steps["stop"]["output"] == {"outcome": "failed", "result": "No refund"}
    assert steps["stop"]["error"] == {"message": "Stop (stop) failed the run: No refund", "code": "RunFailed"}
    assert steps["stop"]["finished_at"] is not None


def test_a_step_that_took_its_error_way_is_done_with_its_error(tmp_path: Path) -> None:
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    document = agent(
        [ANY_START, node("read", "http", {"url": "https://api.example.com/x"}), end("ok"), end("oops")],
        [("start", "next", "read"), ("read", "success", "ok"), ("read", "error", "oops")],
    )
    services = RunServices(
        http=httpx.AsyncClient(transport=httpx.MockTransport(gone)), allowed_hosts=("api.example.com",)
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}, services=services))
    steps = steps_of(ran, document)
    assert statuses(steps) == {"start": "done", "read": "done", "ok": "not_reached", "oops": "done"}
    assert steps["read"]["output"] is None
    assert steps["read"]["error"] == {"message": "It answered 404", "status": 404, "body": "gone"}


def test_a_loops_body_steps_show_their_last_item_and_the_loop_its_results(tmp_path: Path) -> None:
    document = agent(
        [
            ANY_START,
            node("each", "loop", {"items": "[1, 2, 3]", "item_name": "n", "max_iterations": 10, "concurrency": 1}),
            transform("double", "n * 2"),
            end("done", "steps.each.output.results"),
        ],
        [("start", "next", "each"), ("each", "each", "double"), ("double", "next", "each"), ("each", "done", "done")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    steps = steps_of(ran, document)
    assert statuses(steps) == {"start": "done", "each": "done", "double": "done", "done": "done"}
    assert steps["double"]["output"] == 6
    assert steps["each"]["output"] == {"count": 3, "results": [2, 4, 6]}
    # The loop started when its first item did.
    assert steps["each"]["started_at"] <= steps["double"]["finished_at"] <= steps["each"]["finished_at"]


def test_a_pause_inside_a_loops_body_waits_there_and_in_the_loop(tmp_path: Path) -> None:
    document = agent(
        [
            ANY_START,
            node("each", "loop", {"items": "[1]", "item_name": "n", "max_iterations": 10, "concurrency": 1}),
            node("ask", "approval", {"message": "Ok?", "approvers": "org:member", "timeout_hours": 0}),
            end("done"),
        ],
        [
            ("start", "next", "each"),
            ("each", "each", "ask"),
            ("ask", "approved", "each"),
            ("ask", "rejected", "each"),
            ("each", "done", "done"),
        ],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    assert statuses(steps_of(ran, document)) == {
        "start": "done",
        "each": "waiting",
        "ask": "waiting",
        "done": "not_reached",
    }


def test_a_saved_agents_own_steps_are_its_node(tmp_path: Path) -> None:
    inner = agent(
        [
            node("start", "start", {"input_schema": {}}),
            transform("add", "input.n + 1"),
            end("finish", "steps.add.output"),
        ],
        [("start", "next", "add"), ("add", "next", "finish")],
        agent_id="ag_inner00001",
        name="Add one",
    )
    outer = agent(
        [
            ANY_START,
            node("check", "saved", {"agent": "ag_inner00001"}, name="Check the number"),
            transform("after", "previous"),
        ],
        [("start", "next", "check"), ("check", "next", "after")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(outer, {"n": 4}, resolve={"ag_inner00001": inner}.get))
    steps = steps_of(ran, outer)
    assert statuses(steps) == {"start": "done", "check": "done", "after": "done"}
    # Its result, not the inner steps' (their IDs aren't this document's).
    assert steps["check"]["output"] == 5
    assert steps["start"]["output"] == {"n": 4}


def test_logic_steps_record_their_way_and_an_llms_answer_its_schema(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "required": ["category"],
        "properties": {"category": {"type": "string"}, "refund": {"type": "integer"}},
    }
    document = agent(
        [
            ANY_START,
            node(
                "triage",
                "llm",
                {
                    "description": "",
                    "instruction": "Triage it.",
                    "model": {"provider": "", "name": ""},
                    "thinking_level": "",
                    "output_schema": schema,
                    "include_contents": "default",
                    "disallow_transfer_to_parent": False,
                    "disallow_transfer_to_peers": False,
                    "max_output_tokens": None,
                    "sub_agents": [],
                    "mode": "single_turn",
                },
            ),
            node("big", "if", {"condition": "steps.triage.output.refund > 2"}),
            end("refund"),
            end("keep"),
        ],
        [
            ("start", "next", "triage"),
            ("triage", "next", "big"),
            ("big", "true", "refund"),
            ("big", "false", "keep"),
        ],
    )
    llm = ScriptedLlm(turns=[[types.Part(text='{"category": "BILLING", "refund": "3"}')]])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"message": "refund me"}, model=llm))
    steps = steps_of(ran, document)
    assert statuses(steps) == {
        "start": "done",
        "triage": "done",
        "big": "done",
        "refund": "done",
        "keep": "not_reached",
    }
    assert steps["triage"]["output"] == {"category": "BILLING", "refund": 3}
    assert steps["big"]["output"] == {"branch": "true"}


def test_an_llm_step_takes_as_long_as_its_model(tmp_path: Path) -> None:
    # ADK stamps the answer when it asks the model; the runner, when it comes.
    document = agent(
        [ANY_START, node("write", "llm", {"instruction": "Write it."}), end("done")],
        [("start", "next", "write"), ("write", "next", "done")],
    )
    llm = ScriptedLlm(turns=[[types.Part(text="A title")]], delay=0.3)
    steps = steps_of(asyncio.run(Sessions(tmp_path).run(document, {}, model=llm)), document)
    write = steps["write"]
    assert (write["status"], write["output"]) == ("done", "A title")
    took = datetime.fromisoformat(write["finished_at"]) - datetime.fromisoformat(write["started_at"])
    assert took.total_seconds() >= 0.3
    assert write["started_at"] == steps["start"]["finished_at"]


def test_a_step_that_hands_on_nothing_is_done_when_what_only_it_leads_to_ran(tmp_path: Path) -> None:
    document = agent(
        [
            ANY_START,
            node("which", "if", {"condition": "input.left"}),
            transform("left", "input.nothing"),
            transform("right", '"right"'),
            node("join", "merge", {"mode": "any"}),
            transform("after_left", '"after"'),
        ],
        [
            ("start", "next", "which"),
            ("which", "true", "left"),
            ("which", "false", "right"),
            ("left", "next", "after_left"),
            ("after_left", "next", "join"),
            ("right", "next", "join"),
        ],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {"left": True}))
    steps = steps_of(ran, document)
    # Left handed on nothing, so ADK recorded nothing of it; the step after it ran.
    assert statuses(steps) == {
        "start": "done",
        "which": "done",
        "left": "done",
        "right": "not_reached",
        "join": "done",
        "after_left": "done",
    }
    assert steps["left"]["output"] is None and steps["left"]["finished_at"] is None


def test_a_step_that_started_and_hasnt_finished_is_running() -> None:
    document = agent(
        [ANY_START, node("team", "sequential", {"description": "", "sub_agents": []}), end("done")],
        [("start", "next", "team"), ("team", "next", "done")],
    )
    session = Session(
        id="s1",
        app_name=APP,
        user_id=USER,
        events=[
            Event(
                invocation_id="i1",
                author="steps",
                output={"a": 1},
                node_info=NodeInfo(path="steps@1/__input__@1", output_for=["steps@1/__input__@1"]),
                timestamp=1790000000.0,
            ),
            Event(
                invocation_id="i1",
                author="first",
                content=types.Content(role="model", parts=[types.Part(text="thinking it over")]),
                node_info=NodeInfo(path="steps@1/team@1"),
                timestamp=1790000001.5,
            ),
        ],
    )
    steps = {step["id"]: step for step in run_steps(session, document)}
    assert statuses(steps) == {"start": "done", "team": "running", "done": "not_reached"}
    # It started when the start handed on to it, not at its first event.
    assert steps["team"]["started_at"] == steps["start"]["finished_at"] == "2026-09-21T14:13:20.000Z"


def test_a_step_starts_when_the_step_before_hands_on_to_it() -> None:
    # ADK records an LLM agent once it answers: its only event is its end.
    document = agent(
        [ANY_START, node("write", "llm", {"output_schema": {}}), end("done")],
        [("start", "next", "write"), ("write", "next", "done")],
    )
    session = Session(
        id="s1",
        app_name=APP,
        user_id=USER,
        events=[
            Event(
                invocation_id="i1",
                author="steps",
                output={"a": 1},
                node_info=NodeInfo(path="steps@1/__input__@1", output_for=["steps@1/__input__@1"]),
                timestamp=1790000000.0,
            ),
            Event(
                invocation_id="i1",
                author="write",
                content=types.Content(role="model", parts=[types.Part(text="A title")]),
                output="A title",
                node_info=NodeInfo(path="steps@1/write@1", output_for=["steps@1/write@1"]),
                timestamp=1790000002.25,
            ),
        ],
    )
    steps = {step["id"]: step for step in run_steps(session, document)}
    assert statuses(steps) == {"start": "done", "write": "done", "done": "not_reached"}
    assert (steps["write"]["started_at"], steps["write"]["finished_at"]) == (
        "2026-09-21T14:13:20.000Z",
        "2026-09-21T14:13:22.250Z",
    )


def test_before_its_session_nothing_is_reached() -> None:
    steps = run_steps(None, APPROVAL)
    assert [(step["id"], step["status"]) for step in steps] == [(n["id"], "not_reached") for n in APPROVAL["nodes"]]
