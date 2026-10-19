"""Intake agents: a task an agent hands off to, that collects a designed record in any order."""

from __future__ import annotations

import copy
import time
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from forge_agent_runtime import intake as intake_module
from forge_agent_runtime.build import TOOL_KINDS, BuildError, build_app
from forge_agent_runtime.document import DocumentError, load_document, parse_document
from forge_agent_runtime.executor import AgentExecutor, DocumentAgentSource, RunRequest
from forge_agent_runtime.intake import (
    IntakeDesign,
    IntakeRecord,
    ValueRefused,
    checked,
    design_problems,
    holds,
    preview,
    progress_of,
    record_id,
    state_key,
)
from forge_agent_runtime.services import INTAKE, RuntimeServices, StartSource, workflow_starter
from forge_agent_runtime.tasks import TASK_CALLS_KEY, open_task_calls, without_cancelled_tasks
from forge_agent_runtime.wire import to_wire
from forge_common.adk.follow_ups import QUIET_KEY
from tests.conftest import FIXTURES, call, text
from tests.outbox import Outbox

FIELDS: list[dict[str, Any]] = [
    {"key": "full_name", "label": "Full name", "prefill": True},
    {"key": "email", "label": "Email", "type": "email"},
    {"key": "claim_type", "label": "Claim type", "type": "choice", "options": ["Auto", "Home"]},
    {"key": "vehicle_plate", "label": "Plate", "section": "vehicle"},
    {"key": "date_of_loss", "label": "Date of loss", "type": "date", "infer": True},
    {"key": "notes", "label": "Notes", "required": False},
]
SECTIONS = [{"id": "vehicle", "title": "Vehicle", "when": 'claim_type = "Auto"'}]


def document(**config: Any) -> dict[str, Any]:
    """A front desk agent that hands off to a claim intake."""
    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_desk",
        "name": "Desk",
        "nodes": [
            {"id": "agent", "kind": "agent", "name": "Front desk", "config": {"instruction": "Help."}},
            {
                "id": "intake_a",
                "kind": "intake_agent",
                "name": "Claim intake",
                "config": {
                    "description": "Takes a new claim.",
                    "fields": copy.deepcopy(FIELDS),
                    "sections": copy.deepcopy(SECTIONS),
                    **config,
                },
            },
        ],
        "edges": [{"id": "e1", "source": "agent", "source_output": "agents", "target": "intake_a"}],
    }


def design(**config: Any) -> IntakeDesign:
    return IntakeDesign.of("Claim intake", {"fields": FIELDS, "sections": SECTIONS, **config})


def record(**values: Any) -> IntakeRecord:
    return IntakeRecord.model_validate(
        {"answers": {k: {"value": v, "how": "stated"} for k, v in values.items()}}
    )


class Chat:
    """Turns of a conversation with an app, through ADK's runner."""

    def __init__(self, app: Any) -> None:
        self.sessions = InMemorySessionService()
        self.runner = Runner(app=app, session_service=self.sessions)
        self.app = app
        self.id = ""

    async def say(self, *parts: types.Part) -> list[Event]:
        if not self.id:
            self.id = (await self.sessions.create_session(app_name=self.app.name, user_id="ada")).id
        message = types.Content(role="user", parts=list(parts))
        return [
            event
            async for event in self.runner.run_async(user_id="ada", session_id=self.id, new_message=message)
        ]

    async def state(self) -> dict[str, Any]:
        session = await self.sessions.get_session(app_name=self.app.name, user_id="ada", session_id=self.id)
        assert session is not None
        return dict(session.state)


def calls(events: list[Event], name: str) -> list[dict[str, Any]]:
    return [dict(c.args or {}) for e in events for c in e.get_function_calls() if c.name == name]


def answers(events: list[Event], name: str) -> list[dict[str, Any]]:
    return [dict(r.response or {}) for e in events for r in e.get_function_responses() if r.name == name]


# The design --------------------------------------------------------------------


