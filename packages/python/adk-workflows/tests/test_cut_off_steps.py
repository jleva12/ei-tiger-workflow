"""
Steps cut off as they acted, the run stopped while they ran (its worker went,
or another way's hiccup stopped the run), when the run carries on. One that's
safe to repeat runs again. One whose effect may not be (a POST, a Bash
command, Pi with tools that change things, a push) is neither skipped nor run
again: it takes its Error way, saying it may have run, or with none the run
fails. What a command left running is stopped first. And a run never ends as
a success the session can't show (CURRENT_STATE.md IT-2, IT-3 of the worker's
integration suite).
"""

import asyncio
import shlex
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from forge_common.processes import Tie
from forge_pi_agent import fake_pi
from google.adk.sessions import DatabaseSessionService
from google.adk.workflow import FunctionNode
from google.genai import types

from forge_task_adk_workflows.graph.data import STARTED_KEY
from forge_task_adk_workflows.runs import APP_NAME, RunPayload
from forge_task_adk_workflows.support.commands import tag_of
from forge_task_adk_workflows.workspaces import workspace_of
from forge_tasks.errors import TransientError
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm
from .test_commands import FILE_SYSTEM_START, bash
from .test_github_steps import LOCAL, git
from .test_graph import START_NODE, agent, end, llm_config, node
from .test_pi_steps import MODELS
from .test_task import MEMBER, ORGANIZATION, SESSION, Worker, line, overloaded, payload, worker

CHARGE = "https://api.example.com/charge"


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


class FirstHangs:
    """An API whose first request never gets its answer (its worker stops
    meanwhile); the others are answered 200 ``{"charged": true}``."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        return httpx.Response(200, json={"charged": True})


async def cut_off(w: Worker, run: RunPayload, until: Callable[[], bool]) -> None:
    """One attempt of the run, stopped as its worker would be once ``until`` holds."""
    attempt = asyncio.create_task(w.run(run))
    while not until():
        assert not attempt.done(), "the attempt ended before it could be cut off"
        await asyncio.sleep(0.01)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt


def charge(method: str = "POST", **config: Any) -> dict[str, Any]:
    return node("charge", "http", {"method": method, "url": CHARGE, "body": '{"amount": 42}', **config})


def with_error_way(step: dict[str, Any], way: str = "success") -> dict[str, Any]:
    """The step, its Success way to an End handing on its output, its Error way to one handing on its error."""
    return agent(
        [START_NODE, step, end("done", f"steps.{step['id']}.output"), end("check", f"steps.{step['id']}.error")],
        [("start", "next", step["id"]), (step["id"], way, "done"), (step["id"], "error", "check")],
    )


async def started_keys(sessions: DatabaseSessionService, session_id: str = SESSION) -> list[str]:
    """The keys the run's steps began acting under, in order (``STARTED_KEY``)."""
    session = await sessions.get_session(app_name=APP_NAME, user_id=MEMBER, session_id=session_id)
    assert session is not None
    return [
        event.custom_metadata[STARTED_KEY]["key"]
        for event in session.events
        if STARTED_KEY in (event.custom_metadata or {})
    ]


# ------------------------------------------------------------------ HTTP steps


async def test_a_post_cut_off_mid_request_fails_the_run_and_isnt_sent_again(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions, answer=FirstHangs())
    run = payload(line(charge(), end("done", "steps.charge.output.body")), {})
    await cut_off(w, run, lambda: len(w.sent) == 1)

    result = await w.run(run)

    # Neither skipped (the run succeeding without its End) nor sent again.
    assert result.status is JobStatus.FAILED
    assert result.error == (
        f"Charge (charge) failed: POST {CHARGE} was cut off: the run stopped while it waited for the answer, "
        "so it may have taken effect. A POST isn't sent again on its own: check whether it did"
    )
    assert result.detail["step"] == "charge"
    assert len(w.sent) == 1
    assert "The run carries on where it stopped" in w.control.said()


