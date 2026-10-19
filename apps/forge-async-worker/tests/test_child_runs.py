"""
Workflows an LLM agent calls as tools, end to end on the worker: the real ADK
workflows task, run by run_adk on a SQLite run store. The calling run starts
the called workflow's run and gives its worker back, paused at a gate for it;
the run it started ending (or the call's wait being up) carries it on with
what became of that run. Its model (``FirstTool``) calls the first tool it's
offered, then answers with what the tool said.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_agent_runtime import RuntimeServices
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import ModelProviderConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import Field

from forge_async_worker import saq_worker
from forge_async_worker.queue import RunQueue
from forge_async_worker.watchdog import Watchdog
from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunServices
from forge_task_adk_workflows.graph.agent_tools import AgentServices
from forge_task_adk_workflows.run_store import (
    ABANDONED,
    CHILD_RUN,
    PAUSED,
    QUEUED,
    SUCCEEDED,
    RunStore,
    open_gates,
)
from forge_task_adk_workflows.task import SERVICES, SESSIONS, AdkWorkflowsTaskFactory
from forge_tasks.tasks import TaskRegistry

from .conftest import ALICE, BOB, START, Clock, FakeSaqQueue, Worker, at, worker_settings

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
                "models": [{"id": "gemini-test", "name": "Gemini Test", "input": ["text"]}],
            }
        },
    }
)


class FirstTool(BaseLlm):
    """Calls the first tool it's offered; once that answers, says what it answered."""

    model: str = "gemini-test"
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        last = llm_request.contents[-1] if llm_request.contents else None
        answers = [part.function_response for part in (last.parts or []) if part.function_response] if last else []
        if answers:
            said = json.dumps(answers[0].response, sort_keys=True)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=said)]))
            return
        call = types.FunctionCall(name=next(iter(llm_request.tools_dict)), args={})
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))


def node(node_id: str, kind: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "kind": kind, "name": node_id.title(), "config": config or {}, "outputs": []}


def workflow(workflow_id: str, *steps: dict[str, Any]) -> dict[str, Any]:
    """A saved workflow (version 1) whose steps run one after another."""
    ids = ["start", *(step["id"] for step in steps)]
    return {
        "format": "forge.agent/v1",
        "id": workflow_id,
        "name": workflow_id.removeprefix("ag_").title(),
        "version": 1,
        "description": "",
        "organization_id": "org-1",
        "nodes": [node("start", "start", {"input_schema": {}}), *steps],
        "edges": [
            {"id": f"{a}->{b}", "source": a, "source_output": "next", "target": b}
            for a, b in zip(ids, ids[1:], strict=False)
        ],
        "layout": {},
    }


def calls(called: str, *, wait: float = 60) -> dict[str, Any]:
    """An LLM agent calling the saved workflow ``called`` (its only tool), its answer what it says."""
    tool = {"id": "tool", "kind": "adk_workflow", "name": "Call", "config": {"workflow": called, "version": 1}}
    tool["config"]["wait_seconds"] = wait
    agent = {
        "description": "",
        "instruction": "Call the workflow, then say what it returned.",
        "model": {"provider": "", "name": ""},
        "thinking_level": "",
        "output_schema": {},
        "include_contents": "default",
        "disallow_transfer_to_parent": False,
        "disallow_transfer_to_peers": False,
        "max_output_tokens": None,
        "sub_agents": [],
        "tools": [tool],
        "mode": "single_turn",
    }
    return node("agent", "llm", agent)


def end(result: str) -> dict[str, Any]:
    return node("done", "end", {"outcome": "succeeded", "result": result})


#: The workflow called last: it hands on what it's handed, and succeeds.
LEAF = workflow("ag_leaf", end('{"refunded": "A1"}'))
#: A workflow that calls the leaf.
MIDDLE = workflow("ag_middle", calls("ag_leaf"), end("steps.agent.output"))
SAVED = {"ag_leaf@1": LEAF, "ag_middle@1": MIDDLE}


@pytest.fixture
def model() -> FirstTool:
    return FirstTool()


@pytest.fixture
async def adk(tmp_path: Path, store: RunStore, clock: Clock, model: FirstTool) -> AsyncIterator[Worker]:
    """The worker, with the real ADK workflows task and its LLM agents' tools, on ``model``."""
    sessions = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    settings = AdkWorkflowsSettings()
    models = ProviderModels(PROVIDERS, build=lambda _call: model)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    agents = AgentServices(models=models, runtime=RuntimeServices(environment={}), http=http)
    services = RunServices.of(settings, http=http, clock=clock, sleep=clock.sleep, model=models).but(agents=agents)
    saq = FakeSaqQueue()
    state = await saq_worker.start_state(
        worker_settings(),
        registry=TaskRegistry([AdkWorkflowsTaskFactory()]),
        store=store,
        queue=RunQueue(saq),
        options={"adk_workflows": settings},
        extras={SERVICES: services, SESSIONS: sessions},
        redis_url=None,
    )
    assert state.jobs is not None
    halts: list[str] = []
    state.jobs.watchdog = Watchdog(halt=halts.append)
    yield Worker(state, saq, clock)
    await saq_worker.close_state(state)
    assert halts == [], "the watchdog would have ended the process"


