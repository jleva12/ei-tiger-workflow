"""A chat agent's workflow calls, followed from the chat
(``OrganizationWorkflows``), without MySQL, MongoDB or Redis: the run a call
starts is named by the conversation and the call, its steps are read from the
run's ADK session, and only the conversation's person follows it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from forge_task_adk_workflows.run_store import RunStore, metadata
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.build import build_agent
from forge_admin.adk_workflows.runs import PreparedRun, start_adk_run
from forge_admin.chat_agents import source
from forge_admin.chat_agents.source import OrganizationWorkflows, chat_run_id

ORG = "3f6c0000-0000-4000-8000-000000000001"
AGENT = "ca_concierge1"


def node(
    node_id: str, kind: str, config: dict[str, Any], outputs: list[str]
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": node_id.title(),
        "config": config,
        "outputs": outputs,
    }


#: Shapes a key, then waits for an approval before it ships.
SHIPPING = {
    "format": "forge.agent/v1",
    "id": "ag_ship000001",
    "name": "Ship",
    "description": "Ships an order.",
    "nodes": [
        node(
            "start",
            "start",
            {
                "input_schema": {
                    "type": "object",
                    "required": ["key"],
                    "properties": {"key": {"type": "string"}},
                }
            },
            ["next"],
        ),
        node(
            "shape",
            "transform",
            {"expression": '{"loud": $uppercase(input.key)}', "output_schema": {}},
            ["next"],
        ),
        node(
            "review",
            "approval",
            {"message": "Ship it?", "approvers": "org:admin", "timeout_hours": 0},
            ["approved", "rejected"],
        ),
        node("yes", "end", {"outcome": "succeeded", "result": ""}, []),
        node("no", "end", {"outcome": "succeeded", "result": ""}, []),
    ],
    "edges": [
        {"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t}
        for s, o, t in (
            ("start", "next", "shape"),
            ("shape", "next", "review"),
            ("review", "approved", "yes"),
            ("review", "rejected", "no"),
        )
    ],
    "layout": {},
}


class Agents:
    """Stands in for the organization's workflows: the one, its draft."""

    async def get(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
        if (organization_id, agent_id) != (ORG, SHIPPING["id"]):
            return None
        return {
            "_id": agent_id,
            "organization_id": ORG,
            "revision": 2,
            "document": SHIPPING,
            "has_draft": True,
            "published_version": None,
        }


class Queue:
    def __init__(self) -> None:
        self.queued: list[str] = []

    async def run_adk(self, run_id: str) -> None:
        self.queued.append(run_id)


def call(session_id: str = "s-1", call_id: str = "call_1", user: str = "ada") -> Any:
    """The tool context of a model's call of the workflow, in a conversation."""
    return SimpleNamespace(
        session=SimpleNamespace(app_name=AGENT, id=session_id),
        function_call_id=call_id,
        user_id=user,
        agent_name="concierge",
    )


@asynccontextmanager
async def world() -> AsyncIterator[
    tuple[OrganizationWorkflows, Queue, InMemorySessionService]
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    queue, sessions = Queue(), InMemorySessionService()
    workflows = OrganizationWorkflows(
        Agents(),  # type: ignore[arg-type]
        RunStore(engine),
        queue,  # type: ignore[arg-type]
        run_sessions=sessions,
    )
    try:
        yield workflows, queue, sessions
    finally:
        await engine.dispose()


async def run_to_the_approval(
    sessions: InMemorySessionService, run: dict[str, Any]
) -> None:
    """Run the workflow in the run's session, as the worker does, to its approval."""
    payload = run["payload"]
    await sessions.create_session(
        app_name="adk_workflows",
        user_id=payload["run_as"],
        session_id=run["session_id"],
    )
    runner = Runner(
        node=build_agent(SHIPPING), session_service=sessions, app_name="adk_workflows"
    )
    message = types.Content(role="user", parts=[types.Part(text='{"key": "k-1"}')])
    async for _ in runner.run_async(
        user_id=payload["run_as"],
        session_id=run["session_id"],
        new_message=message,
        invocation_id="e-this-run",
    ):
        pass


def test_a_call_starts_the_run_its_conversation_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source, "WORKFLOW_WAIT", 0.0)

    async def check() -> None:
        async with world() as (workflows, queue, sessions):
            await a_call_starts_the_run_its_conversation_follows(
                workflows, queue, sessions
            )

    asyncio.run(check())


async def a_call_starts_the_run_its_conversation_follows(
    workflows: OrganizationWorkflows, queue: Queue, sessions: InMemorySessionService
) -> None:
    ref = SHIPPING["id"]

    answer = await workflows.run(
        ref, {"key": "k-1"}, organization_id=ORG, context=call()
    )
    run_id = chat_run_id(AGENT, "s-1", "call_1")
    assert answer == {"run_id": run_id, "status": "queued"}
    assert queue.queued == [run_id]
    run = await workflows.runs.get(run_id)
    assert run is not None
    assert run["payload"]["trigger"] == {
        "type": "chat_agent",
        "agent_id": AGENT,
        "user": "ada",
        "session_id": "s-1",
        "call_id": "call_1",
    }
    # The same call answered again finds its run; another call starts another.
    again = await workflows.run(
        ref, {"key": "k-1"}, organization_id=ORG, context=call()
    )
    assert again["run_id"] == run_id and queue.queued == [run_id]
    other = await workflows.run(
        ref, {"key": "k-2"}, organization_id=ORG, context=call(call_id="call_2")
    )
    assert other["run_id"] != run_id

    follow = {
        "agent_id": AGENT,
        "user_id": "ada",
        "session_id": "s-1",
        "call_id": "call_1",
        "organization_id": ORG,
    }
    # Queued: only its start shows, where it begins.
    queued = await workflows.progress(**follow)
    assert queued is not None
    assert (queued["run_id"], queued["workflow"], queued["status"]) == (
        run_id,
        "Ship",
        "queued",
    )
    assert [(s["id"], s["status"]) for s in queued["steps"]] == [
        ("start", "not_reached")
    ]
    assert queued["created_at"] and queued["started_at"] is None

    # As the worker runs it: the steps it reached, with their times, and none
    # of what they were handed or handed on.
    await run_to_the_approval(sessions, run)
    await workflows.runs.claim(run_id, owner="w1", lease_seconds=60)
    await workflows.runs.checkpoint(
        run_id,
        owner="w1",
        state={
            "adk_workflow": {
                "session_id": run["session_id"],
                "invocation_id": "e-this-run",
            }
        },
    )
    going = await workflows.progress(**follow)
    assert going is not None and going["status"] == "running"
    assert [(s["id"], s["status"]) for s in going["steps"]] == [
        ("start", "done"),
        ("shape", "done"),
        ("review", "waiting"),
    ]
    shape = going["steps"][1]
    assert set(shape) == {
        "id",
        "name",
        "kind",
        "status",
        "started_at",
        "finished_at",
        "handed_to",
    }
    assert shape["started_at"] and shape["finished_at"]
    assert going["steps"][2]["handed_to"] is True

    # Only the conversation's person, in that conversation, through that agent.
    for wrong in (
        {"user_id": "mallory"},
        {"session_id": "s-2"},
        {"agent_id": "ca_other"},
        {"call_id": "call_9"},
    ):
        assert await workflows.progress(**{**follow, **wrong}) is None
    assert (
        await workflows.progress(**{**follow, "organization_id": "another-org"}) is None
    )


def test_a_run_started_elsewhere_isnt_a_chats_to_follow() -> None:
    async def check() -> None:
        async with world() as (workflows, _, _):
            await a_run_started_elsewhere_isnt_a_chats_to_follow(workflows)

    asyncio.run(check())


async def a_run_started_elsewhere_isnt_a_chats_to_follow(
    workflows: OrganizationWorkflows,
) -> None:
    # A run whose ID happens to be the call's, but started by a member.
    record = await Agents().get(ORG, SHIPPING["id"])
    assert record is not None
    await start_adk_run(
        workflows.runs,
        Queue(),  # type: ignore[arg-type]
        record,
        prepared=PreparedRun({}),
        input={"key": "k"},
        run_as="member-1",
        run_as_name="Mia",
        trigger={"type": "manual", "by": "member-1"},
        run_id=chat_run_id(AGENT, "s-1", "call_1"),
    )
    assert (
        await workflows.progress(
            agent_id=AGENT,
            user_id="ada",
            session_id="s-1",
            call_id="call_1",
            organization_id=ORG,
        )
        is None
    )


def test_a_calls_run_id_fits_the_run_store() -> None:
    made = chat_run_id(AGENT, "s-1", "call_1")
    assert len(made) == 32 and made == chat_run_id(AGENT, "s-1", "call_1")
    assert (
        made
        != chat_run_id(AGENT, "s-1", "call_2")
        != chat_run_id(AGENT, "s-2", "call_1")
    )