def test_a_design_that_cant_work_is_refused() -> None:
    assert design_problems("Claim intake", {"fields": FIELDS, "sections": SECTIONS}) == []
    assert design_problems("X", {"fields": []}) == ["'X' has no fields: add what it collects."]
    problems = design_problems(
        "X",
        {
            "fields": [
                {"key": "Email"},
                {"key": "background"},
                {"key": "kind", "type": "choice"},
                {"key": "kind"},
                {"key": "plate", "section": "gone"},
                {"key": "employer", "when": "employment = "},
            ],
            "sections": [{"id": "s", "when": "(("}, {"id": "s"}],
        },
    )
    assert problems == [
        "'X''s field 'Email': a key is lower-case letters, digits and _, starting with a letter.",
        "'X''s field 'background': that key is kept for what the agent handing off says.",
        "'X''s field 'kind' is a choice with no options.",
        "'X' has two fields called 'kind'.",
        "'X''s field 'plate' is in a section it doesn't have.",
        pytest.approx(problems[5]),
        pytest.approx(problems[6]),
        "'X' has two sections with the ID 's'.",
    ]
    assert problems[5].startswith("'X''s field 'employer': its condition isn't JSONata")
    assert problems[6].startswith("'X''s section 's': its condition isn't JSONata")


@pytest.mark.parametrize(
    ("field", "given", "kept"),
    [
        ({"type": "text"}, 42, "42"),
        ({"type": "number"}, "1,200.5", 1200.5),
        ({"type": "number"}, "3", 3),
        ({"type": "integer"}, 7.0, 7),
        ({"type": "boolean"}, "Yes", True),
        ({"type": "date"}, " 2026-03-14 ", "2026-03-14"),
        ({"type": "email"}, "a@b.co", "a@b.co"),
        ({"type": "choice", "options": ["Auto", "Home"]}, "home", "Home"),
        ({"type": "choices", "options": ["Auto", "Home"]}, ["auto", "AUTO", "Home"], ["Auto", "Home"]),
        ({"type": "choices", "options": ["Auto", "Home"]}, "auto", ["Auto"]),
    ],
)
def test_values_are_kept_as_their_field_takes_them(field: dict[str, Any], given: Any, kept: Any) -> None:
    assert checked(IntakeDesign.of("X", {"fields": [{"key": "f", **field}]}).fields[0], given) == kept


@pytest.mark.parametrize(
    ("field", "given", "why"),
    [
        ({"type": "text"}, "  ", "it's empty"),
        ({"type": "number"}, "lots", "it's a number"),
        ({"type": "number"}, True, "it's a number"),
        ({"type": "integer"}, 2.5, "it's a whole number"),
        ({"type": "boolean"}, "maybe", "it's yes or no"),
        ({"type": "date"}, "14 March", "it's a date, as YYYY-MM-DD"),
        ({"type": "email"}, "ada at home", "it's an email address"),
        ({"type": "choice", "options": ["Auto"]}, "Boat", 'it\'s one of "Auto"'),
    ],
)
def test_a_value_a_field_doesnt_take_is_refused_saying_why(
    field: dict[str, Any], given: Any, why: str
) -> None:
    with pytest.raises(ValueRefused, match=why):
        checked(IntakeDesign.of("X", {"fields": [{"key": "f", **field}]}).fields[0], given)


def test_what_to_ask_next_follows_the_sections_and_their_conditions() -> None:
    claim = design()
    # Nothing in: the fields in no section first, the vehicle's locked.
    first = progress_of(claim, IntakeRecord())
    assert [f.key for f in first.ask_next] == ["full_name", "email", "claim_type", "date_of_loss", "notes"]
    assert [s.id for s in first.locked] == ["vehicle"]
    assert [f.key for f in first.missing] == ["full_name", "email", "claim_type", "date_of_loss"]

    # An auto claim unlocks the vehicle: asked once the first section's in.
    auto = progress_of(
        claim, record(full_name="Dana", email="d@x.co", claim_type="Auto", date_of_loss="2026-01-02")
    )
    assert [f.key for f in auto.ask_next] == ["vehicle_plate"]
    assert auto.next_section is not None and auto.next_section.id == "vehicle"
    assert auto.locked == []

    # A home claim doesn't: its plate never applies.
    home = progress_of(
        claim, record(full_name="Dana", email="d@x.co", claim_type="Home", date_of_loss="2026-01-02")
    )
    assert home.ready
    assert [f.key for f in home.optional] == ["notes"]
    assert "vehicle_plate" not in [f.key for f in home.applying]

    # Only true holds; a field's own condition reads the others.
    conditional = IntakeDesign.of(
        "X", {"fields": [{"key": "employed", "type": "boolean"}, {"key": "employer", "when": "employed"}]}
    )
    assert [f.key for f in progress_of(conditional, record()).applying] == ["employed"]
    assert [f.key for f in progress_of(conditional, record(employed=True)).missing] == ["employer"]


