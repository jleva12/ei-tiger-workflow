"""
Bash steps in OpenShell sandboxes (commands openshell): each command in a
sandbox of its own, the run's folder copied in and its changes back.

The gateway is a stand-in whose sandboxes are folders on this machine: what
it runs (the step's wrapper, tar, the stop script) runs here by sh, with
``/sandbox`` in its arguments and variables meaning the sandbox's folder,
and the gateway's limits kept (1 MiB a request; past its time, the process
it started is killed and the call ends with 124, whatever that left running).
"""

import asyncio
import contextlib
import hashlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc
import pytest
from forge_common.sandbox import OpenShellSettings
from forge_common.sandbox import openshell as gateway_module
from google.adk.sessions import DatabaseSessionService
from openshell import ExecChunk, ExecResult
from openshell._proto import openshell_pb2
from openshell.mutations import DeletionOutcome, DeletionResult

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import RunFailed, RunServices
from forge_task_adk_workflows.support import sandbox_commands
from forge_task_adk_workflows.support.sandbox_commands import (
    ALLOWED_HOSTS_RULE,
    CHUNK_BYTES,
    LABELS,
    SANDBOX_WORKSPACE,
    OpenShellCommands,
    allowed_hosts_rule,
)
from forge_task_adk_workflows.workspaces import workspace_of
from forge_tasks.tasks import JobStatus

from .test_commands import FILE_SYSTEM_START, bash, env, stopped, workflow
from .test_graph import agent, end, ran, run, services
from .test_task import ORGANIZATION, payload, worker

READY = openshell_pb2.SANDBOX_PHASE_READY
STARTING = openshell_pb2.SANDBOX_PHASE_PROVISIONING
ERROR = openshell_pb2.SANDBOX_PHASE_ERROR
#: The gateway's limit on one request.
REQUEST_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class Condition:
    type: str
    status: str
    reason: str = ""
    message: str = ""


@dataclass
class Sandbox:
    phase: int
    conditions: tuple[Condition, ...] = ()


class RpcFailure(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


@dataclass
class Exec:
    """One call to run something in a sandbox."""

    name: str
    script: str
    args: list[str]
    env: dict[str, str]
    stdin: int
    timeout: int | None


@dataclass
class LocalGateway:
    """A stand-in for openshell's SandboxClient: a sandbox is a folder here."""

    base: Path
    phases: list[Sandbox] = field(default_factory=lambda: [Sandbox(STARTING), Sandbox(READY)])
    fail_create: Exception | None = None
    #: Called as the step's command starts (another step changing the run's folder meanwhile).
    meanwhile: Callable[[], None] | None = None
    #: Whether the sandbox's image has bash.
    bash: bool = True
    created: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    execs: list[Exec] = field(default_factory=list)
    roots: dict[str, Path] = field(default_factory=dict)
    running: dict[str, list[subprocess.Popen[bytes]]] = field(default_factory=dict)

    def create(self, *, workspace: str, spec: Any, name: str, labels: dict[str, str]) -> None:
        if self.fail_create:
            raise self.fail_create
        root = self.base / name
        root.mkdir(parents=True)
        self.roots[name] = root
        self.created.append({"workspace": workspace, "spec": spec, "name": name, "labels": labels})

    def get(self, name: str, *, workspace: str) -> Sandbox:
        return self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]

    def delete(self, name: str, *, workspace: str, allow_missing: bool) -> DeletionResult:
        assert allow_missing
        self.deleted.append(name)
        # What runs in it stops: the processes it started, and the command's group.
        groups = [process.pid for process in self.running.pop(name, [])]
        session = self.roots[name] / ".forge" / "command" if name in self.roots else None
        if session is not None and session.exists() and session.read_text().strip():
            groups.append(int(session.read_text()))
        for group in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        return DeletionResult(DeletionOutcome.COMPLETED, "id-1")

    def exec(self, name: str, command: list[str], **options: Any) -> ExecResult:
        out, err, code = self._run(name, command, **options)
        return ExecResult(code, out.decode(errors="replace"), err.decode(errors="replace"))

    def exec_stream(self, name: str, command: list[str], **options: Any) -> Any:
        out, err, code = self._run(name, command, **options)
        for start in range(0, len(out), 64 * 1024):
            yield ExecChunk("stdout", out[start : start + 64 * 1024])
        if err:
            yield ExecChunk("stderr", err)
        yield ExecResult(code, "", "")

    def _run(
        self,
        name: str,
        command: list[str],
        *,
        workspace: str,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        no_login_shell: bool = False,
    ) -> tuple[bytes, bytes, int]:
        assert no_login_shell and workspace == "default"
        if name in self.deleted:
            raise RpcFailure(grpc.StatusCode.NOT_FOUND, f"sandbox {name} not found")
        sent = len(stdin or b"") + sum(map(len, command)) + sum(len(k) + len(v) for k, v in (env or {}).items())
        assert sent < REQUEST_LIMIT, "the gateway refuses a request over 1 MiB"
        root = self.roots[name]
        script = command[2]
        self.execs.append(Exec(name, script, command[4:], dict(env or {}), len(stdin or b""), timeout_seconds))

        def here(text: str) -> str:
            return text.replace("/sandbox", str(root))

        command_run = script == sandbox_commands._RUN
        if command_run and self.meanwhile:
            self.meanwhile()
        if command_run and not self.bash:
            script = script.replace("command -v bash", "command -v no-such-bash")
        environ = {"PATH": os.environ["PATH"], "HOME": str(root), "COPYFILE_DISABLE": "1"}
        environ |= {key: here(value) for key, value in (env or {}).items()}
        process = subprocess.Popen(
            ["sh", "-c", here(script), *map(here, command[3:])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ,
            start_new_session=True,
        )
        self.running.setdefault(name, []).append(process)
        out: list[bytes] = []
        err: list[bytes] = []
        readers = [
            threading.Thread(target=lambda s=stream, b=into: b.extend(iter(lambda: s.read1(65536), b"")), daemon=True)
            for stream, into in ((process.stdout, out), (process.stderr, err))
        ]
        for reader in readers:
            reader.start()
        assert process.stdin is not None
        process.stdin.write(stdin or b"")
        process.stdin.close()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # As OpenShell's supervisor: the process it started, not what that started.
            os.kill(process.pid, signal.SIGKILL)
            process.wait()
            timed_out = True
        for reader in readers:
            reader.join(timeout=0.3 if timed_out else 10)
        stdout, stderr = b"".join(out), b"".join(err)
        if "tar -cf" not in script:
            stdout = stdout.replace(str(root).encode(), b"/sandbox")
            stderr = stderr.replace(str(root).encode(), b"/sandbox")
        return stdout, stderr, 124 if timed_out else process.returncode


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_module, "POLL_SECONDS", 0)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    made = tmp_path / "run"
    made.mkdir()
    return made.resolve()


