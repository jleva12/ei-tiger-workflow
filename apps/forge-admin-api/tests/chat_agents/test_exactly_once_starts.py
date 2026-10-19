"""The workflow runs chat agents start, each exactly once, without MySQL,
MongoDB or Redis (SQLite, a connection per caller): requests change only on
the version they were read at; an intake's runs are written down, then
started once however often they're asked for; the upkeep starts those a
stopped turn left and drops those whose change was never kept; and a
Workflow tool call made again after its turn stopped finds its run."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_agent_runtime.intake import STARTING
from forge_agent_runtime.leases import TurnKey
from forge_agent_runtime.services import RequestConflict, StartSource, WorkflowStart
from forge_task_adk_workflows.run_store import RunStore, metadata
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlite_tables import create
from test_workflow_progress import AGENT, ORG, SHIPPING, Agents, Queue

from forge_admin.chat_agents import source
from forge_admin.chat_agents.requests import OrganizationRequests
from forge_admin.chat_agents.source import (
    OrganizationWorkflows,
    chat_run_id,
    start_run_id,
)
from forge_admin.chat_agents.starts import StartIntents, StartUpkeep
from forge_admin.chat_agents.turn_leases import SqlTurnLeases
from forge_admin.db.audit import utc_now
from forge_admin.models import ChatTurnLease, IntakeRequest, WorkflowStartIntent

pytestmark = pytest.mark.asyncio

ADA = {"organization_id": ORG, "agent_id": AGENT, "user_id": "ada"}
SHIP = SHIPPING["id"]


@asynccontextmanager
async def world(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    await create(engine, IntakeRequest, WorkflowStartIntent, ChatTurnLease)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    queue = Queue()
    intents = StartIntents(sessions)
    workflows = OrganizationWorkflows(
        Agents(),  # type: ignore[arg-type]
        RunStore(engine),
        queue,  # type: ignore[arg-type]
        intents=intents,
    )
    chats = InMemorySessionService()
    try:
        yield SimpleNamespace(
            engine=engine,
            sessions=sessions,
            queue=queue,
            intents=intents,
            workflows=workflows,
            requests=OrganizationRequests(sessions),
            chats=chats,
            upkeep=StartUpkeep(workflows, intents, sessions, chats, grace=0),
            leases=SqlTurnLeases(sessions),
        )
    finally:
        await engine.dispose()


def turn(session_id: str = "s-1", call_id: str = "call_1", events: Any = ()) -> Any:
    """A tool call's (or callback's) context, in Ada's conversation with the agent."""
    return SimpleNamespace(
        session=SimpleNamespace(app_name=AGENT, id=session_id, events=list(events)),
        function_call_id=call_id,
        invocation_id="i-now",
        user_id="ada",
        agent_name="concierge",
    )


def request(request_id: str = "r-1", **changes: Any) -> dict[str, Any]:
    """A request as the runtime saves it."""
    return {
        "id": request_id,
        "intake": "intake_access",
        "title": "Access request",
        "basket": "b-1",
        "session_id": "s-1",
        "status": "open",
        "record": {"answers": {"key": {"value": "k-1"}}},
        **changes,
    }


# Requests: compare-and-set --------------------------------------------------------


