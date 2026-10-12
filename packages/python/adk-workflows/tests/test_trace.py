"""
A run's trace as its run page's Trace tab reads it (``trace.run_trace``): every
event of the run's invocation, with its step, what it said and did, and the
session's state before and after, read back from a SQLite session database.
"""

import asyncio
from pathlib import Path
from typing import Any

from google.genai import types

from forge_task_adk_workflows import trace
from forge_task_adk_workflows.trace import MAX_TEXT, run_trace

from .scripted_llm import ScriptedLlm
from .test_steps import ANY_START, APPROVAL, Sessions, agent, end, node, transform


def public(state: dict[str, Any]) -> dict[str, Any]:
    # Forge's own keys (a finish's endings) aside.
    return {key: value for key, value in state.items() if not key.startswith("forge:")}


def trace_of(ran: Any, document: dict[str, Any], invocation_id: str | None = None) -> dict[str, Any]:
    return run_trace(asyncio.run(ran.session()), document, invocation_id=invocation_id)


def test_each_event_says_its_step_output_and_what_it_set(tmp_path: Path) -> None:
    ran = asyncio.run(Sessions(tmp_path).run(APPROVAL, {"key": "k-1"}))
    found = trace_of(ran, APPROVAL)
    events = found["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    # The person's message first: no step's.
    message = events[0]
    assert (message["author"], message["role"], message["step"], message["steps"]) == ("user", "user", None, [])
    assert message["text"] == '{"key": "k-1"}'
    start = next(event for event in events if event["step"] == "start")
    assert start["own"] and start["has_output"] and start["output"] == {"key": "k-1"}
    assert start["state_delta"] == {"input": {"key": "k-1"}}
    shape = next(event for event in events if event["step"] == "shape")
    assert shape["output"] == {"loud": "K-1"} and shape["state_delta"] == {}
    # The approval asks through ADK's own call: listed as it is.
    review = next(event for event in events if event["step"] == "review")
    [call] = review["calls"]
    assert call["args"]["message"] == "Ship k-1?"
    assert found["invocation_id"]
    assert found["state_before"] == {}
    assert found["state_after"] == found["state"] == {"input": {"key": "k-1"}}
    assert (found["later_changes"], found["omitted"]) == (False, 0)


def test_an_answer_resumes_the_same_invocation_and_its_route_is_listed(tmp_path: Path) -> None:
    ran = asyncio.run(Sessions(tmp_path).run(APPROVAL, {"key": "k-1"}))

    async def approve() -> None:
        await ran.answer({"approved": True, "decided_by": "Lead", "comment": "go"})

    asyncio.run(approve())
    events = trace_of(ran, APPROVAL)["events"]
    answers = [response for event in events for response in event["responses"]]
    assert any(answer["response"].get("comment") == "go" for answer in answers)
    assert any(event["route"] == "approved" for event in events if event["step"] == "review")
    yes = [event for event in events if event["step"] == "yes" and event["has_output"]]
    assert yes and yes[-1]["output"] == {"outcome": "succeeded", "result": True}


def test_a_models_answer_carries_its_text_and_usage(tmp_path: Path) -> None:
    document = agent(
        [
            ANY_START,
            node(
                "write",
                "llm",
                {
                    "description": "",
                    "instruction": "Write a title.",
                    "model": {"provider": "", "name": ""},
                    "thinking_level": "",
                    "output_schema": None,
                    "include_contents": "default",
                    "disallow_transfer_to_parent": False,
                    "disallow_transfer_to_peers": False,
                    "max_output_tokens": None,
                    "sub_agents": [],
                    "mode": "single_turn",
                },
            ),
            end("done", "steps.write.output"),
        ],
        [("start", "next", "write"), ("write", "next", "done")],
    )
    llm = ScriptedLlm(turns=[[types.Part(text="A title")]])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"topic": "x"}, model=llm))
    events = trace_of(ran, document)["events"]
    [answer] = [event for event in events if event["step"] == "write" and event["text"]]
    assert answer["text"] == "A title" and answer["role"] == "model"