@pytest.fixture
def gateway(tmp_path: Path) -> LocalGateway:
    return LocalGateway(tmp_path / "sandboxes")


def sandbox(gateway: LocalGateway, **options: Any) -> OpenShellCommands:
    return OpenShellCommands(
        OpenShellSettings(),
        image=options.pop("image", "python:3.13-slim"),
        allowed_hosts=options.pop("allowed_hosts", ["github.com"]),
        max_folder_bytes=options.pop("max_folder_bytes", 64 * 1024 * 1024),
        client=gateway,
        **options,
    )


def sandboxed(folder: Path, gateway: LocalGateway, **more: Any) -> RunServices:
    commands = sandbox(gateway, **more.pop("sandbox", {}))
    return services(
        commands=True,
        workspace=str(folder),
        command_sandbox=commands,
        command_environment=more.pop("command_environment", {}),
        **more,
    )


def commands_ran(gateway: LocalGateway) -> list[Exec]:
    return [each for each in gateway.execs if each.script == sandbox_commands._RUN]


# ------------------------------------------------------------------ running


def test_a_command_runs_in_a_sandbox_of_its_own_and_its_changes_come_back(folder: Path, gateway: LocalGateway) -> None:
    (folder / "repo").mkdir()
    (folder / "repo" / "README.md").write_text("hello\n")
    step = bash(
        "build",
        'cat README.md > copy.md && mkdir -p out && echo "built in $PWD" && echo "$WORKSPACE|$OUT" && echo careful >&2',
        cwd="repo",
        env=env(OUT="{{ state.workspace }}/repo/out"),
    )

    session = run(lambda: ran(workflow(step), svc=sandboxed(folder, gateway)))

    # It hands on what it would on the worker: the run's folder's paths are the worker's.
    assert session.result == {
        "exit_code": 0,
        "stdout": f"built in {folder}/repo\n{folder}|{folder}/repo/out",
        "stderr": "careful\n",
        "cwd": str(folder / "repo"),
        "truncated": False,
    }
    assert (folder / "repo" / "copy.md").read_text() == "hello\n"
    assert (folder / "repo" / "out").is_dir()
    # One sandbox, made for it, labelled, deleted after it.
    [created] = gateway.created
    name = created["name"]
    assert name.startswith("forge-bash-") and len(name) <= 19
    assert created["labels"] == LABELS and created["workspace"] == "default"
    assert created["spec"].template.image == "python:3.13-slim"
    assert gateway.deleted == [name]
    # In the sandbox the run's folder is /sandbox/workspace, its variables say so.
    [command] = commands_ran(gateway)
    assert command.args[1:] == [f"{SANDBOX_WORKSPACE}/repo", "/sandbox/.forge"]
    assert command.env["OUT"] == f"{SANDBOX_WORKSPACE}/repo/out"
    assert command.env["WORKSPACE"] == SANDBOX_WORKSPACE
    assert command.timeout == 300