async def test_a_post_cut_off_takes_its_error_way_saying_it_may_have_run(sessions: DatabaseSessionService) -> None:
    w = worker(sessions, answer=FirstHangs())
    run = payload(with_error_way(charge()), {})
    await cut_off(w, run, lambda: len(w.sent) == 1)

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    error = result.detail["result"]
    assert error["may_have_run"] is True
    assert error["message"].startswith(f"POST {CHARGE} was cut off")
    # The key it was sent under, to look it up by (or try again under).
    assert error["idempotency_key"] == w.sent[0].headers["idempotency-key"]
    assert len(w.sent) == 1


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_a_request_safe_to_repeat_cut_off_mid_request_is_sent_again(
    sessions: DatabaseSessionService, method: str
) -> None:
    w = worker(sessions, answer=FirstHangs())
    run = payload(line(charge(method), end("done", "steps.charge.output.body")), {})
    await cut_off(w, run, lambda: len(w.sent) == 1)

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == {"charged": True}
    assert [request.method for request in w.sent] == [method, method]
    # Only a POST or PATCH carries a key.
    assert "idempotency-key" not in w.sent[0].headers


async def test_a_post_carries_its_step_s_key_or_the_one_it_sets_itself(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    own = [{"id": "h", "name": "idempotency-key", "value": "order-{{ input.order }}"}]
    run = payload(
        line(charge("PATCH"), node("again", "http", {"method": "POST", "url": CHARGE, "headers": own}), end("done")),
        {"order": 7},
    )

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    key = w.sent[0].headers["idempotency-key"]
    assert key == (await started_keys(sessions))[0]
    assert w.sent[1].headers["idempotency-key"] == "order-7"


async def test_a_post_that_failed_is_sent_under_a_new_key_when_its_run_is_tried_again(
    sessions: DatabaseSessionService,
) -> None:
    down = [True]
    w = worker(sessions, answer=lambda request: httpx.Response(503 if down[0] else 200, json={}))
    run = payload(line(charge(), end("done", "steps.charge.output.status")), {})
    assert (await w.run(run)).status is JobStatus.FAILED  # its retries answered 503 too

    down[0] = False  # fixed, then the run is tried again: the request is another
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    first, *_, last = [request.headers["idempotency-key"] for request in w.sent]
    assert first != last


async def test_a_model_hiccup_on_another_way_doesnt_skip_the_post_it_cut_off(sessions: DatabaseSessionService) -> None:
    # The model's 503 on one way stops the run while the other way's POST waits for its answer.
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="thought")]], delay=0.2)
    w = worker(sessions, model=llm, answer=FirstHangs())
    document = agent(
        [
            START_NODE,
            node("think", "llm", llm_config()),
            end("thought", "steps.think.output"),
            charge(),
            end("charged", "steps.charge.output.body"),
            end("check", "steps.charge.error.may_have_run"),
        ],
        [
            ("start", "next", "think"),
            ("think", "next", "thought"),
            ("start", "next", "charge"),
            ("charge", "success", "charged"),
            ("charge", "error", "check"),
        ],
    )
    run = payload(document, {})
    with pytest.raises(TransientError, match="ServerError"):
        await w.run(run)
    assert len(w.sent) == 1

    result = await w.run(run)

    # Both ways went on to their ends: the model's, and the POST's Error way.
    assert result.status is JobStatus.OK, result.error
    assert sorted((ending["step"], ending["result"]) for ending in result.detail["result"]) == [
        ("check", True),
        ("thought", "thought"),
    ]
    assert len(w.sent) == 1
    assert len(llm.requests) == 2


# ------------------------------------------------------------------ Bash steps


def bash_worker(sessions: DatabaseSessionService, volume: Path) -> Worker:
    return worker(sessions, commands="local", workspaces=str(volume))


def run_folder(volume: Path, document: dict[str, Any]) -> Path:
    return workspace_of(volume, organization=ORGANIZATION, workflow=document["id"], run="local")


def leftover(key: str) -> tuple[subprocess.Popen[bytes], Tie]:
    """What a frozen worker's command would leave running: a process group
    tagged with the step's key, its tie still held (the worker isn't dead)."""
    tie = Tie(tag_of(key))
    process = subprocess.Popen(
        tie.argv(["sleep", "30"]), pass_fds=tie.fds, start_new_session=True, stdin=subprocess.DEVNULL
    )
    tie.started()
    return process, tie