def test_conditions_match_on_the_bounded_engine_and_all_of_them_stop_in_time(monkeypatch) -> None:
    monkeypatch.setattr(intake_module, "CONDITION_MS", 50)
    monkeypatch.setattr(intake_module, "CONDITIONS_MS", 200)
    assert holds("$contains(email, /@EXAMPLE\\.com$/i)", {"email": "dana@example.com"})
    # Python's re backtracks on this for minutes, holding the event loop.
    slow = "$contains(name, /^(a|aa)+$/)"
    many = IntakeDesign.of(
        "X",
        {
            "fields": [
                {"key": "name"},
                *({"key": f"f{i}", "when": f"{slow} or {i} < 0"} for i in range(20)),
                {"key": "last", "when": "true"},
            ]
        },
    )
    answers = IntakeRecord.model_validate({"answers": {"name": {"value": "a" * 200 + "b", "how": "stated"}}})
    started = time.monotonic()
    for _ in range(5):
        progress = progress_of(many, answers)
    # 20 conditions of 50 ms each, five times, would take 5 s: they share 200 ms, worked out once.
    assert time.monotonic() - started < 1
    # A condition that runs out of time doesn't hold, nor do those left after the budget's spent.
    assert [f.key for f in progress.applying] == ["name"]


def test_a_condition_whose_pattern_is_too_big_to_compile_is_a_design_problem() -> None:
    config = {"fields": [{"key": "name", "when": "$contains(name, /((a{1000}){1000}){1000}/)"}]}
    assert any("isn't JSONata" in problem for problem in design_problems("X", config))


def test_an_inferred_value_or_one_that_must_be_confirmed_waits_for_a_yes() -> None:
    claim = design()
    inferred = IntakeRecord.model_validate(
        {
            "answers": {
                "full_name": {"value": "Dana", "how": "handoff"},
                "email": {"value": "d@x.co", "how": "stated"},
                "claim_type": {"value": "Home", "how": "stated"},
                "date_of_loss": {"value": "2026-01-02", "how": "inferred"},
            }
        }
    )
    assert [f.key for f in progress_of(claim, inferred).unconfirmed] == ["date_of_loss"]
    inferred.answers["date_of_loss"].how = "confirmed"
    assert progress_of(claim, inferred).ready
    strict = design(fields=[{**FIELDS[0], "confirm": True}])
    assert [f.key for f in progress_of(strict, inferred).unconfirmed] == ["full_name"]


# The document ------------------------------------------------------------------


def test_an_intake_is_handed_off_to_or_used_as_a_tool_by_agents() -> None:
    assert parse_document(document()).node("intake_a").kind == "intake_agent"
    # Not by a workflow's LLM agent: only on a canvas.
    assert "intake_agent" not in TOOL_KINDS

    called = document()
    called["edges"][0]["source_output"] = "tools"
    assert parse_document(called).targets("agent", "tools")[0].id == "intake_a"

    nested = document()
    nested["nodes"].append({"id": "intake_b", "kind": "intake_agent", "name": "Contact", "config": {}})
    nested["edges"].append({"id": "e2", "source": "intake_a", "source_output": "tools", "target": "intake_b"})
    with pytest.raises(DocumentError, match="an intake agent can't file requests to another"):
        parse_document(nested)

    twice = document()
    twice["nodes"].append(
        {"id": "sub_b", "kind": "sub_agent", "name": "Billing", "config": {"instruction": "Bill."}}
    )
    twice["edges"] += [
        {"id": "e2", "source": "agent", "source_output": "agents", "target": "sub_b"},
        {"id": "e3", "source": "sub_b", "source_output": "agents", "target": "intake_a"},
    ]
    with pytest.raises(DocumentError, match="'Claim intake' is handed off to by 2 agents; ADK allows one"):
        parse_document(twice)

    named = document()
    named["nodes"][1]["name"] = "Front desk"
    with pytest.raises(DocumentError, match="have the same ADK name, 'front_desk'"):
        parse_document(named)

    handing = document()
    handing["nodes"].append(
        {"id": "sub_b", "kind": "sub_agent", "name": "Billing", "config": {"instruction": "Bill."}}
    )
    handing["edges"].append({"id": "e2", "source": "intake_a", "source_output": "agents", "target": "sub_b"})
    with pytest.raises(DocumentError, match="leaves 'Claim intake', which has no 'agents' way out"):
        parse_document(handing)

    # The format's schema has the keys' shape; the design's own rules are checked too.
    broken = document(fields=[{"key": "kind", "type": "choice"}])
    with pytest.raises(DocumentError, match="'Claim intake''s field 'kind' is a choice with no options"):
        parse_document(broken)