def test_it_gets_the_tools_variables_but_none_of_the_worker_s_machine(folder: Path, gateway: LocalGateway) -> None:
    step = bash("look", "true", env=env(ONE="1"))

    run(
        lambda: ran(
            workflow(step),
            svc=sandboxed(folder, gateway, command_environment={"GH_TOKEN": "gho_token"}),
        )
    )

    [command] = commands_ran(gateway)
    assert command.env == {
        "GH_TOKEN": "gho_token",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
        "ONE": "1",
        "WORKSPACE": SANDBOX_WORKSPACE,
    }


def test_what_another_step_changes_meanwhile_stays(folder: Path, gateway: LocalGateway) -> None:
    (folder / "shared.txt").write_text("before\n")
    (folder / "gone.txt").write_text("remove me\n")

    def meanwhile() -> None:
        (folder / "shared.txt").write_text("another branch's\n")
        (folder / "theirs.txt").write_text("theirs\n")

    gateway.meanwhile = meanwhile
    step = bash("tidy", "rm gone.txt && echo mine > mine.txt")

    run(lambda: ran(workflow(step), svc=sandboxed(folder, gateway)))

    assert (folder / "mine.txt").read_text() == "mine\n"
    assert not (folder / "gone.txt").exists()
    assert (folder / "shared.txt").read_text() == "another branch's\n"
    assert (folder / "theirs.txt").read_text() == "theirs\n"


def test_a_big_folder_goes_in_in_pieces_the_gateway_takes(folder: Path, gateway: LocalGateway) -> None:
    data = os.urandom(3 * 1024 * 1024 + 123)
    (folder / "big.bin").write_bytes(data)
    step = bash("sum", "cat big.bin big.bin > twice.bin")

    run(lambda: ran(workflow(step), svc=sandboxed(folder, gateway)))

    pieces = [each for each in gateway.execs if "in.tar" in each.script]
    assert len(pieces) == 4 and all(each.stdin <= CHUNK_BYTES for each in pieces)
    assert hashlib.sha256((folder / "twice.bin").read_bytes()).digest() == hashlib.sha256(data * 2).digest()


def test_a_command_that_fails_takes_the_error_way_and_what_it_did_comes_back(
    folder: Path, gateway: LocalGateway
) -> None:
    step = bash("clone", "echo cloning; touch partial; echo 'fatal: repository not found' >&2; exit 128")

    session = run(lambda: ran(workflow(step, error="steps.{id}.error"), svc=sandboxed(folder, gateway)))

    assert session.result == {
        "message": "It exited with 128: fatal: repository not found",
        "exit_code": 128,
        "stdout": "cloning",
        "stderr": "fatal: repository not found\n",
    }
    assert (folder / "partial").exists()


def test_a_command_past_its_timeout_is_stopped_and_what_it_did_comes_back(folder: Path, gateway: LocalGateway) -> None:
    step = bash("slow", "sleep 30 & echo $! > child.pid; echo started; wait", timeout_seconds=1)

    session = run(lambda: ran(workflow(step, error="steps.{id}.error"), svc=sandboxed(folder, gateway)))

    assert session.result == {"message": "It didn't finish in 1 seconds", "stdout": "started", "stderr": ""}
    # The gateway stopped the wrapper; the stop script, what it left running.
    assert any(each.script == sandbox_commands._STOP for each in gateway.execs)
    assert run(lambda: stopped(int((folder / "child.pid").read_text())))


def test_what_a_command_leaves_running_stops_with_it(folder: Path, gateway: LocalGateway) -> None:
    # Its child holds stdout open: stopped with it, the call ends at once.
    step = bash("leave", "sleep 30 & echo $! > child.pid; echo done", timeout_seconds=20)
    started = time.monotonic()

    session = run(lambda: ran(workflow(step, ok="steps.{id}.output.stdout"), svc=sandboxed(folder, gateway)))

    assert session.result == "done"
    assert time.monotonic() - started < 10
    assert run(lambda: stopped(int((folder / "child.pid").read_text())))