async def test_a_command_cut_off_isnt_run_again_and_what_it_left_is_stopped(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    volume = tmp_path / "volume"
    w = bash_worker(sessions, volume)
    document = agent(
        [FILE_SYSTEM_START, bash("work", 'echo began >> "$WORKSPACE/log"; sleep 30'), end("done")],
        [("start", "next", "work"), ("work", "success", "done")],
    )
    run = payload(document, {})
    log = run_folder(volume, document) / "log"
    await cut_off(w, run, log.exists)
    [key] = await started_keys(sessions)
    process, tie = leftover(key)
    await asyncio.sleep(0.3)  # its watcher is up

    try:
        result = await w.run(run)

        assert result.status is JobStatus.FAILED
        assert result.error == (
            "Work (work) failed: Its command was cut off: the run stopped while it ran, so it may have run, "
            "in whole or in part (what it left running is stopped now). It isn't run again on its own: "
            "check what it did"
        )
        # Killed, with its group (this test is the parent that reaps it).
        assert await asyncio.to_thread(process.wait, 5) == -signal.SIGKILL
        assert log.read_text() == "began\n"
    finally:
        process.kill()
        process.wait()
        tie.close()


async def test_a_command_cut_off_takes_its_error_way_with_its_key(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    volume = tmp_path / "volume"
    w = bash_worker(sessions, volume)
    step = bash("work", 'echo "$FORGE_IDEMPOTENCY_KEY" >> "$WORKSPACE/log"; sleep 30')
    document = agent(
        [FILE_SYSTEM_START, step, end("done"), end("check", "steps.work.error")],
        [("start", "next", "work"), ("work", "success", "done"), ("work", "error", "check")],
    )
    run = payload(document, {})
    log = run_folder(volume, document) / "log"
    await cut_off(w, run, lambda: log.exists() and log.read_text().strip() != "")

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    error = result.detail["result"]
    assert error["may_have_run"] is True
    # The key its command read is the one its error carries.
    assert error["idempotency_key"] == log.read_text().strip()


async def test_a_command_dies_with_its_worker(tmp_path: Path) -> None:
    # A worker that dies (kill -9) mid-command: nothing of its command goes on.
    done = tmp_path / "done"
    worker_process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "from forge_task_adk_workflows.support.commands import run_command\n"
        "print(os.getpid(), flush=True)\n"
        f"asyncio.run(run_command('echo $$; sleep 1; touch {shlex.quote(str(done))}', cwd=Path('.'), "
        "env={'PATH': os.environ['PATH']}, seconds=30, max_bytes=1024, key='a-step'))\n",
        stdout=subprocess.PIPE,
    )
    assert worker_process.stdout is not None
    await worker_process.stdout.readline()
    await asyncio.sleep(0.4)  # bash is running its sleep
    worker_process.send_signal(signal.SIGKILL)
    await worker_process.wait()

    await asyncio.sleep(1.5)
    assert not done.exists()


# ------------------------------------------------------------------ Pi agents


def pi_worker(sessions: DatabaseSessionService, volume: Path) -> Worker:
    models = ProviderModels(parse_model_provider_yaml(MODELS, {}))
    return worker(
        sessions, model=models, workspaces=str(volume), pi_agent="local", pi_command=shlex.join(fake_pi.COMMAND)
    )


def pi_line(tools: list[str]) -> dict[str, Any]:
    document = line(node("explore", "pi_agent", {"tools": tools, "prompt": "slow"}), end("done"))
    document["nodes"][0] = FILE_SYSTEM_START
    return document


async def test_pi_with_tools_that_change_things_cut_off_fails_the_run(
    sessions: DatabaseSessionService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setattr("forge_task_adk_workflows.graph.factories.pi.PI_ENV", {"FAKE_PI_RECORD": str(record)})
    w = pi_worker(sessions, tmp_path / "volume")
    run = payload(pi_line(["read", "bash"]), {})
    await cut_off(w, run, record.exists)
    [key] = await started_keys(sessions)

    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "Explore (explore) failed: pi was cut off: the run stopped while it worked, so its tools may have "
        "changed things. It isn't run again on its own: check what it did"
    )
    # pi read the step's key from its environment.
    assert f'"FORGE_IDEMPOTENCY_KEY": "{key}"' in record.read_text()


async def test_pi_that_only_reads_records_no_start_it_would_be_held_to(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = pi_worker(sessions, tmp_path / "volume")
    document = line(node("explore", "pi_agent", {"tools": ["read", "ls"]}), end("done"))
    document["nodes"][0] = FILE_SYSTEM_START

    result = await w.run(payload(document, {}))

    # Cut off, it would simply run again.
    assert result.status is JobStatus.OK, result.error
    assert await started_keys(sessions) == []


# ------------------------------------------------------------------ GitHub steps


@pytest.fixture
def cloned(tmp_path: Path) -> Path:
    """A bare repository, and a clone of it on main."""
    bare = tmp_path / "remotes" / "acme" / "app.git"
    bare.mkdir(parents=True)
    git("init", "--quiet", "--bare", "--initial-branch=main", cwd=bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    (seed / "README.md").write_text("# app\n")
    git("add", "README.md", cwd=seed)
    git("commit", "--quiet", "-m", "First", cwd=seed)
    git("push", "--quiet", str(bare), "main", cwd=seed)
    return bare


def git_line(step: dict[str, Any], result: str = "") -> dict[str, Any]:
    """A start that allows a file system, the step, and an End for its Success way."""
    return agent(
        [FILE_SYSTEM_START, step, end("done", result)],
        [("start", "next", step["id"]), (step["id"], "success", "done")],
    )


def hanging_once(calls: list[Any]) -> Callable[..., Any]:
    """A step's git work whose first call never ends (its worker stops meanwhile); the others hand on ``{}``."""

    async def work(*args: Any, **options: Any) -> dict[str, Any]:
        calls.append(args)
        if len(calls) == 1:
            await asyncio.Event().wait()
        return {"done": True}

    return work


async def test_a_push_cut_off_isnt_pushed_again(
    sessions: DatabaseSessionService, tmp_path: Path, cloned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("forge_task_adk_workflows.graph.factories.github.commit_and_push", hanging_once(calls))
    volume = tmp_path / "volume"
    w = worker(sessions, workspaces=str(volume))
    w.task.services = w.task.services.but(git_protocols=LOCAL)
    document = git_line(node("push", "git_push", {"folder": "app", "message": "Change"}))
    folder = run_folder(volume, document)
    folder.mkdir(parents=True)
    git("clone", "--quiet", cloned.as_uri(), "app", cwd=folder)
    run = payload(document, {})
    await cut_off(w, run, lambda: bool(calls))

    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "Push (push) failed: Its commit and push were cut off: the run stopped while they ran, so its branch "
        "may have been pushed. It isn't pushed again on its own: check the branch on its remote"
    )
    assert len(calls) == 1


async def test_a_branch_cut_off_is_made_again(
    sessions: DatabaseSessionService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("forge_task_adk_workflows.graph.factories.github.create_branch", hanging_once(calls))
    w = worker(sessions, workspaces=str(tmp_path / "volume"))
    document = git_line(node("branch", "git_branch", {"branch": "fix"}), "steps.branch.output")
    run = payload(document, {})
    await cut_off(w, run, lambda: bool(calls))

    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == {"done": True}
    assert len(calls) == 2


# ------------------------------------------------------------------ the run's end


async def test_a_way_that_ends_at_a_way_out_leading_nowhere_succeeds_saying_so(
    sessions: DatabaseSessionService,
) -> None:
    w = worker(sessions)
    document = agent(
        [START_NODE, node("check", "if", {"condition": "input.go"}), end("done", '"went"')],
        [("start", "next", "check"), ("check", "true", "done")],
    )

    result = await w.run(payload(document, {"go": False}))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] is None
    assert "The run ended without reaching an End: Check (check) took its false way, which leads nowhere" in (
        w.control.said()
    )


async def test_a_step_cut_off_that_carrying_on_skipped_fails_the_run(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A step that records it began, which ADK doesn't run again when the run carries on (as HTTP steps
    # weren't): it would be skipped, and the run end as if it had answered.
    def not_run_again(*args: Any, **options: Any) -> FunctionNode:
        return FunctionNode(*args, **{**options, "rerun_on_resume": False})

    monkeypatch.setattr("forge_task_adk_workflows.graph.factories.actions.FunctionNode", not_run_again)
    w = worker(sessions, answer=FirstHangs())
    run = payload(line(charge(), end("done", "steps.charge.output.body")), {})
    await cut_off(w, run, lambda: len(w.sent) == 1)

    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == (
        "Charge (charge) was cut off as it ran, and the run went on without it, so it may have taken effect: "
        "the run fails rather than end as if it had finished"
    )
    assert result.detail["step"] == "charge"
    assert len(w.sent) == 1
