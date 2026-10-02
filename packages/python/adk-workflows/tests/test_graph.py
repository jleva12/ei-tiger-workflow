"""
Agents built into ADK graphs by their kinds' factories, and run through ADK's
runner with an in-memory (or SQLite) session, a fake HTTP transport, a clock
the tests move and a scripted model.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.adk import Event, Workflow
from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService, Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.workflow import START, BaseNode, FunctionNode, JoinNode
from google.genai import types

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import (
    FACTORIES,
    KINDS,
    AgentBuildError,
    AgentGraph,
    Pause,
    RunFailed,
    RunServices,
    build_agent,
    due_pauses,
    pending_pauses,
)
from forge_task_adk_workflows.graph.factories.logic import MergeAll
from forge_task_adk_workflows.graph.schemas import is_model

from .scripted_llm import ScriptedLlm

# The web console's example agent, as its builder exports it.
EXAMPLE = Path(__file__).with_name("fixtures") / "example.agent.json"
APP = "forge"
USER = "member-1"


def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- helpers


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
    agent_id: str = "ag_test000001",
    name: str = "Test agent",
) -> dict[str, Any]:
    """A forge.agent/v1 document: its nodes, and edges (source, output, target)."""
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


START_NODE = node("start", "start", {"input_schema": {}})
ISSUE_START = node(
    "start",
    "start",
    {
        "input_schema": {
            "type": "object",
            "required": ["issue"],
            "properties": {"issue": {"type": "object"}},
        }
    },
)


def transform(node_id: str, expression: str, **config: Any) -> dict[str, Any]:
    return node(node_id, "transform", {"expression": expression, **config})


def end(node_id: str, result: str = "", outcome: str = "succeeded") -> dict[str, Any]:
    return node(node_id, "end", {"outcome": outcome, "result": result})


class Clock:
    """The time, as the tests move it; sleeping moves it too."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def services(http: Handler | None = None, clock: Clock | None = None, **more: Any) -> RunServices:
    clock = clock or Clock()
    return RunServices(
        http=httpx.AsyncClient(transport=httpx.MockTransport(http or (lambda request: httpx.Response(404)))),
        clock=clock,
        sleep=clock.sleep,
        **{"allowed_hosts": ("api.example.com",), **more},
    )


