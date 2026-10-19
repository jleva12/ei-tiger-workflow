"""The workflows intakes start, started exactly once whatever races them: two
tabs submitting a request at once, a submit cut off as it starts its runs,
a turn whose completing call was never kept; and records tool calls made
together change, kept whole."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.plugins.base_plugin import BasePlugin

from forge_agent_runtime.build import build_app
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.intake import (
    Answer,
    Completion,
    IntakeDesign,
    IntakeEngine,
    IntakeRecord,
    state_key,
)
from forge_agent_runtime.requests import Intake, IntakeRequest, RequestDesk, SubmitRequests
from forge_agent_runtime.services import RequestConflict, RuntimeServices
from tests.conftest import call, text
from tests.outbox import Outbox
from tests.test_intake import Chat, FileClaim, filing

FIELDS = [
    {"key": "requester_email", "label": "Your email", "type": "email"},
    {"key": "app_name", "label": "Application"},
]


class Store:
    """Requests kept by the person, each changed only on the version it was
    read at; each save gives way first, so racing saves interleave."""

    def __init__(self) -> None:
        self.kept: dict[str, dict[str, Any]] = {}
        self.conflicts = 0

    async def requests(self, *, ids: Sequence[str] | None = None, status: str | None = None, **_: Any):
        await asyncio.sleep(0)
        return [
            copy.deepcopy(request)
            for request in self.kept.values()
            if (ids is None or request["id"] in ids) and (status is None or request["status"] == status)
        ]

    async def save(self, requests: Sequence[Mapping[str, Any]], **_: Any) -> None:
        await asyncio.sleep(0)
        for request in requests:
            there = self.kept.get(request["id"])
            if (there["version"] if there is not None else None) != request.get("version"):
                self.conflicts += 1
                raise RequestConflict(f"Request {request['id']} changed since it was read.")
        for request in requests:
            version = request.get("version")
            self.kept[request["id"]] = {
                **copy.deepcopy(dict(request)),
                "version": 0 if version is None else version + 1,
            }


def context(session_id: str) -> Any:
    """A request tool's call, in a conversation of Ada's with the desk."""
    return SimpleNamespace(
        session=SimpleNamespace(app_name="ca_desk", id=session_id, state={}),
        user_id="ada",
        state={},
    )


def desk(outbox: Outbox, store: Store) -> tuple[RequestDesk, str]:
    """A desk with one intake whose submitted requests open a ticket, and one ready request of Ada's."""
    engine = IntakeEngine(
        IntakeDesign.of("Access request", {"fields": FIELDS}),
        "access_request",
        completions=[Completion(name="Open ticket", ref="ag_ticket", wait=0.0)],
        runner=outbox,  # type: ignore[arg-type]
    )
    made = RequestDesk(
        [Intake(id="intake_access", title="Access request", description="", engine=engine)], store=store
    )
    request = IntakeRequest(
        id="req-1",
        intake="intake_access",
        title="Access request",
        basket="b1",
        session_id="tab-1",
        record=IntakeRecord(
            answers={"requester_email": Answer(value="ada@x.co"), "app_name": Answer(value="Ledger")},
            # Read back, and said yes to.
            summary_confirmed=True,
        ),
    )
    store.kept[request.id] = {**request.model_dump(mode="json"), "version": 3}
    return made, request.id


async def test_two_tabs_submitting_a_request_at_once_start_its_runs_once() -> None:
    outbox, store = Outbox(), Store()
    requests, request_id = desk(outbox, store)
    tool = SubmitRequests(requests)
    one, two = await asyncio.gather(
        tool.run_async(args={"requests": [request_id]}, tool_context=context("tab-1")),
        tool.run_async(args={"requests": [request_id]}, tool_context=context("tab-2")),
    )
    # One run, under the ID the request makes, whichever tab got there first.
    assert len(outbox.runs) == 1
    assert outbox.runs[0][2] == f"request-{request_id}-0"
    assert list(outbox.intents) == [Outbox.run_id("ca_desk", f"request-{request_id}-0")]
    assert store.kept[request_id]["status"] == "submitted"
    # Each tab says it's in: one submitted it, the other found it submitted (or finished it).
    said = [len(answer["submitted"]) + len(answer.get("already_submitted", [])) for answer in (one, two)]
    assert said == [1, 1]