async def test_the_agent_handing_off_calls_it_with_what_it_may_fill_in(models) -> None:
    app = await build_app(parse_document(document()), models=models)
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    intake = root.sub_agents[0]
    assert isinstance(intake, LlmAgent)
    assert intake.mode == "task"
    assert (intake.disallow_transfer_to_parent, intake.disallow_transfer_to_peers) == (True, True)
    delegation = next(tool for tool in root.tools if getattr(tool, "name", "") == "claim_intake")
    assert isinstance(delegation, BaseTool)
    declared = delegation._get_declaration()
    assert declared is not None and declared.description is not None
    assert declared.parameters_json_schema is not None
    assert set(declared.parameters_json_schema["properties"]) == {"background", "full_name"}
    assert declared.description.startswith(
        "Takes a new claim. It collects: Full name, Email, Claim type, Plate, Date of loss, Notes."
    )


async def test_an_intake_with_form_controls_may_ask_with_them(models, llm) -> None:
    doc = document()
    doc["nodes"].append(
        {
            "id": "rich",
            "kind": "rich_answers",
            "name": "Rich answers",
            "config": {"components": ["Card", "Form", "Input"]},
        }
    )
    doc["edges"].append({"id": "e_rich", "source": "intake_a", "source_output": "tools", "target": "rich"})
    app = await build_app(parse_document(doc), models=models)
    chat = Chat(app)
    llm.turns = [[call("claim_intake")], [text("Hello")]]
    await chat.say(text("A claim"))
    brief = str(llm.requests[1].config.system_instruction)
    assert (
        "You may ask with present too: a Form (or a Card with asForm), each field named by the key" in brief
    )


async def test_an_intake_uses_what_its_tools_way_leads_to(models, llm) -> None:
    doc = document()
    doc["nodes"].append(
        {
            "id": "rich",
            "kind": "rich_answers",
            "name": "Rich answers",
            "config": {"components": ["Table", "Choices"]},
        }
    )
    doc["edges"].append({"id": "e_rich", "source": "intake_a", "source_output": "tools", "target": "rich"})
    app = await build_app(parse_document(doc), models=models)
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    intake = root.sub_agents[0]
    assert isinstance(intake, LlmAgent)
    # Its own tools, what's on its Tools way, a form to ask with, and ADK's finish_task.
    assert [getattr(tool, "name", "") for tool in intake.tools] == [
        "record_answers",
        "present",
        "adk_request_input",
        "finish_task",
    ]

    # With rich answers, it reads the summary back as a table to confirm.
    chat = Chat(app)
    llm.turns = [[call("claim_intake")], [text("Hello")]]
    await chat.say(text("A claim"))
    brief = str(llm.requests[1].config.system_instruction)
    assert "show it with present (not a form): a Table of each field and its value, with Choices" in brief
    # Without controls among its components, it doesn't draw forms to ask with.
    assert "You may ask with present too" not in brief


class FileClaim(Outbox):
    """The organization's workflows, as the hosted runtime runs them: File claim takes three fields."""

    def __init__(self, fails: bool = False) -> None:
        super().__init__(refuse="Its start needs a policy number." if fails else None)

    async def describe(self, workflow_id, *, organization_id):
        start = {"type": "object", "properties": {"full_name": {}, "email": {}, "claim_type": {}}}
        return "File claim", "Files a claim with the claims system.", start

    def result(self, number: int) -> dict[str, Any]:
        return {"claim_number": f"CLM-{number:06d}"}


def filing(**config: Any) -> dict[str, Any]:
    """The front desk's claim intake, starting File claim when it completes."""
    doc = document(fields=[*FIELDS[:3]], sections=[], **config)
    doc["nodes"].append(
        {
            "id": "wf_file",
            "kind": "adk_workflow",
            "name": "File claim",
            "config": {"workflow": "ag_file", "version": 2, "wait_seconds": 5},
        }
    )
    doc["edges"].append(
        {"id": "e_file", "source": "intake_a", "source_output": "on_complete", "target": "wf_file"}
    )
    return doc