async def test_a_request_changes_only_on_the_version_it_was_read_at(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        await w.requests.save([request()], **ADA)
        [kept] = await w.requests.requests(**ADA)
        assert kept["version"] == 0
        await w.requests.save([request(version=0, title="Access")], **ADA)
        # A change made to what it read before: refused, and nothing changes.
        with pytest.raises(RequestConflict):
            await w.requests.save([request(version=0, title="Stale")], **ADA)
        [kept] = await w.requests.requests(**ADA)
        assert (kept["title"], kept["version"]) == ("Access", 1)
        # All of a save or none of it.
        await w.requests.save([request("r-2")], **ADA)
        with pytest.raises(RequestConflict):
            await w.requests.save(
                [
                    request("r-2", version=0, status="cancelled"),
                    request(version=0, status="cancelled"),
                ],
                **ADA,
            )
        assert {r["status"] for r in await w.requests.requests(**ADA)} == {"open"}
        # Someone else's isn't theirs to change.
        with pytest.raises(LookupError):
            await w.requests.save([request(version=1)], **{**ADA, "user_id": "mallory"})
        # A new one under a kept one's ID is a conflict, not an overwrite.
        with pytest.raises(RequestConflict):
            await w.requests.save([request("r-2")], **ADA)


async def test_submits_racing_move_a_request_from_open_once(tmp_path: Path) -> None:
    async with world(tmp_path) as w:
        await w.requests.save([request()], **ADA)

        async def submit() -> bool:
            try:
                await w.requests.save([request(version=0, status="submitting")], **ADA)
            except RequestConflict:
                return False
            return True

        made = await asyncio.gather(*(submit() for _ in range(5)))
        assert sum(made) == 1
        [kept] = await w.requests.requests(**ADA)
        assert (kept["status"], kept["version"]) == ("submitting", 1)


# Intakes' starts: written down, then started once ---------------------------------


def start(call_id: str = "request-r-1-0", value: Any = "k-1") -> WorkflowStart:
    return WorkflowStart(
        call_id=call_id, ref=SHIP, name="Ship", input={"key": value} if value else {}
    )


async def test_an_intakes_run_is_written_down_then_started_once(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        [run_id] = await w.workflows.intend(
            [start()],
            organization_id=ORG,
            context=turn(),
            source=StartSource("request", "r-1"),
        )
        # Made from the agent and the request, not the conversation or a model's call.
        assert run_id == start_run_id(AGENT, "request-r-1-0")
        # Written down: nothing started yet.
        assert await w.workflows.runs.get(run_id) is None
        assert (await w.intents.get(run_id))["status"] == "intended"
        # Written down again (its change wasn't kept, and it's asked again): the same.
        assert await w.workflows.intend(
            [start()],
            organization_id=ORG,
            context=turn(session_id="s-2"),
            source=StartSource("request", "r-1"),
        ) == [run_id]
        # Started, by however many at once: one run, queued once.
        answers = await asyncio.gather(
            *(
                w.workflows.dispatch(run_id, organization_id=ORG, wait=0)
                for _ in range(4)
            )
        )
        assert {answer["run_id"] for answer in answers} == {run_id}
        assert w.queue.queued == [run_id]
        assert (await w.intents.get(run_id))["status"] == "dispatched"
        run = await w.workflows.runs.get(run_id)
        assert run["payload"]["trigger"]["call_id"] == "request-r-1-0"
        assert run["payload"]["trigger"]["session_id"] == "s-2"
        # Its conversation follows it by its call ID.
        followed = await w.workflows.progress(
            agent_id=AGENT,
            user_id="ada",
            session_id="s-2",
            call_id="request-r-1-0",
            organization_id=ORG,
        )
        assert followed is not None and followed["run_id"] == run_id
        # Started again later: the run, not another.
        again = await w.workflows.dispatch(run_id, organization_id=ORG, wait=0)
        assert again["run_id"] == run_id and w.queue.queued == [run_id]


async def test_a_start_its_workflow_refuses_is_answered_not_raised(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        [run_id] = await w.workflows.intend(
            [start(value=None)],
            organization_id=ORG,
            context=turn(),
            source=StartSource("request", "r-1"),
        )
        answer = await w.workflows.dispatch(run_id, organization_id=ORG, wait=0)
        assert answer["status"] == "refused" and "doesn't fit" in answer["error"]
        assert await w.workflows.runs.get(run_id) is None and w.queue.queued == []
        # Not tried again by the upkeep.
        assert (await w.intents.get(run_id))["status"] == "dispatched"


# The upkeep ------------------------------------------------------------------------


async def test_the_upkeep_starts_a_submitted_requests_runs_its_turn_left(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        source_of = StartSource("request", "r-1")
        [run_id] = await w.workflows.intend(
            [start()], organization_id=ORG, context=turn(), source=source_of
        )
        # The turn kept the request as submitting, then stopped.
        workflows = [
            {
                "workflow": "Ship",
                "call_id": "request-r-1-0",
                "run_id": run_id,
                "status": STARTING,
            }
        ]
        await w.requests.save(
            [request(status="submitting", record={"workflows": workflows})], **ADA
        )
        assert await w.upkeep.once() == 1
        assert w.queue.queued == [run_id]
        [kept] = await w.requests.requests(**ADA)
        assert kept["status"] == "submitted"
        assert kept["record"]["workflows"][0]["status"] == "queued"
        # Looked at again: nothing more.
        assert await w.upkeep.once() == 0 and w.queue.queued == [run_id]


async def test_the_upkeep_drops_a_start_whose_change_was_never_kept(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        # Written down, but the request stayed open: its submit's change failed.
        await w.requests.save([request()], **ADA)
        [run_id] = await w.workflows.intend(
            [start()],
            organization_id=ORG,
            context=turn(),
            source=StartSource("request", "r-1"),
        )
        assert await w.upkeep.once() == 1
        assert (await w.intents.get(run_id))["status"] == "dropped"
        assert await w.workflows.runs.get(run_id) is None and w.queue.queued == []


async def test_the_upkeep_starts_a_kept_records_runs_and_waits_for_a_running_turn(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as w:
        key = "forge:intake:claim_intake"
        kept_record = {
            "id": "rec-1",
            "workflows": [{"call_id": "intake-rec-1-0", "status": STARTING}],
        }
        [kept_run] = await w.workflows.intend(
            [start("intake-rec-1-0")],
            organization_id=ORG,
            context=turn(),
            source=StartSource("record", "rec-1", state_key=key),
        )
        kept_record["workflows"][0]["run_id"] = kept_run
        await w.chats.create_session(
            app_name=AGENT, user_id="ada", session_id="s-1", state={key: kept_record}
        )
        # Another record's, in a conversation whose turn still runs: not yet.
        [lost_run] = await w.workflows.intend(
            [start("intake-rec-2-0")],
            organization_id=ORG,
            context=turn(session_id="s-2"),
            source=StartSource("record", "rec-2", state_key=key),
        )
        conversation = TurnKey(AGENT, "ada", "s-2")
        assert await w.leases.acquire(conversation, owner="turn", seconds=60)
        assert await w.upkeep.once() == 1
        assert w.queue.queued == [kept_run]
        assert (await w.intents.get(lost_run))["status"] == "intended"
        # Its turn ended without keeping it: dropped, once its claim runs out.
        await w.leases.release(conversation, owner="turn")
        w.intents.clock = lambda: utc_now() + timedelta(minutes=5)
        assert await w.upkeep.once() == 1
        assert (await w.intents.get(lost_run))["status"] == "dropped"
        assert w.queue.queued == [kept_run]


# The Workflow tool -----------------------------------------------------------------


def called(call_id: str, invocation: str, **args: Any) -> Event:
    return Event(
        invocation_id=invocation,
        author="concierge",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id=call_id, name="ship", args=args)
                )
            ],
        ),
    )


def answered(call_id: str, invocation: str) -> Event:
    return Event(
        invocation_id=invocation,
        author="concierge",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id, name="ship", response={"status": "queued"}
                    )
                )
            ],
        ),
    )