def said(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def name_of(event: Event) -> str:
    return event.node_info.path.rpartition("/")[2].partition("@")[0]


@dataclass
class Run:
    """One session of a graph, through ADK's runner."""

    runner: Runner
    session_id: str
    events: list[Event] = field(default_factory=list)

    @classmethod
    async def of(
        cls,
        graph: Workflow,
        *,
        sessions: BaseSessionService | None = None,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> "Run":
        sessions = sessions or InMemorySessionService()
        runner = Runner(node=graph, session_service=sessions, app_name=APP)
        if session_id is None:
            session = await sessions.create_session(app_name=APP, user_id=USER, state=state or {})
            session_id = session.id
        return cls(runner, session_id)

    async def send(self, message: types.Content) -> list[Event]:
        events = [
            event
            async for event in self.runner.run_async(user_id=USER, session_id=self.session_id, new_message=message)
        ]
        self.events.extend(events)
        return events

    async def start(self, value: Any = None) -> list[Event]:
        return await self.send(said(json.dumps(value)))

    async def session(self) -> Session:
        session = await self.runner.session_service.get_session(app_name=APP, user_id=USER, session_id=self.session_id)
        assert session is not None
        return session

    async def pauses(self) -> list[Pause]:
        return pending_pauses(await self.session())

    async def answer(self, pause: Pause, response: dict[str, Any]) -> list[Event]:
        return await self.send(pause.answer(response))

    def outputs(self, name: str) -> list[Any]:
        return [event.output for event in self.events if name_of(event) == name and event.output is not None]

    @property
    def finished(self) -> dict[str, Any] | None:
        """The run's ``{outcome, result}``, once it's finished."""
        found = self.outputs("__finish__")
        return found[-1] if found else None

    @property
    def result(self) -> Any:
        assert self.finished is not None, "the run hasn't finished"
        return self.finished["result"]


def run[T](main: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(main())


async def ran(document: dict[str, Any], value: Any = None, **options: Any) -> Run:
    """A document built and run once, to its end or its first pause."""
    graph = build_agent(document, services=options.pop("svc", None) or services())
    session = await Run.of(graph, **options)
    await session.start(value)
    return session


def by_name(graph: Workflow) -> dict[str, BaseNode]:
    assert graph.graph is not None
    return {built.name: built for built in graph.graph.nodes}


def routes(graph: Workflow) -> dict[tuple[str, str], Any]:
    assert graph.graph is not None
    return {(edge.from_node.name, edge.to_node.name): edge.route for edge in graph.graph.edges}


def refused(document: dict[str, Any], **options: Any) -> str:
    with pytest.raises(AgentBuildError) as raised:
        build_agent(document, **options)
    return str(raised.value)


# --------------------------------------------------------------- the factories


def test_every_kind_has_a_factory() -> None:
    assert (
        set(FACTORIES)
        == KINDS
        == {
            "start",
            "llm",
            "sequential",
            "parallel",
            "loop_agent",
            "saved",
            "approval",
            "human_input",
            "http",
            "transform",
            "delay",
            "if",
            "switch",
            "match",
            "loop",
            "merge",
            "end",
        }
    )


def test_each_forge_kind_is_the_adk_node_its_factory_makes() -> None:
    document = agent(
        [
            START_NODE,
            node("get", "http", {"url": "https://api.example.com/x"}),
            node("ask", "approval", {"message": "Ok?", "timeout_hours": 24}),
            node("wait", "delay", {"amount": 1, "unit": "days"}),
            node("check", "if", {"condition": "true"}),
            node("pick", "switch", {"value": "1", "cases": [{"id": "one", "value": "1"}]}),
            node("rule", "match", {"arms": [{"id": "a", "condition": "true"}]}),
            node("each", "loop", {"items": "[1, 2]"}),
            transform("shape", "item"),
            node("both", "merge", {"mode": "all"}),
            node("first", "merge", {"mode": "any"}),
            node("person", "human_input", {"message": "Why?", "response_schema": {}}),
            end("done"),
        ],
        [
            ("start", "next", "get"),
            ("start", "next", "check"),
            ("get", "success", "ask"),
            ("get", "error", "first"),
            ("ask", "approved", "wait"),
            ("ask", "rejected", "wait"),
            ("wait", "next", "pick"),
            ("pick", "one", "rule"),
            ("pick", "default", "rule"),
            ("rule", "a", "each"),
            ("each", "each", "shape"),
            ("shape", "next", "each"),
            ("each", "done", "both"),
            ("check", "true", "both"),
            ("check", "false", "first"),
            ("both", "next", "first"),
            ("first", "next", "person"),
            ("person", "next", "done"),
        ],
    )
    graph = build_agent(document)
    assert isinstance(graph, AgentGraph)
    nodes = by_name(graph)
    for name in ("get", "check", "pick", "rule", "done", "__input__", "__finish__"):
        assert isinstance(nodes[name], FunctionNode), name
        assert not nodes[name].rerun_on_resume
    # Pausing steps and the loop, which runs its body per item, run again when
    # the run resumes.
    for name in ("ask", "wait", "each", "person"):
        assert isinstance(nodes[name], FunctionNode)
        assert nodes[name].rerun_on_resume, name
    assert isinstance(nodes["both"], MergeAll)
    assert isinstance(nodes["both"], JoinNode)
    assert isinstance(nodes["first"], FunctionNode)
    # The loop's body is cut out of the graph.
    assert "shape" not in nodes
    assert routes(graph) == {
        (START.name, "__input__"): None,
        ("__input__", "get"): None,
        ("__input__", "check"): None,
        ("get", "ask"): "success",
        # Two ways to one node: one edge, with both routes.
        ("ask", "wait"): ["approved", "rejected"],
        ("wait", "pick"): None,
        ("pick", "rule"): ["one", "default"],
        ("rule", "each"): "a",
        ("each", "both"): "done",
        ("check", "both"): "true",
        # A Merge "any"'s ways in are tagged with the step they come from.
        ("get", "first__via__get"): "error",
        ("first__via__get", "first"): None,
        ("check", "first__via__check"): "false",
        ("first__via__check", "first"): None,
        ("both", "first__via__both"): None,
        ("first__via__both", "first"): None,
        ("first", "person"): "next",
        ("person", "done"): None,
        ("done", "__finish__"): None,
    }


def test_agents_are_adks_with_pydantic_schemas_and_forge_instructions() -> None:
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    team = {
        "description": "",
        "sub_agents": [
            {"id": "first", "kind": "llm", "name": "First", "config": sub_config()},
            {"id": "second", "kind": "llm", "name": "Second", "config": sub_config()},
        ],
    }
    graph = build_agent(
        agent(
            [
                START_NODE,
                node(
                    "answer",
                    "llm",
                    llm_config(output_schema=schema, max_output_tokens=9),
                ),
                node("team", "sequential", team),
            ],
            [("start", "next", "answer"), ("answer", "next", "team")],
        )
    )
    built = by_name(graph)["answer"]
    assert isinstance(built, LlmAgent)
    assert is_model(built.output_schema)
    assert built.generate_content_config == types.GenerateContentConfig(max_output_tokens=9)
    # Its instruction is Forge's template, rendered for each call; its answer
    # is its step's output, not a state key.
    assert callable(built.instruction)
    assert built.output_key is None
    # A sub-agent's answer is kept in the state under its ID.
    sub_agents = by_name(graph)["team"].sub_agents  # type: ignore[attr-defined]
    assert [agent.output_key for agent in sub_agents] == ["first", "second"]


@pytest.mark.parametrize(
    ("sub_id", "message"),
    [
        ("input", "its ID can't be input: the run's input is kept there."),
        ("2nd", "its ID, '2nd', can't be a state key its answer is kept under."),
        ("first", "its ID, first, is Node 'Team', sub-agent 'First''s too."),
    ],
)
def test_sub_agent_ids_are_state_keys_of_their_own(sub_id: str, message: str) -> None:
    team = {
        "description": "",
        "sub_agents": [
            {"id": "first", "kind": "llm", "name": "First", "config": sub_config()},
            {"id": sub_id, "kind": "llm", "name": "Second", "config": sub_config()},
        ],
    }
    document = agent([START_NODE, node("team", "sequential", team)], [("start", "next", "team")])
    assert refused(document) == f"Node 'Team', sub-agent 'Second': {message}"


def test_the_example_is_one_graph_of_adk_nodes() -> None:
    graph = build_agent(example())
    assert graph.name == "support_desk"
    # The start's input schema is the graph's, as a Pydantic model.
    assert is_model(graph.input_schema)
    assert list(graph.input_schema.model_fields) == ["message", "customer_id"]
    nodes = by_name(graph)
    assert set(nodes) == {
        START.name,
        "__input__",
        "look_up_the_customer",
        "triage",
        "sentiment",
        "read_the_message",
        "route_by_category",
        "bug_team",
        "check_the_reply",
        "send_it",
        "email_each_contact",
        "sent",
        "held_back",
        "billing",
        "refund_owed",
        "approve_the_refund",
        "polish_the_reply",
        "replied",
        "refund_declined",
        "research",
        "sum_up",
        "answered",
        "unknown_customer",
        "__finish__",
    }
    triage = nodes["triage"]
    assert isinstance(triage, LlmAgent)
    assert triage.mode == "single_turn"
    assert triage.output_key is None
    assert is_model(triage.output_schema)
    assert list(triage.output_schema.model_fields) == ["category", "summary"]
    bug_team = nodes["bug_team"]
    assert isinstance(bug_team, SequentialAgent)
    assert [a.name for a in bug_team.sub_agents] == ["reproducer", "fix_drafter"]
    assert [a.output_key for a in bug_team.sub_agents] == ["reproduce", "draft_fix"]
    research = nodes["research"]
    assert isinstance(research, ParallelAgent)
    assert [a.name for a in research.sub_agents] == [
        "docs_researcher",
        "forum_researcher",
    ]
    polish = nodes["polish_the_reply"]
    assert isinstance(polish, LoopAgent)
    assert polish.max_iterations == 3
    assert isinstance(nodes["read_the_message"], MergeAll)
    for name in ("look_up_the_customer", "route_by_category", "sum_up", "sent"):
        assert isinstance(nodes[name], FunctionNode)
    for name in ("check_the_reply", "approve_the_refund", "email_each_contact"):
        assert nodes[name].rerun_on_resume
    assert routes(graph) == {
        (START.name, "__input__"): None,
        ("__input__", "look_up_the_customer"): None,
        ("look_up_the_customer", "triage"): "success",
        ("look_up_the_customer", "sentiment"): "success",
        ("look_up_the_customer", "unknown_customer"): "error",
        ("triage", "read_the_message"): None,
        ("sentiment", "read_the_message"): None,
        ("read_the_message", "route_by_category"): None,
        ("route_by_category", "bug_team"): "bug",
        ("route_by_category", "billing"): "billing",
        ("route_by_category", "research"): "default",
        ("bug_team", "check_the_reply"): None,
        ("check_the_reply", "send_it"): None,
        ("send_it", "email_each_contact"): "true",
        ("send_it", "held_back"): "false",
        ("email_each_contact", "sent"): "done",
        ("billing", "refund_owed"): None,
        ("refund_owed", "approve_the_refund"): "true",
        ("refund_owed", "polish_the_reply"): "false",
        ("approve_the_refund", "polish_the_reply"): "approved",
        ("approve_the_refund", "refund_declined"): "rejected",
        ("polish_the_reply", "replied"): None,
        ("research", "sum_up"): None,
        ("sum_up", "answered"): None,
        # Every ending leads to the one hidden finish.
        ("sent", "__finish__"): None,
        ("held_back", "__finish__"): None,
        ("replied", "__finish__"): None,
        ("refund_declined", "__finish__"): None,
        ("answered", "__finish__"): None,
        ("unknown_customer", "__finish__"): None,
    }


def test_a_merge_all_over_ways_that_exclude_each_other_is_refused() -> None:
    document = agent(
        [
            START_NODE,
            node("check", "if", {"condition": "input.go"}),
            transform("yes", '"Y"'),
            transform("no", '"N"'),
            node("join", "merge", {"mode": "all"}),
        ],
        [
            ("start", "next", "check"),
            ("check", "true", "yes"),
            ("check", "false", "no"),
            ("yes", "next", "join"),
            ("no", "next", "join"),
        ],
    )
    assert refused(document) == (
        "Node 'Join': it waits for all its ways in, but 'Yes' and 'No' come from "
        "different ways out of 'Check' (true and false), so it would never go on; "
        'make it go on at the first ("any").'
    )
    document["nodes"][-1]["config"]["mode"] = "any"
    build_agent(document)


def test_only_a_loops_each_item_way_leads_into_its_body() -> None:
    document = agent(
        [
            START_NODE,
            node("each", "loop", {"items": "[1]"}),
            transform("shape", "item"),
        ],
        [
            ("start", "next", "each"),
            ("start", "next", "shape"),
            ("each", "each", "shape"),
            ("shape", "next", "each"),
        ],
    )
    assert refused(document) == (
        "Node 'Shape': it's in the body of loop 'Each', and node 'start', outside it, "
        "leads into it; only the loop's Each item way may."
    )


def test_settings_a_kind_cant_run_with_are_refused() -> None:
    document = agent(
        [START_NODE, node("get", "http", {"method": "TELEPORT"})],
        [("start", "next", "get")],
    )
    assert refused(document).startswith("Node 'Get': its settings aren't a http's: method: Input should be")


# ------------------------------------------------------------- running: parity


def test_a_straight_run_hands_on_what_each_step_makes() -> None:
    document = agent(
        [
            ISSUE_START,
            transform(
                "shape",
                '{"key": input.issue.key, "loud": $uppercase(input.issue.key)}',
            ),
            transform(
                "look",
                '{"previous": previous, "shape": steps.shape.output, "start": steps.start.output, "input": input}',
            ),
            end("done", 'steps.shape.output.loud & "!"'),
        ],
        [
            ("start", "next", "shape"),
            ("shape", "next", "look"),
            ("look", "next", "done"),
        ],
    )
    session = run(lambda: ran(document, {"issue": {"key": "xmen-12"}}))
    assert session.finished == {"outcome": "succeeded", "result": "XMEN-12!"}
    shaped = {"key": "xmen-12", "loud": "XMEN-12"}
    assert session.outputs("look") == [
        {
            "previous": shaped,
            "shape": shaped,
            # The start's output is the run's input.
            "start": {"issue": {"key": "xmen-12"}},
            "input": {"issue": {"key": "xmen-12"}},
        }
    ]
    # The run's input is kept in the session's state, for LLM agents' {input}.
    state = run(session.session).state
    assert state["input"] == {"issue": {"key": "xmen-12"}}


def test_a_run_without_an_end_finishes_with_what_its_last_step_handed_on() -> None:
    document = agent([START_NODE, transform("shape", "input.n * 2")], [("start", "next", "shape")])
    assert run(lambda: ran(document, {"n": 4})).finished == {
        "outcome": "succeeded",
        "result": 8,
    }


def test_an_input_that_doesnt_fit_the_start_fails_the_run() -> None:
    document = agent([ISSUE_START, end("done")], [("start", "next", "done")])
    with pytest.raises(RunFailed, match="The run's input doesn't fit its start: issue"):
        run(lambda: ran(document, {"nothing": True}))
    with pytest.raises(RunFailed, match="doesn't fit its start"):
        run(lambda: ran(document, "not even JSON"))


def test_a_message_is_held_to_the_graphs_pydantic_input_schema() -> None:
    # ADK takes the run's message against the graph's input schema, coerced.
    start = node(
        "start",
        "start",
        {"input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    )
    document = agent([start, end("done", "input.n + 1")], [("start", "next", "done")])
    assert run(lambda: ran(document, {"n": "41"})).result == 42


def test_logic_steps_route_and_hand_on_what_came_into_them() -> None:
    document = agent(
        [
            ISSUE_START,
            transform("classify", '{"kind": "bug", "severity": "high"}'),
            node(
                "route",
                "match",
                {
                    "arms": [
                        {
                            "id": "urgent",
                            "label": "Urgent",
                            "condition": 'steps.classify.output.kind = "bug" and '
                            'steps.classify.output.severity = "high"',
                        },
                        {
                            "id": "bug",
                            "label": "Bug",
                            "condition": 'steps.classify.output.kind = "bug"',
                        },
                    ]
                },
            ),
            node("check", "if", {"condition": 'previous.severity = "high"'}),
            node(
                "switch",
                "switch",
                {"value": "previous.kind", "cases": [{"id": "is_bug", "value": "bug"}]},
            ),
            end(
                "urgent_end",
                '{"said": "urgent " & previous.kind, "ways": [steps.route.output, '
                "steps.check.output, steps.switch.output]}",
            ),
            end("other_end", '"other"'),
        ],
        [
            ("start", "next", "classify"),
            ("classify", "next", "route"),
            ("route", "urgent", "check"),
            ("route", "bug", "other_end"),
            ("check", "true", "switch"),
            ("switch", "is_bug", "urgent_end"),
        ],
    )
    session = run(lambda: ran(document, {"issue": {}}))
    # previous passes through the match, the if and the switch: still
    # classify's output. Each records the way it took.
    assert session.result == {
        "said": "urgent bug",
        "ways": [{"branch": "urgent"}, {"branch": "true"}, {"branch": "is_bug"}],
    }
    assert [event.actions.route for event in session.events if event.actions.route] == [
        "urgent",
        "true",
        "is_bug",
    ]


@pytest.mark.parametrize(
    ("kind", "count", "expected"),
    [
        ("if", 10, "true"),
        ("if", 9, "false"),
        ("match", 10, "true"),
        ("match", 0, "false"),
    ],
)
def test_conditions_read_steps_and_previous(kind: str, count: int, expected: str) -> None:
    condition = '{{ steps.measure.output.count }} > 9 and {{ previous.priority }} = "high"'
    config = {"condition": condition} if kind == "if" else {"arms": [{"id": "true", "condition": condition}]}
    document = agent(
        [
            START_NODE,
            transform("measure", '{"count": input.count, "priority": "high"}'),
            node("check", kind, config),
            end("yes", '"true"'),
            end("no", '"false"'),
        ],
        [
            ("start", "next", "measure"),
            ("measure", "next", "check"),
            ("check", "true", "yes"),
            ("check", "false" if kind == "if" else "otherwise", "no"),
        ],
    )
    assert run(lambda: ran(document, {"count": count})).result == expected


def test_a_way_taken_with_nothing_handed_on_still_leads_on() -> None:
    # An If after an Error way: it's handed nothing, and hands on nothing, but
    # its event's route alone takes its way.
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    document = agent(
        [
            START_NODE,
            node("read", "http", {"url": "https://api.example.com/issues/1"}),
            node("check", "if", {"condition": "steps.read.error.status = 404"}),
            end(
                "nothing",
                '{"previous": previous, "check": steps.check.output, "read": steps.read}',
            ),
        ],
        [
            ("start", "next", "read"),
            ("read", "error", "check"),
            ("check", "true", "nothing"),
        ],
    )
    session = run(lambda: ran(document, svc=services(gone)))
    assert session.result == {
        "previous": None,
        "check": {"branch": "true"},
        # An Error way hands on nothing, and keeps the step's error.
        "read": {
            "output": None,
            "error": {"message": "It answered 404", "status": 404, "body": "gone"},
        },
    }
    [event] = [e for e in session.events if name_of(e) == "check"]
    assert event.output is None
    assert event.actions.route == "true"


# ------------------------------------------------------------------ HTTP


def test_an_http_request_sends_its_body_and_hands_on_the_response() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 9}, headers={"X-Trace": "abc"})

    document = agent(
        [
            START_NODE,
            node(
                "post",
                "http",
                {
                    "method": "POST",
                    "url": "https://api.example.com/issues/{{ input.key }}",
                    "headers": [
                        {
                            "id": "h",
                            "name": "Authorization",
                            "value": "Token {{ input.token }}",
                        }
                    ],
                    "body": '{"text": "Fix " & input.key}',
                },
            ),
            end("done", "steps.post.output"),
        ],
        [("start", "next", "post"), ("post", "success", "done")],
    )
    session = run(lambda: ran(document, {"key": "XMEN-12", "token": "t0k"}, svc=services(api)))
    assert str(seen[0].url) == "https://api.example.com/issues/XMEN-12"
    assert seen[0].headers["authorization"] == "Token t0k"
    assert json.loads(seen[0].content) == {"text": "Fix XMEN-12"}
    response = session.result
    assert (response["status"], response["body"], response["headers"]["x-trace"]) == (
        201,
        {"id": 9},
        "abc",
    )
    assert [e.actions.route for e in session.events if name_of(e) == "post"] == ["success"]


def test_an_http_request_retries_then_takes_its_error_way() -> None:
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="busy")

    clock = Clock()
    document = agent(
        [
            START_NODE,
            node("get", "http", {"url": "https://api.example.com/x", "retries": 2}),
            end("failed", "steps.get.error"),
        ],
        [("start", "next", "get"), ("get", "error", "failed")],
    )
    session = run(lambda: ran(document, svc=services(api, clock)))
    assert len(calls) == 3
    assert clock.slept == [2, 4]
    assert session.result == {
        "message": "It answered 503",
        "status": 503,
        "body": "busy",
    }


def test_an_http_response_that_doesnt_fit_its_output_schema_takes_the_error_way() -> None:
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": request.url.params.get("id")})

    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    document = agent(
        [
            START_NODE,
            node(
                "get",
                "http",
                {
                    "url": "https://api.example.com/x?id={{ input }}",
                    "retries": 2,
                    "output_schema": schema,
                },
            ),
            end("ok", "steps.get.output.body"),
            end("failed", "steps.get.error"),
        ],
        [
            ("start", "next", "get"),
            ("get", "success", "ok"),
            ("get", "error", "failed"),
        ],
    )
    # A body that fits is handed on as its schema has it.
    assert run(lambda: ran(document, "7", svc=services(api))).result == {"id": 7}
    session = run(lambda: ran(document, "seven", svc=services(api)))
    # It isn't retried: the same answer wouldn't fit either.
    assert len(calls) == 2
    error = session.result
    assert error["status"] == 200
    assert error["message"].startswith(
        "Its response doesn't fit its declared output: id: Input should be a valid integer"
    )
    assert error["body"] == {"id": "seven"}


def test_http_requests_never_reach_a_private_network() -> None:
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    document = agent(
        [
            START_NODE,
            node("get", "http", {"url": "http://{{ input }}/latest/meta-data"}),
            end("failed", "steps.get.error.message"),
        ],
        [("start", "next", "get"), ("get", "error", "failed")],
    )
    for host in ("169.254.169.254", "127.0.0.1", "10.0.0.5"):
        session = run(lambda host=host: ran(document, host, svc=services(api)))
        assert "is on a private network" in session.result
    assert calls == []
    # A deployment can allow it.
    session = run(lambda: ran(document, "10.0.0.5", svc=services(api, allow_private=True)))
    assert len(calls) == 1


def test_a_failing_step_takes_its_error_way_or_fails_the_run() -> None:
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    read = node("read", "http", {"url": "https://api.example.com/issues/1"})
    with_way = agent(
        [START_NODE, read, end("failed", "steps.read.error")],
        [("start", "next", "read"), ("read", "error", "failed")],
    )
    assert run(lambda: ran(with_way, svc=services(gone))).result == {
        "message": "It answered 404",
        "status": 404,
        "body": "gone",
    }
    without = agent(
        [START_NODE, read, end("done")],
        [("start", "next", "read"), ("read", "success", "done")],
    )
    with pytest.raises(RunFailed, match=r"Read \(read\) failed: It answered 404"):
        run(lambda: ran(without, svc=services(gone)))


def test_an_expression_that_fails_says_where() -> None:
    document = agent([START_NODE, transform("bad", "$uppercase(1)")], [("start", "next", "bad")])
    with pytest.raises(RunFailed, match=r"Bad \(bad\) failed: Its expression failed"):
        run(lambda: ran(document))


def test_a_transform_is_held_to_its_output_schema() -> None:
    schema = {
        "type": "object",
        "required": ["n"],
        "properties": {"n": {"type": "integer"}},
        "additionalProperties": False,
    }
    document = agent(
        [START_NODE, transform("shape", "input", output_schema=schema), end("done")],
        [("start", "next", "shape"), ("shape", "next", "done")],
    )
    assert run(lambda: ran(document, {"n": "3"})).result == {"n": 3}
    with pytest.raises(
        RunFailed,
        match=r"Shape \(shape\) failed: What it made doesn't fit its declared output: "
        r"extra: Extra inputs",
    ):
        run(lambda: ran(document, {"n": 3, "extra": True}))


def test_an_end_that_fails_fails_the_run_with_its_result() -> None:
    document = agent(
        [START_NODE, end("stop", '"No customer on file: " & input.id', "failed")],
        [("start", "next", "stop")],
    )
    with pytest.raises(RunFailed) as raised:
        run(lambda: ran(document, {"id": "c9"}))
    assert raised.value.message == "Stop (stop) failed the run: No customer on file: c9"
    assert raised.value.result == "No customer on file: c9"
    assert raised.value.step == "stop"


# --------------------------------------------------------------- approvals


def approval_agent(timeout_hours: int = 24) -> dict[str, Any]:
    return agent(
        [
            ISSUE_START,
            node(
                "move",
                "http",
                {
                    "method": "POST",
                    "url": "https://api.example.com/issues/{{ input.issue.key }}/moves",
                    "body": '{"to": "review"}',
                },
            ),
            node(
                "review",
                "approval",
                {
                    "message": "Ship {{ input.issue.key }}?",
                    "approvers": "org:admin",
                    "timeout_hours": timeout_hours,
                },
            ),
            end("yes", "steps.review.output"),
            end("no", "steps.review.output"),
        ],
        [
            ("start", "next", "move"),
            ("move", "success", "review"),
            ("review", "approved", "yes"),
            ("review", "rejected", "no"),
        ],
    )


@dataclass
class Approving:
    """An approval agent run to its pause, with its HTTP calls and clock."""

    session: Run
    clock: Clock
    sent: list[httpx.Request]
    pause: Pause


async def approving(timeout_hours: int = 24) -> Approving:
    sent: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    clock = Clock()
    session = await ran(
        approval_agent(timeout_hours),
        {"issue": {"key": "XMEN-12"}},
        svc=services(api, clock),
    )
    [pause] = await session.pauses()
    return Approving(session, clock, sent, pause)


def test_an_approval_pauses_and_the_decision_resumes_it_without_repeating_a_step() -> None:
    async def main() -> Approving:
        approval = await approving()
        assert approval.session.finished is None
        approval.clock.advance(hours=3)
        await approval.session.answer(
            approval.pause,
            {"approved": True, "decided_by": "Lead", "comment": "Looks right"},
        )
        return approval

    approval = run(main)
    pause = approval.pause
    assert pause.kind == "approval"
    assert pause.message == "Ship XMEN-12?"
    assert pause.interrupt_id == "review@test_agent@1/review@1"
    assert pause.payload == {
        "kind": "approval",
        "step": "review",
        "approvers": "org:admin",
        "expires_at": "2026-09-27T12:00:00+00:00",
    }
    assert pause.due == datetime(2026, 9, 27, 12, tzinfo=UTC)
    assert approval.session.finished == {
        "outcome": "succeeded",
        "result": {
            "approved": True,
            "decided_by": "Lead",
            "comment": "Looks right",
            "decided_at": "2026-09-26T15:00:00+00:00",
        },
    }
    # The request went out once, not again when the run carried on.
    assert [str(request.url) for request in approval.sent] == ["https://api.example.com/issues/XMEN-12/moves"]
    assert len(approval.session.outputs("move")) == 1
    assert [name_of(e) for e in approval.session.events if name_of(e) == "no"] == []


def test_a_rejection_takes_the_rejected_way() -> None:
    async def main() -> Run:
        approval = await approving()
        await approval.session.answer(
            approval.pause,
            {"approved": False, "decided_by": "Lead", "comment": "Not yet"},
        )
        return approval.session

    session = run(main)
    assert session.outputs("no") == [
        {
            "outcome": "succeeded",
            "result": {
                "approved": False,
                "decided_by": "Lead",
                "comment": "Not yet",
                "decided_at": "2026-09-26T12:00:00+00:00",
            },
        }
    ]
    assert session.outputs("yes") == []


def test_a_decision_after_the_time_limit_is_a_rejection() -> None:
    async def main() -> Run:
        approval = await approving()
        approval.clock.advance(hours=25)
        await approval.session.answer(approval.pause, {"approved": True, "decided_by": "Lead"})
        return approval.session

    assert run(main).result == {
        "approved": False,
        "decided_by": "Forge",
        "comment": "Lead decided after the 24 hours it had",
        "decided_at": "2026-09-27T13:00:00+00:00",
    }


def test_the_timer_rejects_an_approval_nobody_decided_in_time() -> None:
    async def main() -> tuple[list[Pause], list[Pause], Run]:
        approval = await approving()
        approval.clock.advance(hours=23, minutes=59)
        early = due_pauses(await approval.session.session(), approval.clock())
        approval.clock.advance(minutes=1)
        due = due_pauses(await approval.session.session(), approval.clock())
        for pause in due:
            await approval.session.answer(pause, pause.timer_answer())
        return early, due, approval.session

    early, due, session = run(main)
    assert early == []
    assert [pause.kind for pause in due] == ["approval"]
    assert session.result == {
        "approved": False,
        "decided_by": "Forge",
        "comment": "No decision within 24 hours",
        "decided_at": "2026-09-27T12:00:00+00:00",
    }
    assert run(session.pauses) == []


def test_an_approval_without_a_time_limit_waits_until_someone_decides() -> None:
    async def main() -> tuple[Approving, list[Pause]]:
        approval = await approving(timeout_hours=0)
        approval.clock.advance(days=30)
        return approval, due_pauses(await approval.session.session(), approval.clock())

    approval, due = run(main)
    assert approval.pause.payload["expires_at"] is None
    assert approval.pause.due is None
    assert due == []


# ------------------------------------------------------------------ delays


def delay_agent(amount: float, unit: str) -> dict[str, Any]:
    return agent(
        [
            START_NODE,
            node("wait", "delay", {"amount": amount, "unit": unit}),
            end("done", "steps.wait.output"),
        ],
        [("start", "next", "wait"), ("wait", "next", "done")],
    )


def test_a_short_delay_sleeps_in_place() -> None:
    clock = Clock()
    session = run(lambda: ran(delay_agent(10, "seconds"), svc=services(clock=clock)))
    assert clock.slept == [10]
    assert session.result == {"resumed_at": "2026-09-26T12:00:10+00:00"}


def test_a_long_delay_pauses_and_the_timer_wakes_it() -> None:
    clock = Clock()

    async def main() -> tuple[Pause, list[Pause], list[Pause], Run]:
        session = await ran(delay_agent(2, "hours"), svc=services(clock=clock))
        [pause] = await session.pauses()
        clock.advance(hours=1)
        early = due_pauses(await session.session(), clock())
        clock.advance(hours=1)
        due = due_pauses(await session.session(), clock())
        await session.answer(due[0], due[0].timer_answer())
        return pause, early, due, session

    pause, early, due, session = run(main)
    assert pause.kind == "delay"
    assert pause.payload == {
        "kind": "delay",
        "step": "wait",
        "until": "2026-09-26T14:00:00+00:00",
    }
    assert early == []
    assert due == [pause]
    assert clock.slept == []
    assert session.result == {"resumed_at": "2026-09-26T14:00:00+00:00"}


def test_a_delay_woken_early_waits_out_the_rest() -> None:
    clock = Clock()

    async def main() -> Run:
        session = await ran(delay_agent(2, "hours"), svc=services(clock=clock))
        [pause] = await session.pauses()
        # Nearly there: it sleeps the rest in place.
        clock.advance(hours=1, minutes=59, seconds=50)
        await session.answer(pause, pause.timer_answer())
        return session

    session = run(main)
    assert clock.slept == [10.0]
    assert session.result == {"resumed_at": "2026-09-26T14:00:00+00:00"}


def test_a_delay_woken_long_before_its_time_pauses_again() -> None:
    clock = Clock()

    async def main() -> tuple[Pause, list[Pause], Run]:
        session = await ran(delay_agent(2, "hours"), svc=services(clock=clock))
        [pause] = await session.pauses()
        await session.answer(pause, pause.timer_answer())
        again = await session.pauses()
        clock.advance(hours=2)
        await session.answer(again[0], again[0].timer_answer())
        return pause, again, session

    pause, again, session = run(main)
    assert [p.interrupt_id for p in again] == [f"{pause.interrupt_id}#2"]
    assert again[0].payload["until"] == pause.payload["until"]
    assert session.result == {"resumed_at": "2026-09-26T14:00:00+00:00"}


# ------------------------------------------------------------------- loops


def loop_agent(items: str, body: list[dict[str, Any]], **config: Any) -> dict[str, Any]:
    """Start, a loop over ``items`` whose body is ``body`` in a line (its last
    step going back to the loop), and an End with the loop's output."""
    ids = [step["id"] for step in body]
    return agent(
        [
            START_NODE,
            node(
                "each",
                "loop",
                {"items": items, "item_name": "label", "max_iterations": 10, **config},
            ),
            *body,
            end("done", "steps.each.output"),
        ],
        [
            ("start", "next", "each"),
            ("each", "each", ids[0]),
            *[(a, "success" if a == "call" else "next", b) for a, b in zip(ids, ids[1:], strict=False)],
            (ids[-1], "approved" if ids[-1] == "ask" else "next", "each"),
            ("each", "done", "done"),
        ],
    )


def test_a_loop_runs_its_body_per_item_several_at_once() -> None:
    document = loop_agent(
        "input.labels",
        [transform("shout", '$uppercase(label) & "#" & $string(index)')],
        concurrency=3,
    )
    session = run(lambda: ran(document, {"labels": ["a", "b", "c", "d"]}))
    assert session.result == {"count": 4, "results": ["A#0", "B#1", "C#2", "D#3"]}
    # Each item's body is a graph of its own, run by the loop's node.
    paths = [e.node_info.path for e in session.events if name_of(e) == "shout"]
    assert paths == [f"test_agent@1/each@1/each__each@item_{i}/shout@1" for i in range(4)]


def test_a_loops_items_run_up_to_its_concurrency_at_once() -> None:
    running: list[int] = []
    most = [0]

    async def api(request: httpx.Request) -> httpx.Response:
        running.append(1)
        most[0] = max(most[0], len(running))
        await asyncio.sleep(0.01)
        running.pop()
        return httpx.Response(200, json={"n": int(request.url.params["n"])})

    body = [
        node("call", "http", {"url": "https://api.example.com/x?n={{ label }}"}),
        transform("pick", "previous.body.n * 10"),
    ]
    document = loop_agent("[1..5]", body, concurrency=3)
    session = run(lambda: ran(document, svc=services(api)))
    assert session.result == {"count": 5, "results": [10, 20, 30, 40, 50]}
    assert most[0] == 3
    document = loop_agent("[1..5]", body, concurrency=1)
    most[0] = 0
    run(lambda: ran(document, svc=services(api)))
    assert most[0] == 1


def test_a_loop_takes_nothing_as_no_items_and_one_value_as_one() -> None:
    body = [transform("shout", "$uppercase(label)")]
    assert run(lambda: ran(loop_agent("input.none", body), {})).result == {
        "count": 0,
        "results": [],
    }
    assert run(lambda: ran(loop_agent("input.one", body), {"one": "x"})).result == {
        "count": 1,
        "results": ["X"],
    }


def test_a_loop_over_too_many_items_fails() -> None:
    document = loop_agent("[1..5]", [transform("x", "1")], max_iterations=3)
    with pytest.raises(
        RunFailed,
        match=r"Each \(each\) has 5 items to go through; the most it may is 3",
    ):
        run(lambda: ran(document))


def test_a_step_failing_in_a_loops_body_fails_the_run() -> None:
    document = loop_agent("[1, 2, 3]", [transform("bad", "$uppercase(label)")], concurrency=2)
    with pytest.raises(RunFailed, match=r"Bad \(bad\) failed: Its expression failed"):
        run(lambda: ran(document))


def test_a_pause_inside_a_loops_body_resumes_one_item_at_a_time() -> None:
    document = loop_agent(
        "input.labels",
        [
            transform("shape", '{"label": label, "index": index}'),
            node("ask", "approval", {"message": "Ship {{ label }}?", "timeout_hours": 0}),
        ],
        concurrency=3,
    )

    async def main() -> tuple[list[list[str]], Run]:
        session = await ran(document, {"labels": ["a", "b"]})
        asked: list[list[str]] = []
        while pauses := await session.pauses():
            asked.append([pause.message for pause in pauses])
            await session.answer(pauses[0], {"approved": True, "decided_by": "Lead"})
        return asked, session

    asked, session = run(main)
    # One item at a time, though it may run three at once: its body pauses.
    assert asked == [["Ship a?"], ["Ship b?"]]
    assert [result["decided_by"] for result in session.result["results"]] == [
        "Lead",
        "Lead",
    ]
    # Each item's first step ran once, not again when the run resumed.
    assert session.outputs("shape") == [
        {"label": "a", "index": 0},
        {"label": "b", "index": 1},
    ]


def test_steps_in_a_loops_body_read_the_items_and_the_graph_around_it() -> None:
    document = agent(
        [
            START_NODE,
            transform("before", '"outside"'),
            node("outer", "loop", {"items": "[1, 2]", "item_name": "row"}),
            node("inner", "loop", {"items": '["x", "y"]', "item_name": "cell"}),
            transform(
                "cellwise",
                '{"row": row, "cell": cell, "index": index, "before": steps.before.output, "input": input}',
            ),
            end("done", "steps.outer.output.results"),
        ],
        [
            ("start", "next", "before"),
            ("before", "next", "outer"),
            ("outer", "each", "inner"),
            ("inner", "each", "cellwise"),
            ("cellwise", "next", "inner"),
            ("inner", "done", "outer"),
            ("outer", "done", "done"),
        ],
    )
    result = run(lambda: ran(document, {"n": 1})).result
    assert result == [
        {
            "count": 2,
            "results": [
                {
                    "row": row,
                    "cell": cell,
                    "index": index,
                    "before": "outside",
                    "input": {"n": 1},
                }
                for index, cell in enumerate(["x", "y"])
            ],
        }
        for row in (1, 2)
    ]


# ------------------------------------------------------------------ merges


def merge_agent(mode: str) -> dict[str, Any]:
    return agent(
        [
            START_NODE,
            transform("a", '"A"'),
            transform("b", '"B"'),
            node("join", "merge", {"mode": mode}),
            transform("after", "previous"),
        ],
        [
            ("start", "next", "a"),
            ("start", "next", "b"),
            ("a", "next", "join"),
            ("b", "next", "join"),
            ("join", "next", "after"),
        ],
    )


def test_merge_all_waits_for_every_way_and_any_goes_on_at_the_first() -> None:
    all_of = run(lambda: ran(merge_agent("all")))
    assert all_of.result == {"a": "A", "b": "B"}
    any_of = run(lambda: ran(merge_agent("any")))
    assert any_of.result == {"a": "A"}
    # The later way ends at the merge: what comes after runs once.
    assert any_of.outputs("after") == [{"a": "A"}]
    assert any_of.outputs("join") == [{"a": "A"}]


# ------------------------------------------------------------- LLM agents


def sub_config(**config: Any) -> dict[str, Any]:
    """An LLM sub-agent's settings."""
    return {
        "description": "",
        "instruction": "",
        "model": {"provider": "", "name": ""},
        "thinking_level": "",
        "output_schema": {},
        "include_contents": "default",
        "disallow_transfer_to_parent": False,
        "disallow_transfer_to_peers": False,
        "max_output_tokens": None,
        "sub_agents": [],
        **config,
    }


def llm_config(**config: Any) -> dict[str, Any]:
    """An LLM node's settings."""
    return sub_config(**{"mode": "single_turn", **config})


ANSWER = {
    "type": "object",
    "required": ["category", "refund"],
    "properties": {
        "category": {"type": "string", "enum": ["BUG", "BILLING"]},
        "refund": {"type": "number", "minimum": 0},
    },
}


def llm_agent() -> dict[str, Any]:
    return agent(
        [
            START_NODE,
            node(
                "triage",
                "llm",
                llm_config(
                    instruction="The customer wrote: {{ input.message }}",
                    output_schema=ANSWER,
                ),
            ),
            transform("after", '{"previous": previous, "triage": steps.triage.output}'),
        ],
        [("start", "next", "triage"), ("triage", "next", "after")],
    )


def test_an_llm_agent_answers_in_json_held_to_its_pydantic_schema() -> None:
    llm = ScriptedLlm(turns=[[types.Part(text='{"category": "BILLING", "refund": "3"}')]])
    svc = services(model=llm)
    session = run(lambda: ran(llm_agent(), {"message": "refund me"}, svc=svc))
    answer = {"category": "BILLING", "refund": 3}
    # Coerced by its model, as it's handed on and as its step's output; a
    # node's answer isn't kept in the state.
    assert session.outputs("after") == [{"previous": answer, "triage": answer}]
    assert "triage" not in run(session.session).state
    assert llm.requests[0].config.system_instruction == "The customer wrote: refund me"
    # Its schema is what the model is asked for.
    assert llm.requests[0].config.response_schema is not None


def test_llm_instructions_are_forge_templates() -> None:
    document = agent(
        [
            START_NODE,
            transform("lookup", '{"name": "Ada", "plan": "pro"}'),
            node(
                "write",
                "llm",
                llm_config(
                    instruction=(
                        "Write to {{ steps.lookup.output.name }} about "
                        "{{ input.topic }} ({{ previous.plan }}). Answer as "
                        '{"reply": "..."}, not {state} or {input}.'
                    )
                ),
            ),
            node(
                "team",
                "sequential",
                {
                    "description": "",
                    "sub_agents": [
                        {
                            "id": "draft",
                            "kind": "llm",
                            "name": "Drafter",
                            "config": sub_config(instruction="Draft from: {{ previous }}"),
                        },
                        {
                            "id": "polish",
                            "kind": "llm",
                            "name": "Polisher",
                            "config": sub_config(
                                instruction=(
                                    "Polish {{ state.draft }} for "
                                    "{{ steps.lookup.output.name }}; it was {{ previous }}."
                                )
                            ),
                        },
                    ],
                },
            ),
        ],
        [
            ("start", "next", "lookup"),
            ("lookup", "next", "write"),
            ("write", "next", "team"),
        ],
    )
    llm = ScriptedLlm(
        turns=[
            [types.Part(text="Dear Ada")],
            [types.Part(text="A draft")],
            [types.Part(text="A polished draft")],
        ]
    )
    session = run(lambda: ran(document, {"topic": "exports"}, svc=services(model=llm)))
    # What each was told: its instruction (before what ADK adds about who it is).
    prompts = [str(request.config.system_instruction).split("\n\nYou are an agent.")[0] for request in llm.requests]
    assert prompts == [
        # A { that isn't {{ }} reaches the model as it is: ADK's own {key}
        # state injection doesn't apply.
        'Write to Ada about exports (pro). Answer as {"reply": "..."}, not {state} or {input}.',
        # A team's sub-agents read what their node was handed as previous.
        "Draft from: Dear Ada",
        # A sub-agent's answer is kept in the state under its ID.
        "Polish A draft for Ada; it was Dear Ada.",
    ]
    state = run(session.session).state
    assert (state["draft"], state["polish"]) == ("A draft", "A polished draft")
    assert "write" not in state
    assert session.result == "A polished draft"


def test_an_instruction_that_fails_says_where() -> None:
    document = agent(
        [
            START_NODE,
            node("write", "llm", llm_config(instruction="{{ $uppercase(1) }}")),
        ],
        [("start", "next", "write")],
    )
    with pytest.raises(RunFailed, match=r"Write \(write\) failed: Its instruction failed"):
        run(lambda: ran(document, svc=services(model=ScriptedLlm())))


def test_an_llm_answer_that_doesnt_fit_its_schema_fails_the_run() -> None:
    llm = ScriptedLlm(turns=[[types.Part(text='{"category": "OTHER", "refund": 1}')]])
    with pytest.raises(ValueError, match="category"):
        run(lambda: ran(llm_agent(), {"message": "x"}, svc=services(model=llm)))


def test_a_team_hands_on_its_answer() -> None:
    def sub(sub_id: str, **config: Any) -> dict[str, Any]:
        return {
            "id": sub_id,
            "kind": "llm",
            "name": sub_id.title(),
            "config": sub_config(**config),
        }

    document = agent(
        [
            START_NODE,
            node(
                "team",
                "sequential",
                {"description": "", "sub_agents": [sub("first"), sub("second")]},
            ),
            node(
                "both",
                "parallel",
                {
                    "description": "",
                    "sub_agents": [sub("left"), sub("right", output_schema=ANSWER)],
                },
            ),
            transform("after", '{"team": steps.team.output, "both": previous}'),
        ],
        [
            ("start", "next", "team"),
            ("team", "next", "both"),
            ("both", "next", "after"),
        ],
    )
    llm = ScriptedLlm(
        turns=[
            [types.Part(text="one")],
            [types.Part(text="two")],
            [types.Part(text="L")],
            [types.Part(text='{"category": "BUG", "refund": 0}')],
        ]
    )
    session = run(lambda: ran(document, svc=services(model=llm)))
    assert session.outputs("after") == [
        {
            "team": "two",
            "both": {"left": "L", "right": {"category": "BUG", "refund": 0}},
        }
    ]


# ------------------------------------------------------------ saved agents


INNER = agent(
    [
        node(
            "start",
            "start",
            {
                "input_schema": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                }
            },
        ),
        transform(
            "add",
            '{"n": input.n + 1, "start": steps.start.output, "state": state.input}',
        ),
        end("done", "steps.add.output"),
    ],
    [("start", "next", "add"), ("add", "next", "done")],
    agent_id="ag_inner00001",
    name="Add one",
)

OUTER = agent(
    [
        START_NODE,
        transform("shape", '{"n": input.base}'),
        node("check", "saved", {"agent": "ag_inner00001"}, name="Check the number"),
        transform(
            "after",
            '{"previous": previous, "check": steps.check.output, "input": input}',
        ),
    ],
    [
        ("start", "next", "shape"),
        ("shape", "next", "check"),
        ("check", "next", "after"),
    ],
    agent_id="ag_outer00001",
    name="Outer",
)


def test_a_saved_agent_runs_nested_with_its_own_input() -> None:
    svc = services(resolve={"ag_inner00001": INNER}.get)
    session = run(lambda: ran(OUTER, {"base": "4"}, svc=svc))
    inner = {"n": 5, "start": {"n": 4}, "state": {"base": "4"}}
    # It hands on its result; its input is its own (coerced by its start), and
    # the run's stays the run's, for the steps after it too.
    assert session.outputs("after") == [{"previous": inner, "check": inner, "input": {"base": "4"}}]
    assert run(session.session).state["input"] == {"base": "4"}
    graph = build_agent(OUTER, services=svc)
    nested = by_name(graph)["check_the_number"]
    assert isinstance(nested, AgentGraph)
    assert set(by_name(nested)) == {
        START.name,
        "__input__",
        "add",
        "done",
        "__finish__",
    }


def test_a_saved_agents_input_must_fit_its_start() -> None:
    svc = services(resolve={"ag_inner00001": INNER}.get)
    with pytest.raises(
        RunFailed,
        match="Node 'Check the number': its input doesn't fit agent ag_inner00001's "
        "start: n: Input should be a valid integer",
    ):
        run(lambda: ran(OUTER, {"base": "four"}, svc=svc))


# ------------------------------------------------- surviving the process


def test_a_paused_run_resumes_in_a_fresh_build_from_its_sqlite_session(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "sessions.db")
    sent: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async def first() -> tuple[str, Pause]:
        # One process: the run starts, and pauses at the approval.
        session = await ran(
            approval_agent(),
            {"issue": {"key": "XMEN-12"}},
            svc=services(api),
            sessions=SqliteSessionService(database),
        )
        [pause] = await session.pauses()
        return session.session_id, pause

    session_id, pause = run(first)

    async def second() -> Run:
        # Another: the agent built again from its document; the run resumes
        # from its session's events.
        graph = build_agent(approval_agent(), services=services(api))
        session = await Run.of(graph, sessions=SqliteSessionService(database), session_id=session_id)
        [again] = await session.pauses()
        assert again == pause
        await session.answer(again, {"approved": True, "decided_by": "Lead"})
        return session

    session = run(second)
    assert session.result["approved"] is True
    assert session.result["decided_by"] == "Lead"
    # The request went out once, in the first process.
    assert sent == ["https://api.example.com/issues/XMEN-12/moves"]
    assert session.outputs("move") == []


# ------------------------------------------------- the example, run whole


def example_services(llm: ScriptedLlm, sent: list[httpx.Request]) -> RunServices:
    def api(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.startswith("/customers/"):
            return httpx.Response(
                200,
                json={"name": "Ada", "plan": "pro", "contacts": ["a@x.com", "b@x.com"]},
            )
        return httpx.Response(200, json={"ok": True})

    return services(api, model=llm, allowed_hosts=("api.example.com", "hooks.example.com"))


MESSAGE = {"message": "It crashes", "customer_id": "c1"}


def test_the_example_takes_a_bug_to_a_person_then_emails_each_contact() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part(text='{"category": "BUG", "summary": "It crashes"}')],
            [types.Part(text='{"mood": "annoyed"}')],
            [types.Part(text="1. Open it. 2. It crashes.")],
            [types.Part(text="Sorry! It's fixed.")],
        ]
    )
    sent: list[httpx.Request] = []

    async def main() -> tuple[list[Pause], Run]:
        session = await ran(example(), MESSAGE, svc=example_services(llm, sent))
        pauses = await session.pauses()
        await session.answer(pauses[0], {"send": True})
        return pauses, session

    pauses, session = run(main)
    assert [pause.message for pause in pauses] == ["Send this reply to Ada? Sorry! It's fixed."]
    assert session.outputs("read_the_message") == [
        {
            "triage": {"category": "BUG", "summary": "It crashes"},
            "sentiment": {"mood": "annoyed"},
        }
    ]
    assert [(r.method, str(r.url), json.loads(r.content or b"null")) for r in sent] == [
        ("GET", "https://api.example.com/customers/c1", None),
        *[
            (
                "POST",
                "https://hooks.example.com/support/replies",
                {"to": to, "reply": "Sorry! It's fixed."},
            )
            for to in ("a@x.com", "b@x.com")
        ],
    ]
    assert session.finished is not None
    assert session.finished["outcome"] == "succeeded"
    assert session.result["count"] == 2