async def test_completing_starts_its_workflows_with_the_record(models, llm) -> None:
    workflows = FileClaim()
    services = RuntimeServices(workflows=workflows)
    app = await build_app(parse_document(filing(confirm_summary=False)), models=models, services=services)
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    delegation = next(tool for tool in root.tools if getattr(tool, "name", "") == "claim_intake")
    assert isinstance(delegation, BaseTool)
    declared = delegation._get_declaration()
    assert declared is not None and declared.description is not None
    assert "When it completes it starts File claim with the record" in declared.description

    chat = Chat(app)
    llm.turns = [
        [call("claim_intake", full_name="Dana Li")],
        [call("record_answers", answers={"email": "dana@x.co", "claim_type": "Auto"})],
        [call("finish_task", status="completed")],
        [text("Your claim CLM-000123 is in.")],
    ]
    events = await chat.say(text("I had a car accident"))
    handoff = next(c for e in events for c in e.get_function_calls() if c.name == "claim_intake")
    # Known by the record (the conversation and the hand-off), never the model's finish_task call.
    record_of = record_id(chat.id, "claim_intake", handoff.id)
    started = f"intake-{record_of}-0"
    # Started once, by code, with what its start takes, at the version and wait its node says.
    assert workflows.runs == [
        (
            "ag_file@2",
            {"full_name": "Dana Li", "email": "dana@x.co", "claim_type": "Auto"},
            started,
            5.0,
        )
    ]
    # Written down as the intake (for the run's record), kept with the record, then started.
    [intent] = workflows.intents.values()
    assert intent["source"] == StartSource("record", record_of, state_key=state_key("claim_intake"))
    assert workflows.starters == [INTAKE] and workflow_starter() is None
    handed_back = answers(events, "claim_intake")
    run_id = Outbox.run_id(app.name, started)
    # Handed back as it was kept, before it started; a chat follows the run by its call ID.
    assert handed_back[0]["workflows"] == [
        {"workflow": "File claim", "call_id": started, "run_id": run_id, "status": "starting"}
    ]
    # Started once that was kept: the agent it handed back to is told where it got to.
    told = str(llm.requests[-1].config.system_instruction)
    assert "File claim (run" in told and "CLM-000001" in told
    kept = (await chat.state())[state_key("claim_intake")]
    assert kept["id"] == record_of
    assert kept["workflows"][0]["run_id"] == run_id and kept["workflows"][0]["status"] == "succeeded"


async def test_a_workflow_that_cant_start_doesnt_stop_the_intake_completing(models, llm) -> None:
    services = RuntimeServices(workflows=FileClaim(fails=True))
    app = await build_app(parse_document(filing(confirm_summary=False)), models=models, services=services)
    chat = Chat(app)
    llm.turns = [
        [call("claim_intake", full_name="Dana Li")],
        [call("record_answers", answers={"email": "dana@x.co", "claim_type": "Auto"})],
        [call("finish_task", status="completed")],
        [text("Your claim is recorded; filing it didn't start.")],
    ]
    handed_back = answers(await chat.say(text("Claim")), "claim_intake")
    assert handed_back[0]["status"] == "completed"
    [workflow] = (await chat.state())[state_key("claim_intake")]["workflows"]
    assert (workflow["workflow"], workflow["status"], workflow["error"]) == (
        "File claim",
        "refused",
        "Its start needs a policy number.",
    )
    told = str(llm.requests[-1].config.system_instruction)
    assert "Its start needs a policy number." in told


async def test_only_workflows_start_when_it_completes(models) -> None:
    wrong = filing()
    wrong["nodes"][-1] = {"id": "wf_file", "kind": "memory", "name": "Memory", "config": {}}
    with pytest.raises(
        DocumentError, match="'Claim intake' starts 'Memory' when it completes, which isn't a workflow"
    ):
        parse_document(wrong)
    # Only the hosted runtime runs workflows.
    with pytest.raises(BuildError, match="'File claim' runs a workflow, which only the hosted runtime can"):
        await build_app(parse_document(filing()), models=models)


# Conversations -----------------------------------------------------------------


