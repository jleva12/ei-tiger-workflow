"""Intake agents used as tools: an agent files the person's requests to them itself."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from forge_agent_runtime.build import BuildError, build_app
from forge_agent_runtime.document import load_document, parse_document
from forge_agent_runtime.requests import SNAPSHOT_KEY, STATE_KEY
from forge_agent_runtime.services import RequestConflict, RuntimeServices, StartSource
from tests.conftest import FIXTURES, call, text
from tests.outbox import Outbox

ACCESS = [
    {"key": "requester_email", "label": "Your email", "type": "email"},
    {"key": "department", "label": "Department", "type": "choice", "options": ["Finance", "Sales"]},
    {"key": "app_name", "label": "Application"},
    {"key": "access_level", "label": "Access", "type": "choice", "options": ["Read", "Write"]},
    {"key": "reason", "label": "Reason", "required": False},
]
LAPTOP = [
    {"key": "requester_email", "label": "Your email", "type": "email"},
    {"key": "department", "label": "Department"},
    {"key": "laptop_model", "label": "Model", "type": "choice", "options": ["Mac", "Windows"]},
    {"key": "needed_by", "label": "Needed by", "type": "date"},
]


def service_desk(*, ticket: bool = True) -> dict[str, Any]:
    """A service desk that files requests to two teams' intakes, each opening a ticket when submitted."""
    nodes: list[dict[str, Any]] = [
        {"id": "agent", "kind": "agent", "name": "Service desk", "config": {"instruction": "Help."}},
        {
            "id": "intake_access",
            "kind": "intake_agent",
            "name": "Access request",
            "config": {"description": "Access to an application.", "fields": copy.deepcopy(ACCESS)},
        },
        {
            "id": "intake_laptop",
            "kind": "intake_agent",
            "name": "Laptop request",
            "config": {"description": "A new laptop.", "fields": copy.deepcopy(LAPTOP)},
        },
    ]
    edges: list[dict[str, Any]] = [
        {"id": "e1", "source": "agent", "source_output": "tools", "target": "intake_access"},
        {"id": "e2", "source": "agent", "source_output": "tools", "target": "intake_laptop"},
    ]
    if ticket:
        nodes.append(
            {
                "id": "wf_ticket",
                "kind": "adk_workflow",
                "name": "Open ticket",
                "config": {"workflow": "ag_ticket"},
            }
        )
        edges += [
            {"id": "e3", "source": "intake_access", "source_output": "on_complete", "target": "wf_ticket"},
            {"id": "e4", "source": "intake_laptop", "source_output": "on_complete", "target": "wf_ticket"},
        ]
    return {"format": "forge.chat_agent/v1", "id": "ca_desk", "name": "Desk", "nodes": nodes, "edges": edges}


class Tickets(Outbox):
    """The organization's workflows: Open ticket takes the requester and department."""

    async def describe(self, workflow_id, *, organization_id):
        start = {"type": "object", "properties": {"requester_email": {}, "department": {}}}
        return "Open ticket", "Opens a ticket with the team.", start

    def result(self, number: int) -> dict[str, Any]:
        return {"ticket": number}


