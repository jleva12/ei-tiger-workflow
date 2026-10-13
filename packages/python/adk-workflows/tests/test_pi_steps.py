"""
Pi agent steps: pi (a stand-in, forge_pi_agent.fake_pi) run in a folder of the
run's own, with the step's tools and model, its answer handed on (held to its
output schema when it has one), and its model and tool calls recorded as the
run's usage.
"""

import json
import shlex
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from forge_common.adk.models import ProviderModels
from forge_common.adk.usage import UsageCall, recording
from forge_common.model_provider import parse_model_provider_yaml
from forge_pi_agent import fake_pi
from google.adk.sessions import DatabaseSessionService

from forge_task_adk_workflows.graph.factories.pi import folder_in
from forge_task_adk_workflows.workspaces import workspace_of
from forge_tasks.tasks import JobStatus

from .test_graph import end, node
from .test_task import ORGANIZATION, Worker, line, payload, worker
from .test_workspaces import file_system_line

MODELS = """
version: 1
default: {provider: openai, model: gpt-5.2}
providers:
  openai:
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    auth: {type: apiKey, key: sk-test}
    models:
      - id: gpt-5.2
        cost: {input: 1, output: 10, cacheRead: 0.1}
      - id: gpt-5.6-luna
"""


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


def pi_worker(sessions: DatabaseSessionService, volume: Path, **settings: Any) -> Worker:
    """A worker that runs Pi agents on the stand-in pi, its runs' folders in ``volume``."""
    models = ProviderModels(parse_model_provider_yaml(MODELS, {}))
    return worker(
        sessions,
        model=models,
        workspaces=str(volume),
        **{"pi_agent": "local", "pi_command": shlex.join(fake_pi.COMMAND), **settings},
    )


def explore(**config: Any) -> dict[str, Any]:
    return node("explore", "pi_agent", {"tools": ["read", "ls"], **config}, name="Explore")


def run_folder(volume: Path, document: dict[str, Any]) -> Path:
    return workspace_of(volume, organization=ORGANIZATION, workflow=document["id"], run="local").resolve()


# ------------------------------------------------------------------ the folder


@pytest.mark.parametrize(
    ("where", "found"),
    [("", "."), ("backend", "backend"), ("a/b", "a/b"), ("./x/../y", "y")],
)
def test_a_pi_agent_s_folder_is_in_the_run_s(tmp_path: Path, where: str, found: str) -> None:
    assert folder_in(tmp_path, where) == (tmp_path / found).resolve()
    assert folder_in(tmp_path, str(tmp_path / where)) == (tmp_path / found).resolve()


@pytest.mark.parametrize("where", ["..", "../other", "/etc", "a/../../b"])
def test_a_folder_outside_the_run_s_is_refused(tmp_path: Path, where: str) -> None:
    with pytest.raises(ValueError, match="isn't in the run's folder"):
        folder_in(tmp_path / "run", where)


def test_a_link_out_of_the_run_s_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "out").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="isn't in the run's folder"):
        folder_in(tmp_path / "run", "out/secrets")


# ------------------------------------------------------------------ runs