async def test_an_intake_collects_its_record_in_any_order_and_hands_it_back(models, llm) -> None:
    app = await build_app(parse_document(document()), models=models)
    chat = Chat(app)
    llm.turns = [
        # The front desk hands off with what it knows.
        [call("claim_intake", full_name="Dana Li", background="A car accident.")],
        # The person front-loads: the plate before the type that unlocks it.
        [
            call(
                "record_answers",
                answers={"vehicle_plate": "ABC123", "claim_type": "auto", "email": "dana@x.co"},
            )
        ],
        [text("When did it happen?")],
        # Recorded beside another call, finishing waits for it; then it isn't done.
        [
            call("record_answers", inferred={"date_of_loss": "2026-10-15"}),
            call("finish_task", status="completed"),
        ],
        [call("finish_task", status="completed")],
        [text("Yesterday, the 15th? And here's what I have: … Is that right?")],
        [call("record_answers", confirmed=["date_of_loss"], skipped=["notes"], summary_confirmed=True)],
        [call("finish_task", status="completed", record={"email": "made@up.example"})],
        [text("Your claim is in.")],
    ]

    first = await chat.say(text("I had a car accident"))
    assert [e.author for e in first if e.content] == [
        "front_desk",
        "claim_intake",
        "claim_intake",
        "claim_intake",
    ]
    # What the model's told before each call: the record, and what to ask next.
    brief = str(llm.requests[2].config.system_instruction)
    assert "# The intake: Claim intake" in brief
    assert '- Full name (full_name; text; required): "Dana Li" (from the agent that handed off)' in brief
    assert "What the agent that handed off said: A car accident." in brief
    # It didn't see the conversation before the hand-off; what the person said there comes with it.
    assert "What the person said before you were handed this" in brief
    assert "\n- I had a car accident" in brief
    assert "Ask next: Date of loss (date_of_loss)" in brief
    # An intake's questions get no follow-ups.
    assert (first[-1].custom_metadata or {}).get(QUIET_KEY) is True

    second = await chat.say(text("Yesterday"))
    assert calls(second, "finish_task") == [
        {
            "status": "completed",
            "note": "",
            "record": {
                "full_name": "Dana Li",
                "email": "dana@x.co",
                "claim_type": "Auto",
                "date_of_loss": "2026-10-15",
                "vehicle_plate": "ABC123",
            },
            "missing": [],
            "workflows": [],
        }
    ]
    assert answers(second, "finish_task")[0]["error"].startswith(
        "It isn't done: waiting for the person's yes: date_of_loss."
    )

    third = await chat.say(text("Yes, that's right"))
    handed_back = answers(third, "claim_intake")
    # The record the state kept, not what the model typed.
    assert handed_back == [
        {
            "status": "completed",
            "note": "",
            "record": {
                "full_name": "Dana Li",
                "email": "dana@x.co",
                "claim_type": "Auto",
                "date_of_loss": "2026-10-15",
                "vehicle_plate": "ABC123",
            },
            "missing": [],
            "workflows": [],
        }
    ]
    assert third[-1].author == "front_desk"
    kept = (await chat.state())[state_key("claim_intake")]
    assert kept["status"] == "completed"
    assert kept["answers"]["date_of_loss"] == {"value": "2026-10-15", "how": "confirmed"}
    assert kept["answers"]["notes"] == {"value": None, "how": "skipped"}


async def test_an_interrupted_intake_carries_on_when_handed_off_again(models, llm) -> None:
    app = await build_app(parse_document(document()), models=models)
    chat = Chat(app)
    llm.turns = [
        [call("claim_intake", full_name="Dana")],
        [call("record_answers", answers={"full_name": "Dana Li", "email": "dana@x.co"})],
        [text("What kind of claim is it?")],
        # The person asks something else: it hands back, keeping what it has.
        [call("finish_task", status="interrupted", note="They asked about their premium.")],
        [text("Your premium is due on the 1st. Shall we carry on with the claim?")],
        # Handed off again: what the person said stays, over what's handed in.
        [call("claim_intake", full_name="D. Li")],
        [text("Picking up where we were: what kind of claim is it?")],
    ]
    await chat.say(text("I need to make a claim"))
    aside = await chat.say(text("When's my premium due?"))
    assert answers(aside, "claim_intake") == [
        {
            "status": "interrupted",
            "note": "They asked about their premium.",
            "record": {"full_name": "Dana Li", "email": "dana@x.co"},
            "missing": ["claim_type", "date_of_loss"],
            "workflows": [],
        }
    ]
    await chat.say(text("Yes"))
    kept = (await chat.state())[state_key("claim_intake")]
    assert kept["status"] == "open"
    assert kept["answers"]["full_name"] == {"value": "Dana Li", "how": "stated"}
    assert "Ask next: Claim type (claim_type), Date of loss (date_of_loss)" in str(
        llm.requests[-1].config.system_instruction
    )