def test_only_the_end_of_what_a_command_writes_is_kept(folder: Path, gateway: LocalGateway) -> None:
    step = bash("loud", "seq 1 2000")

    session = run(lambda: ran(workflow(step), svc=sandboxed(folder, gateway, max_command_output_bytes=15)))

    assert session.result["stdout"] == "1998\n1999\n2000"
    assert session.result["truncated"] is True


# ------------------------------------------------------------------ limits and failures


def test_a_folder_too_big_to_copy_in_runs_nothing(folder: Path, gateway: LocalGateway) -> None:
    (folder / "big.bin").write_bytes(b"x" * 300_000)
    step = bash("run", "touch ran")

    session = run(
        lambda: ran(
            workflow(step, error="steps.{id}.error.message"),
            svc=sandboxed(folder, gateway, sandbox={"max_folder_bytes": 100_000}),
        )
    )

    assert session.result == (
        "The run's folder holds more than 100000 bytes, more than a sandboxed command may take "
        "(HYBRID_ADK_WORKFLOWS__COMMAND_MAX_FOLDER_BYTES)"
    )
    assert not commands_ran(gateway) and len(gateway.deleted) == 1


def test_a_folder_grown_past_its_limit_brings_nothing_back(folder: Path, gateway: LocalGateway) -> None:
    step = bash("grow", "head -c 300000 /dev/urandom > big.bin; echo grown")

    session = run(
        lambda: ran(
            workflow(step, error="steps.{id}.error"),
            svc=sandboxed(folder, gateway, sandbox={"max_folder_bytes": 100_000}),
        )
    )

    assert session.result == {
        "message": "What it changed in the run's folder couldn't be copied back: the run's folder grew past "
        "100000 bytes (HYBRID_ADK_WORKFLOWS__COMMAND_MAX_FOLDER_BYTES)",
        "exit_code": 0,
        "stdout": "grown",
        "stderr": "",
    }
    assert not (folder / "big.bin").exists()


def test_a_sandbox_that_doesn_t_start_says_why_and_is_deleted(folder: Path, gateway: LocalGateway) -> None:
    landlock = Condition("Ready", "False", "ControlSupervisorStartFailed", "Landlock probe child exited")
    gateway.phases = [Sandbox(STARTING), Sandbox(ERROR, (landlock,))]

    session = run(
        lambda: ran(workflow(bash("run", "true"), error="steps.{id}.error.message"), svc=sandboxed(folder, gateway))
    )

    assert session.result == (
        "The OpenShell sandbox didn't start: ControlSupervisorStartFailed: Landlock probe child exited"
    )
    assert gateway.deleted == [gateway.created[0]["name"]]


def test_a_gateway_that_can_t_make_one_fails_the_step(folder: Path, gateway: LocalGateway) -> None:
    gateway.fail_create = RpcFailure(grpc.StatusCode.UNAVAILABLE, "connection refused")

    with pytest.raises(RunFailed, match=r"Run \(run\) failed: OpenShell couldn't make its sandbox: connection refused"):
        run(lambda: ran(workflow(bash("run", "true")), svc=sandboxed(folder, gateway)))


def test_an_image_without_bash_is_the_deployment_s_to_fix(folder: Path, gateway: LocalGateway) -> None:
    gateway.bash = False

    session = run(
        lambda: ran(workflow(bash("run", "true"), error="steps.{id}.error.message"), svc=sandboxed(folder, gateway))
    )

    assert session.result.startswith("Its sandbox's image has no bash: give Bash steps one that has")
    assert "HYBRID_ADK_WORKFLOWS__COMMAND_IMAGE" in session.result


async def test_a_command_stops_when_its_run_does(folder: Path, gateway: LocalGateway) -> None:
    commands = sandbox(gateway)
    task = asyncio.create_task(
        commands.run(
            "sleep 30 & echo $! > child.pid; wait",
            workspace=str(folder),
            folder=folder,
            env={},
            seconds=60,
            max_bytes=1024,
        )
    )
    for _ in range(200):
        if commands_ran(gateway):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Its sandbox is deleted at once (which stops the command), and nothing comes back.
    assert gateway.deleted
    assert not (folder / "child.pid").exists()
    commands.close()


# ------------------------------------------------------------------ the sandbox's policy