def test_the_example_asks_before_refunding_and_a_rejection_declines() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part(text='{"category": "BILLING", "summary": "Charged twice"}')],
            [types.Part(text='{"mood": "angry"}')],
            [types.Part(text='{"answer": "We charged you twice.", "refund": 20}')],
        ]
    )
    sent: list[httpx.Request] = []

    async def main() -> tuple[list[Pause], Run]:
        session = await ran(example(), MESSAGE, svc=example_services(llm, sent))
        pauses = await session.pauses()
        await session.answer(pauses[0], {"approved": False, "decided_by": "Lead", "comment": "Too much"})
        return pauses, session

    pauses, session = run(main)
    assert [pause.message for pause in pauses] == ["Refund 20 dollars to Ada? We charged you twice."]
    assert pauses[0].payload["approvers"] == "org:admin"
    assert pauses[0].payload["expires_at"] == "2026-09-27T12:00:00+00:00"
    assert session.finished == {
        "outcome": "succeeded",
        "result": {"refund": 0, "reason": "Too much"},
    }


def test_the_example_researches_anything_else_in_parallel() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part(text='{"category": "ELSE", "summary": "How do exports work?"}')],
            [types.Part(text='{"mood": "calm"}')],
            [types.Part(text="The docs say: use Export.")],
            [types.Part(text="The forum says: it works.")],
        ]
    )
    session = run(lambda: ran(example(), MESSAGE, svc=example_services(llm, [])))
    # Its researchers run at once, each reading the triage's summary.
    assert sorted(str(r.config.system_instruction).split("\n")[0] for r in llm.requests[2:]) == [
        "Find what the docs say about: How do exports work?",
        "Find what the forum says about: How do exports work?",
    ]
    assert session.outputs("research") == [{"docs": "The docs say: use Export.", "forum": "The forum says: it works."}]
    assert session.result == {
        "docs": "The docs say: use Export.",
        "forum": "The forum says: it works.",
    }