async def test_what_it_records_is_checked_against_the_design(models, llm) -> None:
    app = await build_app(parse_document(document(allow_cancel=False)), models=models)
    chat = Chat(app)
    llm.turns = [
        [call("claim_intake")],
        [
            call(
                "record_answers",
                answers={"email": "not an email", "colour": "red", "claim_type": "Home"},
                inferred={"email": "dana@x.co"},
                skipped=["claim_type"],
                summary_confirmed=True,
            )
        ],
        [call("finish_task", status="cancelled")],
        [text("Sorry, I can't stop here. What's your email?")],
    ]
    events = await chat.say(text("Claim, please"))
    recorded = answers(events, "record_answers")[0]
    assert recorded["recorded"] == ["claim_type"]
    assert recorded["refused"] == {
        "email": "it can't be inferred: ask the person",
        "colour": "it isn't one of the intake's fields",
        "claim_type": "it's required, so it can't be skipped",
        "summary_confirmed": "the record changed: read back the summary again",
    }
    assert answers(events, "finish_task") == [{"error": "It ends as one of: completed, interrupted."}]


async def test_an_intake_can_ask_with_a_form(models, llm) -> None:
    doc = document(fields=[{"key": "claim_type", "type": "choice", "options": ["Auto", "Home"]}], sections=[])
    app = await build_app(parse_document(doc), models=models)
    chat = Chat(app)
    schema = {"type": "string", "enum": ["Auto", "Home"]}
    llm.turns = [
        [call("claim_intake")],
        [call("adk_request_input", message="What kind of claim?", response_schema=schema)],
        [call("record_answers", answers={"claim_type": "Home"})],
        [text("Is a home claim right?")],
    ]
    asked = await chat.say(text("Claim"))
    request = next(c for e in asked for c in e.get_function_calls() if c.name == "adk_request_input")
    assert any(request.id in (e.long_running_tool_ids or ()) for e in asked)
    answered = await chat.say(
        types.Part(
            function_response=types.FunctionResponse(
                id=request.id, name="adk_request_input", response={"result": "Home"}
            )
        )
    )
    assert [e.author for e in answered if e.content][-1] == "claim_intake"
    kept = (await chat.state())[state_key("claim_intake")]
    assert kept["answers"]["claim_type"] == {"value": "Home", "how": "stated"}


# The builder's preview -----------------------------------------------------------


def test_the_preview_shows_what_sample_answers_would_make() -> None:
    config = {"fields": FIELDS, "sections": SECTIONS, "confirm_summary": True}
    shown = preview(
        "Claim intake",
        config,
        {
            "full_name": "Dana",
            "email": {"value": "nope", "how": "stated"},
            "claim_type": {"value": "auto", "how": "stated"},
            "notes": {"value": None, "how": "skipped"},
        },
    )
    assert shown["problems"] == []
    assert shown["refused"] == {"email": "it's an email address"}
    assert shown["ask_next"] == ["email", "date_of_loss"]
    assert shown["locked"] == []
    assert shown["done"] is False
    assert shown["result"]["record"] == {"full_name": "Dana", "claim_type": "Auto"}
    assert set(shown["input_schema"]["properties"]) == {"background", "full_name"}
    assert "# The intake: Claim intake" in shown["brief"]

    broken = preview("X", {"fields": []}, {})
    assert broken["problems"] == ["'X' has no fields: add what it collects."]


# Chats ------------------------------------------------------------------------


