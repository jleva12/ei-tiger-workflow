"""
A run's LLM steps with the context window plugin, as the worker runs them: a
step whose tool results outgrow its model's window (model_provider.yaml's
``contextWindow``) is compacted in the middle of the step, by its own model,
and the next step, which sees all of that too, compacts its own view of it.
The run's session keeps every event as it was.
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_agent_runtime import RuntimeServices
from forge_common.adk.context_window import context_of, summaries_of
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import ModelProviderConfig
from google.adk.sessions import DatabaseSessionService

from forge_task_adk_workflows.graph.agent_tools import AgentServices
from forge_task_adk_workflows.runs import APP_NAME
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm
from .test_agent_tools import ORDERS, call, text
from .test_graph import end, llm_config, node
from .test_task import MEMBER, Worker, line, payload, worker

#: One model whose requests may hold 2,000 tokens: it compacts at 1,600.
SMALL = ModelProviderConfig.model_validate(
    {
        "version": 1,
        "default": {"provider": "google", "model": "gemini-test"},
        "providers": {
            "google": {
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
                "api": "google-generative-ai",
                "auth": {"type": "apiKey", "key": "key"},
                "models": [{"id": "gemini-test", "contextWindow": 2_500, "maxTokens": 500}],
            }
        },
    }
)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


def order(request: httpx.Request) -> httpx.Response:
    """Each order with ~3,000 characters of notes."""
    number = request.url.path.rpartition("/")[2]
    return httpx.Response(200, json={"order": number, "notes": f"<{number}> " + "shipped late; " * 210})


def small_worker(sessions: DatabaseSessionService, llm: ScriptedLlm) -> Worker:
    models = ProviderModels(SMALL, build=lambda _call: llm)
    w = worker(sessions, model=models, answer=order)
    agents = AgentServices(
        models=models,
        runtime=RuntimeServices(environment={}, allowed_hosts=("api.example.com",)),
        http=w.task.services.http,
    )
    w.task.services = w.task.services.but(agents=agents)
    return w


def sent(request: Any) -> str:
    return "\n".join(
        part.text or json.dumps(part.function_response.response if part.function_response else None)
        for content in request.contents
        for part in content.parts or ()
    )


def summarizing(request: Any) -> bool:
    return not request.config.system_instruction


async def test_a_step_that_reads_too_much_is_compacted_mid_step(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(
        routed={
            "Read the orders": [
                [call("look_up_order", order_id="A1")],
                [call("look_up_order", order_id="A2")],
                [call("look_up_order", order_id="A3")],
                [text("A2 shipped latest.")],
            ],
            "Write it up": [[text("Report: A2.")]],
        },
        # The summaries, made by the steps' own model.
        turns=[
            [text("Looked up A1: shipped late.")],
            [text("Looked up A1 and A2.")],
            [text("The reader looked up A1 to A3.")],
        ],
    )
    w = small_worker(sessions, llm)
    document = line(
        node("reader", "llm", llm_config(instruction="Read the orders.", tools=[ORDERS])),
        node("writer", "llm", llm_config(instruction="Write it up.")),
        end("done", "steps.writer.output"),
    )

    result = await w.run(payload(document, {"question": "Which order shipped latest?"}))

    assert result.status is JobStatus.OK, result.detail
    assert result.detail["result"] == "Report: A2."
    asked = [request for request in llm.requests if not summarizing(request)]
    summaries = [request for request in llm.requests if summarizing(request)]
    assert len(asked) == 5 and len(summaries) == 3

    # The reader's third and fourth requests: what it read before summarized,
    # its input and its last read as they were.
    third, fourth, writers = sent(asked[2]), sent(asked[3]), sent(asked[4])
    assert "Which order shipped latest?" in third
    assert "<A1>" not in third and "<A2>" in third
    assert "Looked up A1: shipped late." in third
    assert "<A2>" not in fourth and "<A3>" in fourth
    assert "Looked up A1 and A2." in fourth
    # The second summary was made from the first and the read after it.
    assert "Looked up A1: shipped late." in sent(summaries[1])
    # The writer saw all three reads: it summarized them, keeping its input
    # (what the reader answered) as it was.
    assert "Summary of the conversation before the current request" in writers
    assert "The reader looked up A1 to A3." in writers
    assert "<A1>" not in writers and "<A3>" not in writers
    assert "A2 shipped latest." in writers
    assert "<A3>" in sent(summaries[2])

    (session_id,) = await w.session_ids()
    session = await sessions.get_session(app_name=APP_NAME, user_id=MEMBER, session_id=session_id)
    assert session is not None
    # The session is as the steps left it; each step's summaries are its own.
    assert not any(e.actions.compaction for e in session.events)
    (reader_summary,) = summaries_of(session.state, "reader")
    (writer_summary,) = summaries_of(session.state, "writer")
    assert (reader_summary["part"], writer_summary["part"]) == ("turn", "history")
    reader = context_of(session.state, "reader")
    assert reader is not None
    assert (reader["limit"], reader["threshold"], reader["compactions"]) == (2_000, 1_600, 2)
    writer = context_of(session.state, "writer")
    assert writer is not None and writer["compactions"] == 1


async def test_without_a_threshold_a_run_only_keeps_count(
    sessions: DatabaseSessionService,
) -> None:
    llm = ScriptedLlm(
        turns=[
            [call("look_up_order", order_id="A1")],
            [call("look_up_order", order_id="A2")],
            [call("look_up_order", order_id="A3")],
            [text("A2 shipped latest.")],
        ]
    )
    w = small_worker(sessions, llm)
    w.task.services = replace(w.task.services, compaction_threshold=None)
    document = line(
        node("reader", "llm", llm_config(instruction="Read the orders.", tools=[ORDERS])),
        end("done", "steps.reader.output"),
    )

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.detail
    assert "<A1>" in sent(llm.requests[-1])
    (session_id,) = await w.session_ids()
    session = await sessions.get_session(app_name=APP_NAME, user_id=MEMBER, session_id=session_id)
    assert session is not None
    reader = context_of(session.state, "reader")
    assert reader is not None
    assert reader["tokens"] > reader["threshold"] and "compactions" not in reader


def test_the_settings_threshold_reaches_the_runs() -> None:
    from forge_task_adk_workflows.config import AdkWorkflowsSettings
    from forge_task_adk_workflows.graph import RunServices

    assert RunServices.of(AdkWorkflowsSettings()).compaction_threshold == 0.8
    assert RunServices.of(AdkWorkflowsSettings(context_compaction_threshold=0.6)).compaction_threshold == 0.6
    assert RunServices.of(AdkWorkflowsSettings(context_compaction=False)).compaction_threshold is None