def test_the_example_fails_for_a_customer_it_cant_find() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such customer"})

    with pytest.raises(RunFailed) as raised:
        run(lambda: ran(example(), MESSAGE, svc=services(missing, model=ScriptedLlm())))
    assert raised.value.message == ("Unknown customer (unknown_customer) failed the run: No customer on file: c1")


def test_the_example_polishes_an_approved_refunds_reply_in_a_loop() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part(text='{"category": "BILLING", "summary": "Charged twice"}')],
            [types.Part(text='{"mood": "angry"}')],
            [types.Part(text='{"answer": "We charged you twice.", "refund": 20}')],
            *[[types.Part(text=f"{who} {n}")] for n in (1, 2, 3) for who in ("Draft", "Critique")],
        ]
    )

    async def main() -> Run:
        session = await ran(example(), MESSAGE, svc=example_services(llm, []))
        [pause] = await session.pauses()
        await session.answer(pause, {"approved": True, "decided_by": "Lead"})
        return session

    session = run(main)
    prompts = [str(r.config.system_instruction) for r in llm.requests]
    assert prompts[2].startswith("Answer the customer's billing question: It crashes\n\nThey feel angry.")
    # The writer reads the billing agent's answer and the critic's last
    # critique; the critic, the writer's last draft.
    assert prompts[3].startswith(
        "Write the reply to the customer from this answer: We charged you twice.\n\nTake in any critique: \n"
    )
    assert prompts[4].startswith("Critique this reply: Draft 1")
    assert prompts[5].startswith(
        "Write the reply to the customer from this answer: We charged you twice.\n\nTake in any critique: Critique 1"
    )
    assert session.finished == {"outcome": "succeeded", "result": "Draft 3"}
    assert session.outputs("polish_the_reply") == ["Critique 3"]