async def started(worker: Worker, document: dict[str, Any]) -> dict[str, Any]:
    """A run as the admin API starts it, carrying the saved workflows it calls."""
    session = f"session-{document['id']}"
    return await worker.store.create(
        organization_id="org-1",
        agent_id=document["id"],
        agent_name=document["name"],
        revision=1,
        session_id=session,
        payload={
            "tenant_id": "org-1",
            "agent_id": document["id"],
            "name": document["name"],
            "document": document,
            "saved": SAVED,
            "input": {},
            "session_id": session,
            "run_as": ALICE.id,
            "run_as_name": ALICE.name,
        },
        requested_by=ALICE,
    )


def jobs_for(worker: Worker, function: str = "run_adk") -> list[str]:
    """The runs the worker queued ``function`` for, in order."""
    return [kwargs["run_id"] for name, kwargs in worker.sent if name == function]


async def child_of(worker: Worker, run_id: str) -> dict[str, Any]:
    [child] = await worker.store.children(run_id)
    return child


async def test_a_run_calling_a_workflow_gives_its_worker_back_until_the_run_it_started_ends(
    adk: Worker, model: FirstTool
) -> None:
    parent = await started(adk, workflow("ag_parent", calls("ag_leaf", wait=60), end("steps.agent.output")))

    assert await adk.run(parent["id"]) == "paused"

    paused = await adk.get(parent["id"])
    assert (paused["status"], paused["lease_owner"], paused["lease_until"]) == (PAUSED, None, None)
    child = await child_of(adk, parent["id"])
    [waiting] = open_gates(paused)
    assert (waiting["kind"], waiting["details"]["child_run_id"]) == (CHILD_RUN, child["id"])
    assert waiting["reason"] == f"Waits for Leaf (run {child['id'][:8]}) to end"
    assert waiting["deadline"] == "2026-10-01T12:01:01+00:00"  # the call's wait (and a second)
    assert jobs_for(adk) == [child["id"]]  # the run it started is queued
    [(_, expiry)] = [sent for sent in adk.sent if sent[0] == "expire_pause"]
    assert (expiry["pause_id"], expiry["scheduled"]) == (waiting["id"], at(START) + 61)

    assert await adk.run(child["id"]) == "succeeded"

    woken = await adk.get(parent["id"])
    assert woken["status"] == QUEUED  # by the run it started ending
    assert jobs_for(adk) == [child["id"], parent["id"]]
    assert await adk.run(parent["id"]) == "succeeded"
    done = await adk.get(parent["id"])
    said = {"status": "success", "payload": {"run_id": child["id"], "status": SUCCEEDED, "result": {"refunded": "A1"}}}
    assert json.loads(done["result"]) == said
    assert [request.contents[-1].parts[0].function_response.response for request in model.requests[-1:]] == [said]


async def test_a_run_whose_wait_for_the_run_it_started_is_up_carries_on_with_where_that_got_to(
    adk: Worker,
) -> None:
    parent = await started(adk, workflow("ag_parent", calls("ag_leaf", wait=60), end("steps.agent.output")))
    assert await adk.run(parent["id"]) == "paused"
    child = await child_of(adk, parent["id"])
    [waiting] = open_gates(await adk.get(parent["id"]))

    adk.clock.advance(61)
    assert (await adk.state.jobs.expire_pause(parent["id"], waiting["id"]))["outcome"] == "timed_out"
    assert await adk.run(parent["id"]) == "succeeded"

    said = json.loads((await adk.get(parent["id"]))["result"])
    assert said == {"status": "success", "payload": {"run_id": child["id"], "status": QUEUED}}
    # The run it started goes on, its own: it ends as it would have.
    assert await adk.run(child["id"]) == "succeeded"
    assert (await adk.get(parent["id"]))["status"] == SUCCEEDED


async def test_abandoning_a_run_waiting_for_the_run_it_started_abandons_both(adk: Worker) -> None:
    parent = await started(adk, workflow("ag_parent", calls("ag_leaf"), end("steps.agent.output")))
    assert await adk.run(parent["id"]) == "paused"
    child = await child_of(adk, parent["id"])

    await adk.store.abandon(parent["id"], actor=BOB)

    assert (await adk.get(parent["id"]))["status"] == ABANDONED
    assert (await adk.get(child["id"]))["status"] == ABANDONED
    assert await adk.run(child["id"]) == "skipped"


async def test_runs_waiting_for_runs_that_wait_for_runs_carry_on_in_turn(adk: Worker) -> None:
    parent = await started(adk, workflow("ag_parent", calls("ag_middle"), end("steps.agent.output")))
    assert await adk.run(parent["id"]) == "paused"
    middle = await child_of(adk, parent["id"])

    assert await adk.run(middle["id"]) == "paused"  # it waits for its own
    leaf = await child_of(adk, middle["id"])
    assert await adk.run(leaf["id"]) == "succeeded"
    assert (await adk.get(middle["id"]))["status"] == QUEUED
    assert (await adk.get(parent["id"]))["status"] == PAUSED
    assert await adk.run(middle["id"]) == "succeeded"
    assert await adk.run(parent["id"]) == "succeeded"

    said = json.loads((await adk.get(parent["id"]))["result"])
    assert (said["payload"]["run_id"], said["payload"]["status"]) == (middle["id"], SUCCEEDED)
    inner = json.loads(said["payload"]["result"])
    assert inner["payload"] == {"run_id": leaf["id"], "status": SUCCEEDED, "result": {"refunded": "A1"}}
    assert jobs_for(adk) == [middle["id"], leaf["id"], middle["id"], parent["id"]]