async def test_a_call_made_again_after_its_turn_stopped_finds_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source, "WORKFLOW_WAIT", 0.0)
    async with world(tmp_path) as w:
        first = await w.workflows.run(
            SHIP,
            {"key": "k-1"},
            organization_id=ORG,
            context=turn(events=[called("call_1", "i-1", key="k-1")]),
        )
        assert first["run_id"] == chat_run_id(AGENT, "ada", "s-1", "call_1")
        # The turn stopped as it waited: the call's never answered, and the
        # chat drops it; the model calls the workflow again in the next turn.
        events = [
            called("call_1", "i-1", key="k-1"),
            called("call_2", "i-now", key="k-1"),
        ]
        again = await w.workflows.run(
            SHIP,
            {"key": "k-1"},
            organization_id=ORG,
            context=turn(call_id="call_2", events=events),
        )
        assert again["run_id"] == first["run_id"] and w.queue.queued == [
            first["run_id"]
        ]
        # Another input is another run; so is a call again after the first was answered.
        other = await w.workflows.run(
            SHIP,
            {"key": "k-2"},
            organization_id=ORG,
            context=turn(
                call_id="call_3", events=[*events, called("call_3", "i-now", key="k-2")]
            ),
        )
        assert other["run_id"] != first["run_id"]
        done = [
            called("call_1", "i-1", key="k-1"),
            answered("call_1", "i-1"),
            called("call_4", "i-now", key="k-1"),
        ]
        fresh = await w.workflows.run(
            SHIP,
            {"key": "k-1"},
            organization_id=ORG,
            context=turn(call_id="call_4", events=done),
        )
        assert fresh["run_id"] == chat_run_id(AGENT, "ada", "s-1", "call_4")
        assert len(w.queue.queued) == 3