def test_a_persons_answer_is_held_to_what_it_asks() -> None:
    schema = {
        "type": "object",
        "required": ["count"],
        "properties": {"count": {"type": "integer"}, "note": {"type": "string"}},
    }
    document = agent(
        [
            START_NODE,
            node(
                "ask",
                "human_input",
                {"message": "How many for {{ input.who }}?", "response_schema": schema},
            ),
            end("done", '{"answer": previous, "step": steps.ask.output}'),
        ],
        [("start", "next", "ask"), ("ask", "next", "done")],
    )

    async def main() -> tuple[Pause, Run]:
        session = await ran(document, {"who": "Ada"})
        [pause] = await session.pauses()
        await session.answer(pause, {"count": "3"})
        return pause, session

    pause, session = run(main)
    assert (pause.kind, pause.message, pause.due) == (
        "human_input",
        "How many for Ada?",
        None,
    )
    # What the person left out stays out; what they gave is coerced.
    assert session.result == {"answer": {"count": 3}, "step": {"count": 3}}


def test_run_services_take_the_workers_settings() -> None:
    settings = AdkWorkflowsSettings(
        http_allow_private=True,
        http_allowed_hosts=["intranet"],
        inline_delay_seconds=5,
        max_loop_items=7,
        expression_timeout_ms=100,
    )
    made = RunServices.of(settings, model="forge/model")
    assert made.allow_private is True
    assert made.allowed_hosts == ("intranet",)
    assert made.inline_delay_seconds == 5
    assert made.max_loop_items == 7
    assert made.evaluator.timeout_ms == 100
    assert made.model == "forge/model"
    # None changes nothing.
    assert made.but(model=None, resolve=None) == made