async def test_a_pi_agent_works_in_its_folder_and_hands_on_what_it_did(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    volume = tmp_path / "volume"
    record = tmp_path / "record.json"
    w = pi_worker(sessions, volume)
    document = file_system_line(
        explore(
            folder="{{ input.repo }}",
            prompt="Where is the login in {{ input.repo }}?",
            instruction="Answer for {{ input.who }}.",
            model={"provider": "openai", "name": "gpt-5.6-luna"},
            thinking_level="low",
        ),
        end("done", "steps.explore.output"),
    )
    folder = run_folder(volume, document) / "backend"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("forge_task_adk_workflows.graph.factories.pi.PI_ENV", {"FAKE_PI_RECORD": str(record)})
        result = await w.run(payload(document, {"repo": "backend", "who": "Ada"}))

    assert result.status is JobStatus.OK, result.error
    started = json.loads(record.read_text())
    assert started["cwd"] == str(folder)
    assert started["prompts"] == ["Where is the login in backend?"]
    args = started["args"]
    assert args[args.index("--model") + 1] == "forge-openai/gpt-5.6-luna"
    assert args[args.index("--tools") + 1] == "read,ls"
    assert args[args.index("--thinking") + 1] == "low"
    assert "--no-context-files" in args
    assert Path(args[args.index("--append-system-prompt") + 1]).name == "instruction.md"
    assert started["credential"] == "sk-test"
    assert result.detail["result"] == {
        "answer": "you said: Where is the login in backend? .",
        "tool_calls": [{"tool": "ls", "args": {"path": "."}, "is_error": False}],
        "usage": {"input": 20, "cached": 4, "output": 10, "cost": None},
        "folder": str(folder),
    }


async def test_a_pi_agent_without_a_prompt_is_asked_what_it_s_handed(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = pi_worker(sessions, tmp_path / "volume")
    document = file_system_line(explore(tools=[]), end("done", "steps.explore.output.answer"))

    result = await w.run(payload(document, {"task": "map it"}))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == 'you said: {"task": "map it"} .'


async def test_its_usage_is_the_run_s(sessions: DatabaseSessionService, tmp_path: Path) -> None:
    calls: list[UsageCall] = []

    async def sink(call: UsageCall) -> None:
        calls.append(call)

    w = pi_worker(sessions, tmp_path / "volume")
    document = file_system_line(explore(), end("done"))
    with recording(sink):
        result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.error
    mine = [call for call in calls if call.author == "explore"]
    assert [(call.call, call.name) for call in mine] == [
        ("model", "openai/gpt-5.2"),
        ("model", "openai/gpt-5.2"),
        ("tool", "pi:ls"),
    ]
    model_call = mine[0]
    assert (model_call.input_tokens, model_call.cached_tokens, model_call.output_tokens) == (10, 2, 5)
    # 8 fresh at $1, 2 cached at $0.10 and 5 out at $10, per million.
    assert model_call.cost == pytest.approx((8 * 1 + 2 * 0.1 + 5 * 10) / 1_000_000)
    assert model_call.attribution.subject_id == document["id"]
    assert model_call.run_id == "local"


async def test_an_answer_held_to_its_output_schema_is_sent_back_once_when_it_doesnt_fit(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {"files": {"type": "array", "items": {"type": "string"}}},
    }
    w = pi_worker(sessions, tmp_path / "volume")
    document = file_system_line(
        explore(prompt='json {"files": "app.py"}', output_schema=schema),
        end("done", "steps.explore.output.answer"),
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("forge_task_adk_workflows.graph.factories.pi.PI_ENV", {"FAKE_PI_FIX": '{"files": ["app.py"]}'})
        result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == {"files": ["app.py"]}


async def test_an_answer_that_never_fits_fails_the_step(sessions: DatabaseSessionService, tmp_path: Path) -> None:
    w = pi_worker(sessions, tmp_path / "volume")
    document = file_system_line(
        explore(prompt='json {"n": "x"}', output_schema={"type": "object", "properties": {"n": {"type": "number"}}}),
        end("done"),
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("forge_task_adk_workflows.graph.factories.pi.PI_ENV", {"FAKE_PI_FIX": '{"n": "still"}'})
        result = await w.run(payload(document, {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("Explore (explore) failed: pi's answer doesn't fit its output schema")


@pytest.mark.parametrize(
    ("config", "settings", "said"),
    [
        ({"folder": "../elsewhere"}, {}, "isn't in the run's folder"),
        ({"prompt": "fail"}, {}, "pi's model failed: 429 rate limited"),
        ({"prompt": "slow", "timeout_seconds": 0.5}, {}, "pi took longer than 0.5 seconds; it was stopped"),
        ({}, {"pi_agent": "off"}, "Pi agents don't run on this worker: set HYBRID_ADK_WORKFLOWS__PI_AGENT=local"),
        ({}, {"pi_command": "no-such-pi"}, "pi isn't installed here"),
    ],
)
async def test_a_pi_agent_that_can_t_do_its_work_fails_the_run_saying_why(
    sessions: DatabaseSessionService, tmp_path: Path, config: dict[str, Any], settings: dict[str, Any], said: str
) -> None:
    w = pi_worker(sessions, tmp_path / "volume", **settings)

    result = await w.run(payload(file_system_line(explore(**config), end("done")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("Explore (explore) failed: ")
    assert said in result.error


async def test_a_pi_agent_needs_its_start_to_allow_a_file_system(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = pi_worker(sessions, tmp_path / "volume")

    result = await w.run(payload(line(explore(), end("done")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert "Node 'Explore': a Pi agent works in the run's folder: turn on Allow file system on the start." in (
        result.error
    )


@pytest.mark.parametrize("tools", [["read", "web"], "read"])
async def test_a_tool_pi_hasnt_can_t_be_given(sessions: DatabaseSessionService, tmp_path: Path, tools: Any) -> None:
    w = pi_worker(sessions, tmp_path / "volume")

    result = await w.run(payload(file_system_line(explore(tools=tools), end("done")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert "Node 'Explore': its settings aren't a pi_agent's" in result.error