class Requests:
    """The hosted runtime's requests, kept by organization, agent and person."""

    def __init__(self) -> None:
        self.kept: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}

    async def requests(
        self,
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
        ids: Sequence[str] | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        found = [
            request
            for request in self.kept.get((organization_id, agent_id, user_id), {}).values()
            if (ids is None or request["id"] in ids) and (status is None or request["status"] == status)
        ]
        found.sort(key=lambda request: request["updated_at"], reverse=True)
        return copy.deepcopy(found[:limit] if limit else found)

    async def save(
        self,
        requests: Sequence[Mapping[str, Any]],
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
    ) -> None:
        mine = self.kept.setdefault((organization_id, agent_id, user_id), {})
        # Each kept only on the version it was read at: all of them, or none.
        for request in requests:
            there = mine.get(request["id"])
            if (there["version"] if there is not None else None) != request.get("version"):
                raise RequestConflict(f"Request {request['id']} changed since it was read.")
        for request in requests:
            version = request.get("version")
            mine[request["id"]] = {
                **copy.deepcopy(dict(request)),
                "version": 0 if version is None else version + 1,
            }


class Chat:
    def __init__(self, app: Any) -> None:
        self.sessions = InMemorySessionService()
        self.runner = Runner(app=app, session_service=self.sessions)
        self.app = app
        self.id = ""

    async def say(self, value: str) -> list[Event]:
        if not self.id:
            self.id = (await self.sessions.create_session(app_name=self.app.name, user_id="ada")).id
        message = types.Content(role="user", parts=[text(value)])
        return [
            event
            async for event in self.runner.run_async(user_id="ada", session_id=self.id, new_message=message)
        ]

    async def state(self) -> dict[str, Any]:
        session = await self.sessions.get_session(app_name=self.app.name, user_id="ada", session_id=self.id)
        assert session is not None
        return dict(session.state)


def answered(events: list[Event], name: str) -> dict[str, Any]:
    return next(dict(r.response or {}) for e in events for r in e.get_function_responses() if r.name == name)


def call_id(events: list[Event], name: str) -> str:
    return next(str(c.id) for e in events for c in e.get_function_calls() if c.name == name)


TOOLS = [
    "start_requests",
    "answer_requests",
    "review_requests",
    "submit_requests",
    "cancel_requests",
    "list_requests",
]


async def test_intakes_on_an_agents_tools_way_are_its_request_tools(models) -> None:
    app = await build_app(
        parse_document(service_desk()), models=models, services=RuntimeServices(workflows=Tickets())
    )
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    # No agent of their own: the agent asks their questions itself.
    assert root.sub_agents == []
    assert [getattr(tool, "name", "") for tool in root.tools] == TOOLS
    start = root.tools[0]
    declared = start._get_declaration()  # type: ignore[union-attr]
    assert declared is not None
    schema = declared.parameters_json_schema
    assert isinstance(schema, dict)
    assert schema["properties"]["intakes"]["items"]["enum"] == ["access_request", "laptop_request"]
    # Every key once, as the first intake that has it takes it.
    known = schema["properties"]["known"]["properties"]
    assert list(known) == [
        "requester_email",
        "department",
        "app_name",
        "access_level",
        "reason",
        "laptop_model",
        "needed_by",
    ]
    assert known["department"]["enum"] == ["Finance", "Sales"]

    # Their workflows run only in the hosted runtime.
    with pytest.raises(BuildError, match="'Open ticket' runs a workflow, which only the hosted runtime can"):
        await build_app(parse_document(service_desk()), models=models)


async def test_an_agent_files_several_requests_asking_what_they_share_once(models, llm) -> None:
    tickets = Tickets()
    app = await build_app(
        parse_document(service_desk()), models=models, services=RuntimeServices(workflows=tickets)
    )
    chat = Chat(app)
    llm.turns = [
        [
            call(
                "start_requests",
                intakes=["access_request", "laptop_request"],
                known={"requester_email": "ada@x.co"},
            )
        ],
        [text("Which department are you in?")],
    ]
    events = await chat.say("I need read access to Ledger and a new laptop. I'm ada@x.co.")
    started = answered(events, "start_requests")
    assert [request["intake"] for request in started["started"]] == ["Access request", "Laptop request"]
    # What they share is asked once, for both.
    ask = {entry["key"]: entry for entry in started["ask_next"]}
    assert "requester_email" not in ask
    assert ask["department"]["for"] == ["Access request", "Laptop request"]
    # Each takes it its own way, and the agent's told how.
    assert ask["department"]["options"] == ["Finance", "Sales"]
    assert ask["department"]["differs"] == [{"for": "Laptop request", "type": "text"}]
    # The agent is told where its requests stand before its next call.
    brief = llm.requests[-1].config.system_instruction
    assert "## Open requests in this conversation" in str(brief)
    assert "Department (department, for Access request, Laptop request)" in str(brief)

    llm.turns = [
        [
            call(
                "answer_requests",
                answers={
                    "department": "Finance",
                    "app_name": "Ledger",
                    "access_level": "Read",
                    "laptop_model": "Mac",
                    "needed_by": "2026-11-01",
                    "cost_center": "CC-12",
                },
            )
        ],
        [text("Here's what I have. Shall I submit them?")],
    ]
    events = await chat.say("Finance. A Mac by November 1st.")
    recorded = answered(events, "answer_requests")
    assert recorded["recorded"] == {
        "Access request": ["department", "app_name", "access_level"],
        "Laptop request": ["department", "laptop_model", "needed_by"],
    }
    assert recorded["refused"] == {"cost_center": "no open request asks for it"}
    assert all(request["ready"] for request in recorded["requests"])

    # Ready, but not read back and said yes to: not submitted.
    llm.turns = [[call("submit_requests")], [text("Let me read them back first.")]]
    early = answered(await chat.say("Submit them."), "submit_requests")
    assert (
        early["submitted"] == []
        and [r["summary"] for r in early["not_ready"]]
        == [
            "read it back (review_requests); when they say it's right, record summary_confirmed (answer_requests)"
        ]
        * 2
    )
    assert tickets.runs == [] and tickets.intents == {}

    llm.turns = [
        [call("review_requests")],
        [text("Here's what I have. Is it right?")],
    ]
    await chat.say("Go on.")
    llm.turns = [
        [call("answer_requests", summary_confirmed=True)],
        [call("submit_requests")],
        [text("Both are in.")],
    ]
    events = await chat.say("Yes, submit them.")
    assert answered(events, "answer_requests")["recorded"] == {
        "Access request": ["summary_confirmed"],
        "Laptop request": ["summary_confirmed"],
    }
    submitted = answered(events, "submit_requests")["submitted"]
    assert [request["intake"] for request in submitted] == ["Access request", "Laptop request"]
    state = await chat.state()
    ids = {request["intake"]: request["id"] for request in state[STATE_KEY].values()}
    # Each starts its own workflows, with what their starts take, known by the request (not the call).
    assert tickets.runs == [
        (
            "ag_ticket",
            {"requester_email": "ada@x.co", "department": "Finance"},
            f"request-{ids['intake_access']}-0",
            120.0,
        ),
        (
            "ag_ticket",
            {"requester_email": "ada@x.co", "department": "Finance"},
            f"request-{ids['intake_laptop']}-0",
            120.0,
        ),
    ]
    assert submitted[0]["workflows"][0]["call_id"] == f"request-{ids['intake_access']}-0"
    assert submitted[1]["workflows"][0]["result"] == {"ticket": 2}
    # Written down for the request, then started, once each.
    assert [intent["source"] for intent in tickets.intents.values()] == [
        StartSource("request", ids["intake_access"]),
        StartSource("request", ids["intake_laptop"]),
    ]
    assert [intent["status"] for intent in tickets.intents.values()] == ["dispatched", "dispatched"]

    # Without a store, the conversation keeps them.
    assert sorted(request["status"] for request in state[STATE_KEY].values()) == ["submitted", "submitted"]
    assert state[SNAPSHOT_KEY] == {"requests": [], "ask_next": []}

    # Submitted again (the model calls it again, by ID): nothing starts twice.
    llm.turns = [[call("submit_requests", requests=list(ids.values()))], [text("They're in already.")]]
    again = answered(await chat.say("Did you submit them?"), "submit_requests")
    assert again["submitted"] == [] and len(again["already_submitted"]) == 2
    assert len(tickets.runs) == 2


async def test_a_value_one_request_doesnt_take_is_refused_only_there(models, llm) -> None:
    app = await build_app(parse_document(service_desk(ticket=False)), models=models)
    chat = Chat(app)
    llm.turns = [
        [call("start_requests", intakes=["access_request", "laptop_request"])],
        [call("answer_requests", answers={"department": "Marketing"})],
        [text("Access requests only take Finance or Sales.")],
    ]
    events = await chat.say("Marketing needs things.")
    recorded = answered(events, "answer_requests")
    assert recorded["recorded"] == {"Laptop request": ["department"]}
    assert list(recorded["refused"]) == ["department (Access request)"]


async def test_a_request_started_later_starts_with_what_was_said(models, llm) -> None:
    app = await build_app(parse_document(service_desk(ticket=False)), models=models)
    chat = Chat(app)
    llm.turns = [
        [call("start_requests", intakes=["laptop_request"])],
        [call("answer_requests", answers={"requester_email": "ada@x.co", "department": "Sales"})],
        [text("Which model?")],
    ]
    await chat.say("I need a laptop.")
    llm.turns = [[call("start_requests", intakes=["access_request"])], [text("Which application?")]]
    started = answered(await chat.say("Oh, and access to Ledger."), "start_requests")
    ask = {entry["key"] for entry in started["ask_next"]}
    # Its email and department were said already, and it takes them.
    assert "requester_email" not in ask and "department" not in ask
    assert {"laptop_model", "app_name"} <= ask


async def test_a_request_that_isnt_ready_isnt_submitted(models, llm) -> None:
    tickets = Tickets()
    app = await build_app(
        parse_document(service_desk()), models=models, services=RuntimeServices(workflows=tickets)
    )
    chat = Chat(app)
    llm.turns = [
        [call("start_requests", intakes=["laptop_request"], known={"requester_email": "ada@x.co"})],
        [call("submit_requests")],
        [text("I still need a few things.")],
    ]
    submitted = answered(await chat.say("Laptop please."), "submit_requests")
    assert submitted["submitted"] == []
    assert submitted["not_ready"][0]["still_needed"] == ["department", "laptop_model", "needed_by"]
    assert tickets.runs == []


async def test_requests_are_kept_for_the_person_across_conversations(models, llm) -> None:
    store = Requests()
    services = RuntimeServices(workflows=Tickets(), intake_requests=store)
    app = await build_app(parse_document(service_desk()), models=models, services=services)
    first = Chat(app)
    llm.turns = [
        [
            call(
                "start_requests",
                intakes=["access_request", "laptop_request"],
                known={"requester_email": "ada@x.co"},
            )
        ],
        [text("Which department?")],
    ]
    await first.say("Access and a laptop.")
    kept = store.kept[(None, "ca_desk", "ada")]
    assert sorted(request["intake"] for request in kept.values()) == ["intake_access", "intake_laptop"]
    access = next(request["id"] for request in kept.values() if request["intake"] == "intake_access")
    laptop = next(request["id"] for request in kept.values() if request["intake"] == "intake_laptop")
    # Nothing in the conversation's own state but where they stand.
    assert STATE_KEY not in await first.state()

    second = Chat(app)
    llm.turns = [[call("list_requests", status="open")], [text("You have two open requests.")]]
    listed = answered(await second.say("Where are my requests?"), "list_requests")["requests"]
    assert {request["id"] for request in listed} == {access, laptop}
    assert not any(request["in_this_conversation"] for request in listed)
    assert listed[0]["still_needed"]

    llm.turns = [
        [call("cancel_requests", requests=[laptop])],
        [call("start_requests", resume=[access])],
        [text("Picked up your access request.")],
    ]
    events = await second.say("Drop the laptop; carry on with the access one.")
    assert answered(events, "cancel_requests")["cancelled"] == [{"id": laptop, "intake": "Laptop request"}]
    resumed = answered(events, "start_requests")
    assert resumed["resumed"] == [{"id": access, "intake": "Access request"}]
    assert [request["id"] for request in resumed["requests"]] == [access]
    assert kept[laptop]["status"] == "cancelled"
    assert kept[access]["session_id"] == second.id


# The example ------------------------------------------------------------------

SERVICE_DESK = FIXTURES / "service-desk.chat-agent.json"


async def test_the_example_service_desk_files_requests_to_three_teams(models, llm) -> None:
    doc = load_document(SERVICE_DESK)
    tickets = Tickets()
    app = await build_app(doc, models=models, services=RuntimeServices(workflows=tickets))
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    assert root.sub_agents == []
    # It reads requests back as a table, and files them with the request tools.
    assert [getattr(tool, "name", "") for tool in root.tools] == ["present", *TOOLS]

    chat = Chat(app)
    llm.turns = [
        [
            call(
                "start_requests",
                intakes=["access_request", "laptop_request", "report_request"],
                known={"requester_email": "ada@northwind.example"},
            )
        ],
        [text("Which department are you in?")],
    ]
    started = answered(
        await chat.say("I'm new: I need Ledger access, a laptop and a sales report."), "start_requests"
    )
    ask = {entry["key"]: entry["for"] for entry in started["ask_next"]}
    # What the teams share is asked once, for each that needs it.
    assert ask["department"] == ["Access request", "Laptop request", "Report request"]
    assert ask["manager_email"] == ["Access request", "Laptop request"]
    assert ask["cost_center"] == ["Laptop request", "Report request"]

    llm.turns = [
        [
            call(
                "answer_requests",
                answers={
                    "department": "Sales",
                    "manager_email": "lin@northwind.example",
                    "cost_center": "CC-4410",
                    "needed_by": "2026-11-02",
                    "application": "Ledger",
                    "access_level": "Admin",
                    "work_location": "Remote",
                },
            )
        ],
        [text("Why do you need admin access, and where should the laptop go?")],
    ]
    progress = answered(
        await chat.say("Sales, my manager is lin@, CC-4410, by Nov 2. Admin on Ledger. I'm remote."),
        "answer_requests",
    )
    still = {request["intake"]: request["still_needed"] for request in progress["requests"]}
    # Admin access and working remotely each open a question of their own.
    assert "admin_justification" in still["Access request"]
    assert "shipping_address" in still["Laptop request"]
    assert "cost_center" not in still["Report request"] and "needed_by" not in still["Report request"]