def test_any_program_may_reach_the_allowed_hosts() -> None:
    rule = allowed_hosts_rule(
        ["github.com", "*.githubusercontent.com", "registry.example.com:8443", "[2001:db8::1]:444"]
    )

    assert rule is not None and rule.name == ALLOWED_HOSTS_RULE
    assert [(e.host, e.port) for e in rule.endpoints] == [
        ("github.com", 443),
        ("*.githubusercontent.com", 443),
        ("registry.example.com", 8443),
        ("2001:db8::1", 444),
    ]
    assert [b.path for b in rule.binaries] == ["/**"]
    assert allowed_hosts_rule([]) is None


@pytest.mark.parametrize("host", ["https://github.com", "github.com:http", "github.com:0", "", "a b.com"])
def test_an_allowed_host_that_isn_t_one_stops_the_worker_starting(host: str) -> None:
    with pytest.raises(ValueError, match="isn't a host or host:port"):
        allowed_hosts_rule([host])


def test_the_policy_is_the_deployment_s_with_the_allowed_hosts_besides(gateway: LocalGateway, tmp_path: Path) -> None:
    default = sandbox(gateway, allowed_hosts=[])
    assert not default.spec.policy.network_policies
    assert list(default.spec.policy.filesystem.read_write) == ["/sandbox", "/tmp", "/dev/null"]

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        "filesystem:\n  include_workdir: true\n  read_only: [/usr, /etc]\n  read_write: [/sandbox, /tmp]\n"
        "network_policies:\n  pypi:\n    endpoints: [{host: pypi.org, port: 443}]\n    binaries: [{path: /usr/bin/pip}]\n"
    )
    own = sandbox(gateway, policy=str(policy), allowed_hosts=["github.com"])
    assert set(own.spec.policy.network_policies) == {"pypi", ALLOWED_HOSTS_RULE}
    assert list(own.spec.policy.filesystem.read_only) == ["/usr", "/etc"]

    with pytest.raises(ValueError, match="can't be used"):
        sandbox(gateway, policy=str(tmp_path / "missing.yaml"))


def test_a_worker_set_to_openshell_runs_bash_steps_in_sandboxes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_token")
    policy = tmp_path / "shared.yaml"
    policy.write_text("version: 1\n")
    settings = AdkWorkflowsSettings(
        commands="openshell",
        tool_environment=["GH_TOKEN"],
        openshell={"image": "python:3.13-slim", "policy": str(policy)},
        command_allowed_hosts=["github.com"],
    )

    built = RunServices.of(settings)

    assert built.commands is True
    commands = built.command_sandbox
    assert isinstance(commands, OpenShellCommands)
    # The shared image and policy, unless Bash steps have their own.
    assert commands.spec.template.image == "python:3.13-slim"
    assert set(commands.spec.policy.network_policies) == {ALLOWED_HOSTS_RULE}
    assert commands.max_folder_bytes == 1024 * 1024 * 1024
    # Not this machine's PATH or HOME: the sandbox has its own.
    assert built.command_environment == {"GH_TOKEN": "gho_token"}
    own = RunServices.of(settings.model_copy(update={"command_image": "buildpack-deps:bookworm-scm"}))
    assert own.command_sandbox.spec.template.image == "buildpack-deps:bookworm-scm"
    assert RunServices.of(AdkWorkflowsSettings(commands="local")).command_sandbox is None


# ------------------------------------------------------------------ runs


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


async def test_a_run_s_later_steps_see_what_its_sandboxed_steps_did(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = worker(sessions, commands="openshell", workspaces=str(tmp_path / "volume"))
    gateway = LocalGateway(tmp_path / "sandboxes")
    w.task.services.command_sandbox.client = gateway
    document = agent(
        [
            FILE_SYSTEM_START,
            bash(
                "write",
                'mkdir -p "$NAME" && echo notes > "$NAME/a.txt" && echo "$NAME"',
                env=env(NAME="{{ input.name }}"),
            ),
            bash("read", "cat a.txt && pwd", cwd="{{ steps.write.output.stdout }}"),
            end("done", "steps.read.output"),
        ],
        [("start", "next", "write"), ("write", "success", "read"), ("read", "success", "done")],
    )

    result = await w.run(payload(document, {"name": "backend"}))

    assert result.status is JobStatus.OK, result.error
    folder = workspace_of(tmp_path / "volume", organization=ORGANIZATION, workflow=document["id"], run="local")
    assert (folder / "backend" / "a.txt").read_text() == "notes\n"
    assert result.detail["result"]["stdout"] == f"notes\n{folder}/backend"
    # A sandbox for each step, each deleted.
    assert len(gateway.created) == 2 and sorted(gateway.deleted) == sorted(c["name"] for c in gateway.created)
    await w.task.close()