def test_a_failing_step_carries_its_error(tmp_path: Path) -> None:
    document = agent(
        [ANY_START, transform("boom", "$uppercase(1)"), end("done")],
        [("start", "next", "boom"), ("boom", "next", "done")],
    )
    ran = asyncio.run(Sessions(tmp_path).run(document, {}))
    events = trace_of(ran, document)["events"]
    failed = [event for event in events if event["error"]]
    assert failed and failed[-1]["step"] == "boom" and failed[-1]["error"]["message"]


def test_the_state_before_holds_what_earlier_runs_in_the_session_set(tmp_path: Path) -> None:
    document = agent([ANY_START, end("done", "input")], [("start", "next", "done")])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"n": 1}))
    first = trace_of(ran, document)
    asyncio.run(ran.send(types.Content(role="user", parts=[types.Part(text='{"n": 2}')])))
    session = asyncio.run(ran.session())
    second = run_trace(session, document)
    assert second["invocation_id"] != first["invocation_id"]
    assert public(second["state_before"]) == {"input": {"n": 1}}
    assert public(second["state_after"]) == {"input": {"n": 2}}
    # Forge's own keys are listed too: the first run's endings, before the second.
    assert any(key.startswith("forge:endings:") for key in second["state_before"])
    # The first run's, read now: a later run changed the state.
    again = run_trace(session, document, invocation_id=first["invocation_id"])
    assert public(again["state_after"]) == {"input": {"n": 1}} and again["later_changes"]
    assert [event["seq"] for event in again["events"]] == [event["seq"] for event in first["events"]]


def test_long_strings_are_cut_short_and_only_the_latest_events_listed(tmp_path: Path, monkeypatch: Any) -> None:
    document = agent([ANY_START, end("done", "input")], [("start", "next", "done")])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"long": "x" * (MAX_TEXT + 10)}))
    found = trace_of(ran, document)
    assert found["state"]["input"]["long"].endswith("(10 more characters)")
    whole = len(found["events"])
    monkeypatch.setattr(trace, "MAX_EVENTS", 2)
    cut = trace_of(ran, document)
    assert cut["omitted"] == whole - 2
    assert [event["seq"] for event in cut["events"]] == [whole - 1, whole]
    # What the left-out events set is in the state before.
    assert public(cut["state_before"]) == {"input": {"long": found["state"]["input"]["long"]}}


def test_before_its_session_the_trace_is_empty() -> None:
    assert run_trace(None, APPROVAL)["events"] == []


def test_the_files_a_runs_events_saved_say_their_step(tmp_path: Path) -> None:
    from google.adk.events import Event, EventActions
    from google.adk.events.event import NodeInfo
    from google.adk.sessions import Session

    document = agent([ANY_START, node("write", "llm", {}), end("done")], [("start", "next", "write")])
    saving = Event(
        invocation_id="e-1",
        author="write",
        actions=EventActions(artifact_delta={"summary.md": 0}),
        node_info=NodeInfo(path="steps@1/write@1"),
        timestamp=1_791_000_000.0,
    )
    other = saving.model_copy(update={"invocation_id": "e-2", "id": "x"})
    session = Session(id="s", app_name="a", user_id="u", events=[saving, other])

    assert trace.saved_files(session, document, invocation_id="e-1") == {
        ("summary.md", 0): {"step": "write", "author": "write", "at": "2026-10-03T04:00:00.000Z"}
    }
    assert trace.saved_files(None, document, invocation_id="e-1") == {}


def test_a_conversations_trace_lists_every_turn_or_only_those_asked_for(tmp_path: Path) -> None:
    document = agent([ANY_START, end("done", "input")], [("start", "next", "done")])
    ran = asyncio.run(Sessions(tmp_path).run(document, {"n": 1}))
    asyncio.run(ran.send(types.Content(role="user", parts=[types.Part(text='{"n": 2}')])))
    session = asyncio.run(ran.session())
    turns = list(dict.fromkeys(event.invocation_id for event in session.events))
    assert len(turns) == 2

    whole = trace.session_trace(session)
    assert [event["seq"] for event in whole["events"]] == list(range(1, len(session.events) + 1))
    assert {event["step"] for event in whole["events"]} == {None}
    assert whole["state_before"] == {} and public(whole["state_after"]) == {"input": {"n": 2}}

    first = trace.session_trace(session, invocations={turns[0]})
    assert all(event["text"] != '{"n": 2}' for event in first["events"])
    assert public(first["state_after"]) == {"input": {"n": 1}} and first["later_changes"]