async def test_a_chat_leaves_the_hand_off_open_and_its_cancellation_is_dropped(models, llm) -> None:
    executor = AgentExecutor(DocumentAgentSource(parse_document(document())), models=models)
    session = await executor.sessions.create_session(app_name="ca_desk", user_id="ada")

    async def say(*parts: types.Part) -> list[Event]:
        request = RunRequest(
            app_name="ca_desk",
            user_id="ada",
            session_id=session.id,
            new_message=types.Content(role="user", parts=list(parts)),
        )
        return [event async for event in executor.run(request)]

    llm.turns = [
        [call("claim_intake", full_name="Dana Li")],
        [text("What's your email?")],
        [call("record_answers", answers={"email": "dana@x.co"})],
        [text("Thanks. What kind of claim is it?")],
    ]
    first = await say(text("I need to make a claim"))
    handing = next(e for e in first if e.get_function_calls())
    delegation = handing.get_function_calls()[0]
    # Tagged where it's kept; long-running only for chats, ADK's own list untouched.
    assert (handing.custom_metadata or {})[TASK_CALLS_KEY] == [delegation.id]
    assert not handing.long_running_tool_ids
    assert to_wire(handing)["longRunningToolIds"] == [delegation.id]
    kept = await executor.sessions.get_session(app_name="ca_desk", user_id="ada", session_id=session.id)
    assert kept is not None
    assert open_task_calls(kept.events) == {delegation.id}
    wired = to_wire(kept)["events"]
    assert [
        e.get("longRunningToolIds") for e in wired if e.get("customMetadata", {}).get(TASK_CALLS_KEY)
    ] == [[delegation.id]]

    # A chat cancels what's still open with the next message: the task goes on.
    cancelled = types.Part(
        function_response=types.FunctionResponse(
            id=delegation.id, name="claim_intake", response={"cancelled": True}
        )
    )
    second = await say(cancelled, text("dana@x.co"))
    assert [e.author for e in second if e.content][-1] == "claim_intake"
    assert answers(second, "claim_intake") == []
    await executor.close()


def test_only_a_cancellation_of_an_open_hand_off_is_dropped() -> None:
    handed = Event(
        author="front_desk",
        content=types.Content(role="model", parts=[call("claim_intake")]),
        custom_metadata={TASK_CALLS_KEY: ["c1"]},
    )
    handed.content.parts[0].function_call.id = "c1"  # type: ignore[union-attr,index]
    other = types.Part(
        function_response=types.FunctionResponse(id="c2", name="lookup", response={"cancelled": True})
    )
    mine = types.Part(
        function_response=types.FunctionResponse(id="c1", name="claim_intake", response={"cancelled": True})
    )
    message = types.Content(role="user", parts=[mine, other, text("hi")])
    left = without_cancelled_tasks(message, [handed])
    assert left is not None and left.parts is not None
    assert [p.function_response.id if p.function_response else p.text for p in left.parts] == ["c2", "hi"]
    # Answered already: nothing's open, so it stays; and a message that's only it stays whole.
    answered = Event(author="user", content=types.Content(role="user", parts=[mine]))
    assert without_cancelled_tasks(message, [handed, answered]) is message
    alone = types.Content(role="user", parts=[mine])
    assert without_cancelled_tasks(alone, [handed]) is alone


# The example ------------------------------------------------------------------

ACME = FIXTURES / "acme-front-desk.chat-agent.json"


async def test_the_example_front_desk_hands_off_to_its_two_intakes(models) -> None:
    doc = load_document(ACME)
    # Its claim intake starts File claim when it completes: the hosted runtime runs it.
    app = await build_app(doc, models=models, services=RuntimeServices(workflows=FileClaim()))
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    assert [agent.name for agent in root.sub_agents] == ["claim_intake", "contact_update"]
    assert {getattr(tool, "name", "") for tool in root.tools} >= {"claim_intake", "contact_update"}
    # The claim intake reads its summary back as a table (Rich answers on its Tools way).
    claim_agent = root.sub_agents[0]
    assert isinstance(claim_agent, LlmAgent)
    assert "present" in [getattr(tool, "name", "") for tool in claim_agent.tools]

    claim = doc.node("intake_claim").config
    # An auto claim unlocks the vehicle; a home claim asks for the property instead.
    auto = preview("Claim intake", claim, {"claim_type": "Auto"})
    assert auto["problems"] == [] and "s_auto" not in auto["locked"]
    assert {"s_home", "s_travel"} <= set(auto["locked"])
    home = preview("Claim intake", claim, {"claim_type": "home", "damage_kinds": "water"})
    assert "property_address" in home["applying"] and "vehicle_plate" not in home["applying"]
    assert home["result"]["record"]["damage_kinds"] == ["Water"]

    contact = doc.node("intake_contact").config
    # Only what's changing is asked, and each new detail is confirmed.
    email = preview("Contact update", contact, {"policy_number": "PN-1", "what_to_update": ["Email"]})
    assert email["missing"] == ["new_email"]
    sent = preview(
        "Contact update",
        contact,
        {"policy_number": "PN-1", "what_to_update": ["Email"], "new_email": "a@b.co"},
    )
    assert (sent["missing"], sent["unconfirmed"], sent["done"]) == ([], ["new_email"], False)