class Stalling(Outbox):
    """Starting a run waits until it's let go: the turn can be cut off meanwhile."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.go = asyncio.Event()

    async def dispatch(self, run_id: str, *, organization_id: str | None, wait: float) -> dict[str, Any]:
        self.entered.set()
        await self.go.wait()
        return await super().dispatch(run_id, organization_id=organization_id, wait=wait)


async def test_a_submit_cut_off_as_it_starts_runs_is_finished_by_the_next_once() -> None:
    outbox, store = Stalling(), Store()
    requests, request_id = desk(outbox, store)
    tool = SubmitRequests(requests)
    first = asyncio.create_task(tool.run_async(args={}, tool_context=context("tab-1")))
    await asyncio.wait_for(outbox.entered.wait(), 5)
    # The person closes the tab: the turn's cancelled as it starts the run.
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Kept as submitting, its run written down, not started: never open again.
    assert store.kept[request_id]["status"] == "submitting"
    assert [intent["status"] for intent in outbox.intents.values()] == ["intended"]
    assert outbox.runs == []
    # Submitted again (a new call, with an ID of its own): the run it wrote down starts, once.
    outbox.go.set()
    again = await tool.run_async(args={}, tool_context=context("tab-1"))
    assert [request["id"] for request in again["submitted"]] == [request_id]
    assert len(outbox.runs) == 1 and len(outbox.intents) == 1
    assert store.kept[request_id]["status"] == "submitted"
    # And again: nothing more.
    later = await tool.run_async(args={"requests": [request_id]}, tool_context=context("tab-1"))
    assert later["submitted"] == [] and later["already_submitted"][0]["status"] == "submitted"
    assert len(outbox.runs) == 1


class LosesTheFirstFinish(BasePlugin):
    """The model's finish_task call isn't kept the first time (the tab closed,
    the session went stale): the turn fails before the call is kept."""

    def __init__(self) -> None:
        super().__init__(name="loses_the_first_finish")
        self.lost = False

    async def on_event_callback(self, *, invocation_context: Any, event: Any) -> Any:
        if not self.lost and any(c.name == "finish_task" for c in event.get_function_calls()):
            self.lost = True
            raise ConnectionError("The conversation's write failed.")
        return None


async def test_a_completion_whose_call_wasnt_kept_starts_its_runs_once(models, llm) -> None:
    workflows = FileClaim()
    app = await build_app(
        parse_document(filing(confirm_summary=False)),
        models=models,
        services=RuntimeServices(workflows=workflows),
    )
    losing = LosesTheFirstFinish()
    app.plugins.append(losing)
    chat = Chat(app)
    llm.turns = [
        [call("claim_intake", full_name="Dana Li")],
        [call("record_answers", answers={"email": "dana@x.co", "claim_type": "Auto"})],
        [call("finish_task", status="completed")],
    ]
    with pytest.raises(RuntimeError):
        await chat.say(text("I had a car accident"))
    assert losing.lost
    # Its runs were written down, never started: the call that'd start them wasn't kept.
    assert len(workflows.intents) == 1 and workflows.runs == []
    assert not (await chat.state())[state_key("claim_intake")]["workflows"]

    # The person says it again: the model calls finish_task again, with a call ID of its own.
    llm.turns = [[call("finish_task", status="completed")], [text("Your claim is in.")]]
    await chat.say(text("Did it go through?"))
    # The same run (made from the record, not the call), started once.
    assert len(workflows.intents) == 1
    assert len(workflows.runs) == 1
    kept = (await chat.state())[state_key("claim_intake")]
    assert [run["status"] for run in kept["workflows"]] == ["succeeded"]


async def test_a_field_one_call_cleared_while_another_wrote_stays_cleared(models, llm) -> None:
    app = await build_app(
        parse_document(filing(confirm_summary=False)),
        models=models,
        services=RuntimeServices(workflows=FileClaim()),
    )
    chat = Chat(app)
    llm.turns = [
        [call("claim_intake", full_name="Dana Li")],
        [call("record_answers", answers={"email": "dana@x.co"})],
        [text("What kind of claim is it?")],
    ]
    await chat.say(text("Claim, please"))
    # Two calls in one answer: one records a value, the next clears the email (it was wrong).
    llm.turns = [
        [
            call("record_answers", answers={"claim_type": "Auto"}),
            call("record_answers", cleared=["email"]),
        ],
        [text("What's your email?")],
    ]
    await chat.say(text("It's Auto, and that email's wrong"))
    kept = (await chat.state())[state_key("claim_intake")]
    # ADK deep-merges the calls' writes: without keeping the record whole, the email came back.
    assert "email" not in kept["answers"]
    assert kept["answers"]["claim_type"]["value"] == "Auto"
    assert kept["answers"]["full_name"]["value"] == "Dana Li"
