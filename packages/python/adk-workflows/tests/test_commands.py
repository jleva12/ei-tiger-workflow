"""
Bash steps: a command run by bash in a folder of the run's own, its data
handed in through environment variables, never written into the command.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from google.adk.sessions import DatabaseSessionService

from forge_task_adk_workflows.graph import RunFailed
from forge_task_adk_workflows.support.commands import folder_in, passed_environment, run_command
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.workspaces import runs_commands, workspace_of
from forge_tasks.tasks import JobStatus

from .test_graph import START_NODE, agent, end, node, ran, refused, run, services
from .test_task import ORGANIZATION, payload, worker

FILE_SYSTEM_START = node("start", "start", {"input_schema": {}, "allow_file_system": True})


def bash(node_id: str, command: str, **config: Any) -> dict[str, Any]:
    return node(node_id, "bash", {"command": command, **config})


def env(**variables: str) -> list[dict[str, str]]:
    return [{"id": f"v{i}", "name": name, "value": value} for i, (name, value) in enumerate(variables.items())]


def workflow(step: dict[str, Any], *, ok: str = "steps.{id}.output", error: str | None = None) -> dict[str, Any]:
    """A start that allows a file system, the step, and an End for its Success way (and its Error way)."""
    nodes = [FILE_SYSTEM_START, step, end("done", ok.format(id=step["id"]))]
    edges = [("start", "next", step["id"]), (step["id"], "success", "done")]
    if error is not None:
        nodes.append(end("failed", error.format(id=step["id"]), outcome="failed"))
        edges.append((step["id"], "error", "failed"))
    return agent(nodes, edges)


def in_folder(folder: Path, **more: Any) -> Any:
    return services(commands=True, workspace=str(folder), **more)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not yet reaped by its parent) has stopped too.
    status = Path(f"/proc/{pid}/status")
    return not (status.exists() and "zombie" in status.read_text())


async def stopped(pid: int, seconds: float = 5) -> bool:
    for _ in range(int(seconds / 0.05)):
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    return folder


# ------------------------------------------------------------------ running


def test_a_command_runs_in_the_run_s_folder_and_hands_on_what_it_wrote(workspace: Path) -> None:
    step = bash("clone", 'mkdir backend && echo "cloned into $PWD" && echo careful >&2')
    session = run(lambda: ran(workflow(step), svc=in_folder(workspace)))

    folder = str(workspace.resolve())
    assert session.result == {
        "exit_code": 0,
        "stdout": f"cloned into {folder}",
        "stderr": "careful\n",
        "cwd": folder,
        "truncated": False,
    }
    assert (workspace / "backend").is_dir()


def test_the_run_s_data_comes_in_as_variables_and_never_becomes_code(workspace: Path) -> None:
    step = bash(
        "clone",
        'printf "%s|%s|%s" "$REPO" "$WORKSPACE" "{{ input.repo }}"',
        env=env(REPO="{{ input.repo }}"),
    )
    sneaky = 'acme/app"; touch pwned; echo "'
    session = run(
        lambda: ran(workflow(step, ok="steps.{id}.output.stdout"), {"repo": sneaky}, svc=in_folder(workspace))
    )

    # The command is run as written: its {{ }} is bash's text, not the input.
    assert session.result == f"{sneaky}|{workspace.resolve()}|{{{{ input.repo }}}}"
    assert not (workspace / "pwned").exists()


def test_its_folder_is_one_of_the_run_s_made_when_it_isn_t_there(workspace: Path) -> None:
    relative = bash("here", "pwd", cwd="repos/{{ input.name }}")
    absolute = bash("there", "pwd", cwd="{{ state.workspace }}/repos")
    root = workspace.resolve()

    first = run(
        lambda: ran(workflow(relative, ok="steps.{id}.output.stdout"), {"name": "app"}, svc=in_folder(workspace))
    )
    second = run(lambda: ran(workflow(absolute, ok="steps.{id}.output.cwd"), svc=in_folder(workspace)))

    assert first.result == str(root / "repos" / "app")
    assert second.result == str(root / "repos")


@pytest.mark.parametrize("cwd", ["..", "../elsewhere", "/tmp", "repos/../../elsewhere", "out"])
def test_a_folder_outside_the_run_s_takes_the_error_way(workspace: Path, cwd: str) -> None:
    (workspace.parent / "elsewhere").mkdir(exist_ok=True)
    (workspace / "out").symlink_to(workspace.parent / "elsewhere")
    step = bash("escape", "touch escaped", cwd=cwd)

    session = run(lambda: ran(workflow(step, error="steps.{id}.error"), svc=in_folder(workspace)))

    assert session.finished is not None
    assert session.finished["outcome"] == "failed"
    assert session.result == {"message": f"Its folder, {cwd}, is outside the run's folder ({workspace.resolve()})"}
    assert not list(workspace.parent.rglob("escaped"))


def test_a_command_that_fails_takes_the_error_way_with_what_it_wrote(workspace: Path) -> None:
    step = bash("clone", "echo cloning; echo 'fatal: repository not found' >&2; exit 128; echo never")

    session = run(lambda: ran(workflow(step, error="steps.{id}.error"), svc=in_folder(workspace)))

    assert session.result == {
        "message": "It exited with 128: fatal: repository not found",
        "exit_code": 128,
        "stdout": "cloning",
        "stderr": "fatal: repository not found\n",
    }


def test_without_an_error_way_a_command_that_fails_fails_the_run(workspace: Path) -> None:
    # bash stops at the first command that fails, and a pipeline fails when any of it does.
    step = bash("check", "false | cat\necho never")

    with pytest.raises(RunFailed, match=r"^Check \(check\) failed: It exited with 1$"):
        run(lambda: ran(workflow(step), svc=in_folder(workspace)))


def test_a_command_reads_nothing_and_gets_none_of_the_worker_s_secrets(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("GH_TOKEN", "gho_token")
    step = bash("look", 'cat; env | sort | grep -v "^_=" | grep -v "^PWD=" | grep -v "^SHLVL="', env=env(ONE="1"))

    session = run(
        lambda: ran(
            workflow(step, ok="steps.{id}.output.stdout"),
            svc=in_folder(workspace, command_environment=passed_environment(["GH_TOKEN"])),
        )
    )

    seen = dict(line.split("=", 1) for line in session.result.splitlines())
    assert "FORGE_ADMIN_SECRET" not in seen
    assert seen["GH_TOKEN"] == "gho_token"
    assert seen["PATH"] == os.environ["PATH"]
    assert seen["ONE"] == "1"
    assert seen["WORKSPACE"] == str(workspace)
    assert (seen["GIT_TERMINAL_PROMPT"], seen["GH_PROMPT_DISABLED"]) == ("0", "1")


def test_a_command_past_its_timeout_is_stopped_with_what_it_started(workspace: Path) -> None:
    step = bash("slow", "sleep 30 & echo $! > child.pid; echo started; wait", timeout_seconds=1)

    session = run(lambda: ran(workflow(step, error="steps.{id}.error"), svc=in_folder(workspace)))

    assert session.result == {"message": "It didn't finish in 1 seconds", "stdout": "started", "stderr": ""}
    assert run(lambda: stopped(int((workspace / "child.pid").read_text())))


def test_what_a_command_leaves_running_stops_with_it(workspace: Path) -> None:
    step = bash("leave", "sleep 30 > /dev/null 2>&1 & echo $! > child.pid; echo done", timeout_seconds=10)

    session = run(lambda: ran(workflow(step, ok="steps.{id}.output.stdout"), svc=in_folder(workspace)))

    assert session.result == "done"
    assert run(lambda: stopped(int((workspace / "child.pid").read_text())))


def test_only_the_end_of_what_a_command_writes_is_kept(workspace: Path) -> None:
    step = bash("loud", "seq 1 2000")

    session = run(lambda: ran(workflow(step), svc=in_folder(workspace, max_command_output_bytes=15)))

    assert session.result["stdout"] == "1998\n1999\n2000"
    assert session.result["truncated"] is True


async def test_a_command_stops_when_its_run_does(workspace: Path) -> None:
    task = asyncio.create_task(
        run_command(
            "sleep 30 & echo $! > child.pid; wait",
            cwd=workspace,
            env=passed_environment(),
            seconds=60,
            max_bytes=1024,
        )
    )
    pid_file = workspace / "child.pid"
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await stopped(int(pid_file.read_text()))


async def test_a_command_without_bash_on_its_path_says_so(workspace: Path) -> None:
    with pytest.raises(StepFailed, match="bash isn't on this worker's PATH"):
        await run_command("true", cwd=workspace, env={"PATH": str(workspace)}, seconds=5, max_bytes=1024)


def test_a_folder_in_the_run_s_never_leads_out_of_it(workspace: Path) -> None:
    root = workspace.resolve()
    assert folder_in(str(workspace), "") == root
    assert folder_in(str(workspace), "  a/b  ") == root / "a" / "b"
    assert folder_in(str(workspace), str(root / "c")) == root / "c"
    (workspace / "file").write_text("not a folder")
    with pytest.raises(StepFailed, match="can't be made"):
        folder_in(str(workspace), "file")
    with pytest.raises(StepFailed, match="outside the run's folder"):
        folder_in(str(workspace), "a/../../..")


# ------------------------------------------------------------------ refusals


def test_a_bash_step_needs_a_start_that_allows_a_file_system() -> None:
    document = agent([START_NODE, bash("run", "true")], [("start", "next", "run")])

    assert refused(document) == (
        "Node 'Run': a Bash step runs its command in the run's folder: turn on Allow file system on the start."
    )


@pytest.mark.parametrize(
    ("variables", "why"),
    [
        ([("MY VAR", "x")], "its environment variable 'MY VAR' isn't a name bash reads"),
        ([("1ST", "x")], "its environment variable '1ST' isn't a name bash reads"),
        ([("", "x")], "its environment variable '' isn't a name bash reads"),
        ([("WORKSPACE", "x")], "WORKSPACE is the run's folder"),
        ([("FORGE_IDEMPOTENCY_KEY", "x")], "FORGE_IDEMPOTENCY_KEY is its step's key"),
        ([("REPO", "a"), ("REPO", "b")], "two of its environment variables are called REPO"),
    ],
)
def test_its_variables_are_names_bash_reads_each_once(variables: list[tuple[str, str]], why: str) -> None:
    rows = [{"id": f"v{i}", "name": name, "value": value} for i, (name, value) in enumerate(variables)]

    assert why in refused(workflow(bash("run", "true", env=rows)))


def test_a_worker_that_doesn_t_run_commands_fails_the_step(workspace: Path) -> None:
    with pytest.raises(RunFailed, match=r"Run \(run\) failed: this worker doesn't run commands"):
        run(lambda: ran(workflow(bash("run", "touch ran")), svc=services(workspace=str(workspace))))
    assert not (workspace / "ran").exists()


def test_a_saved_workflow_s_bash_step_counts_however_deep() -> None:
    inner = agent([FILE_SYSTEM_START, bash("run", "true")], [], agent_id="ag_inner00002")
    outer = agent([START_NODE, node("nested", "saved", {"agent": "ag_inner00002"})], [])

    assert runs_commands(outer, {"ag_inner00002": inner})
    assert not runs_commands(outer, {})
    assert not runs_commands(inner | {"nodes": [FILE_SYSTEM_START]}, {})


# ------------------------------------------------------------------ runs


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


async def test_a_run_s_bash_steps_work_in_its_folder(sessions: DatabaseSessionService, tmp_path: Path) -> None:
    w = worker(sessions, commands="local", workspaces=str(tmp_path / "volume"))
    document = agent(
        [
            FILE_SYSTEM_START,
            bash("clone", 'git init -q "$NAME" && echo "$NAME"', env=env(NAME="{{ input.name }}")),
            bash("look", "git rev-parse --is-inside-work-tree", cwd="{{ steps.clone.output.stdout }}"),
            end("done", "steps.look.output"),
        ],
        [("start", "next", "clone"), ("clone", "success", "look"), ("look", "success", "done")],
    )

    result = await w.run(payload(document, {"name": "backend"}))

    assert result.status is JobStatus.OK, result.error
    folder = workspace_of(tmp_path / "volume", organization=ORGANIZATION, workflow=document["id"], run="local")
    assert result.detail["result"]["stdout"] == "true"
    assert result.detail["result"]["cwd"] == str(folder.resolve() / "backend")


async def test_a_run_with_a_bash_step_fails_before_it_starts_where_commands_are_off(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = worker(sessions, workspaces=str(tmp_path / "volume"))

    result = await w.run(payload(workflow(bash("run", "touch ran")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("The workflow has Bash steps, and this worker doesn't run commands")
    assert "HYBRID_ADK_WORKFLOWS__COMMANDS=local" in result.error
    assert not (tmp_path / "volume").exists()
