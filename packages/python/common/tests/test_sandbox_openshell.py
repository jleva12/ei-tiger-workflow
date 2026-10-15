"""OpenShellCodeExecutor: each call's code in a new sandbox, on a stand-in gateway."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import grpc
import pytest
from google.adk.code_executors.code_execution_utils import CodeExecutionInput, File
from openshell import ExecChunk, ExecResult, SandboxClient, SandboxError
from openshell._proto import openshell_pb2
from openshell.mutations import DeletionOutcome, DeletionResult

from forge_common.sandbox import OpenShellSettings, SandboxUnavailable
from forge_common.sandbox import openshell as module
from forge_common.sandbox.openshell import (
    LABELS,
    OpenShellCodeExecutor,
    connect,
    default_policy,
    load_policy,
)

READY = openshell_pb2.SANDBOX_PHASE_READY
STARTING = openshell_pb2.SANDBOX_PHASE_PROVISIONING
ERROR = openshell_pb2.SANDBOX_PHASE_ERROR
CONTEXT: Any = None


@dataclass(frozen=True)
class Condition:
    type: str
    status: str
    reason: str = ""
    message: str = ""


@dataclass
class Sandbox:
    """What the executor reads of a sandbox: its phase and conditions."""

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
class Gateway:
    """A stand-in for openshell's SandboxClient: it records what's asked of
    it and answers as it's told."""

    phases: list[Sandbox] = field(default_factory=lambda: [Sandbox(STARTING), Sandbox(READY)])
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int | None = 0
    fail_run: Exception | None = None
    fail_create: Exception | None = None
    deletion: DeletionResult | Exception = field(
        default_factory=lambda: DeletionResult(DeletionOutcome.ACCEPTED, "id-1")
    )
    created: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    def create(self, **request: Any) -> None:
        if self.fail_create:
            raise self.fail_create
        self.created.append(request)

    def get(self, name: str, *, workspace: str) -> Sandbox:
        assert workspace == "default"
        return self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]

    def exec(self, name: str, command: list[str], **options: Any) -> ExecResult:
        # A file the call gives the code, written with sh's cat.
        assert command[:2] == ["sh", "-c"]
        self.files[command[-1]] = options["stdin"]
        return ExecResult(exit_code=0, stdout="", stderr="")

    def exec_stream(
        self, name: str, command: list[str], **options: Any
    ) -> Iterator[ExecChunk | ExecResult]:
        self.runs.append({"name": name, "command": command, **options})
        if self.stdout:
            yield ExecChunk("stdout", self.stdout)
        if self.stderr:
            yield ExecChunk("stderr", self.stderr)
        if self.fail_run:
            raise self.fail_run
        assert self.exit_code is not None
        yield ExecResult(self.exit_code, "", "")

    def delete(self, name: str, *, workspace: str, allow_missing: bool) -> DeletionResult:
        assert allow_missing
        self.deleted.append(name)
        if isinstance(self.deletion, Exception):
            raise self.deletion
        return self.deletion


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "POLL_SECONDS", 0)


def executor(gateway: Gateway, **settings: Any) -> OpenShellCodeExecutor:
    return OpenShellCodeExecutor(settings=OpenShellSettings(**settings), client=gateway)


def test_each_call_runs_in_a_new_sandbox_that_is_deleted_after_it():
    gateway = Gateway(stdout=b"10\n", stderr=b"a warning\n")
    ran = executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="print(sum(range(5)))"))

    # What it printed; a warning of a run that succeeded isn't an error.
    assert (ran.stdout, ran.stderr, ran.exit_code) == ("10\n", "", 0)
    [created] = gateway.created
    name = created["name"]
    assert name.startswith("forge-") and len(name) <= 19 and created["labels"] == LABELS
    assert created["workspace"] == "default"
    spec = created["spec"]
    # The gateway's default image, under the default policy: no network.
    assert not spec.HasField("template")
    assert spec.policy == default_policy()
    assert not spec.policy.network_policies
    assert list(spec.policy.filesystem.read_write) == ["/sandbox", "/tmp", "/dev/null"]
    [run] = gateway.runs
    assert run["command"] == ["python3", "-"]
    assert run["stdin"] == b"print(sum(range(5)))"
    assert run["workdir"] == "/sandbox" and run["no_login_shell"]
    assert run["timeout_seconds"] == 60
    # Nothing of the runtime's environment goes in.
    assert run["env"] == {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    assert gateway.deleted == [name]

    # The next call gets a sandbox of its own.
    executor_ = executor(gateway)
    executor_.execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
    assert gateway.created[1]["name"] != name and gateway.deleted[1] == gateway.created[1]["name"]


def test_a_failed_run_answers_with_its_errors():
    gateway = Gateway(
        stdout=b"so far\n", stderr=b"ZeroDivisionError: division by zero\n", exit_code=1
    )
    ran = executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="print('so far')\n1 / 0"))
    assert ran.stdout == "so far\n"
    assert "ZeroDivisionError" in ran.stderr and ran.exit_code == 1

    # One that died without saying why says how it ended.
    silent = executor(Gateway(exit_code=137)).execute_code(CONTEXT, CodeExecutionInput(code="..."))
    assert silent.stderr == "Code execution exited with status 137."

    # An image without Python is the deployment's to fix, not the code's (as
    # the VM driver's default Ubuntu image answers).
    no_python = Gateway(stderr=b"/bin/bash: line 1: python3: command not found\n", exit_code=127)
    with pytest.raises(SandboxUnavailable, match="image has no python3: .*FORGE_OPENSHELL_IMAGE"):
        executor(no_python).execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
    assert len(no_python.deleted) == 1


def test_a_run_past_its_time_keeps_what_it_printed_and_says_it_was_stopped():
    gateway = Gateway(
        stdout=b"started\n",
        fail_run=RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED, "Deadline Exceeded"),
    )
    ran = executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="while True: pass"))
    assert ran.stdout == "started\n"
    assert ran.stderr == "Code execution timed out after 60 seconds."
    assert ran.exit_code is None
    assert len(gateway.deleted) == 1


def test_a_sandbox_that_doesnt_start_says_why_and_is_deleted():
    landlock = Condition(
        "Ready",
        "False",
        "ControlSupervisorStartFailed",
        "Docker sandbox exited before supervisor became ready; Landlock probe child exited",
    )
    gateway = Gateway(
        phases=[Sandbox(STARTING), Sandbox(ERROR, (landlock, Condition("Scheduled", "True")))]
    )
    with pytest.raises(
        SandboxUnavailable, match="didn't start: ControlSupervisorStartFailed: .*Landlock"
    ):
        executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
    assert gateway.runs == [] and len(gateway.deleted) == 1

    # Nor one still starting when its time is up.
    slow = Gateway(phases=[Sandbox(STARTING)])
    with pytest.raises(
        SandboxUnavailable, match="didn't start in 0.01 seconds: SANDBOX_PHASE_PROVISIONING"
    ):
        executor(slow, ready_seconds=0.01).execute_code(
            CONTEXT, CodeExecutionInput(code="print(1)")
        )
    assert len(slow.deleted) == 1


def test_a_gateway_that_cant_be_reached_ran_nothing():
    down = Gateway(
        fail_create=RpcFailure(grpc.StatusCode.UNAVAILABLE, "failed to connect to all addresses")
    )
    with pytest.raises(SandboxUnavailable, match="couldn't make a sandbox: failed to connect"):
        executor(down).execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
    assert down.runs == []

    # Nor one the run's stream breaks on.
    broken = Gateway(fail_run=SandboxError("ExecSandbox stream ended without an exit event"))
    with pytest.raises(
        SandboxUnavailable, match="stopped running the code: ExecSandbox stream ended"
    ):
        executor(broken).execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
    assert len(broken.deleted) == 1


def test_the_calls_files_are_written_in_the_workspace():
    gateway = Gateway()
    files = [File("data.csv", "a,b\n1,2\n"), File("raw/blob.bin", b"\x00\xff")]
    executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="print(1)", input_files=files))
    assert gateway.files == {
        "/sandbox/data.csv": b"a,b\n1,2\n",
        "/sandbox/raw/blob.bin": b"\x00\xff",
    }

    # None outside it.
    for name in ("../etc/passwd", "/etc/passwd", ".."):
        outside = Gateway()
        with pytest.raises(ValueError, match="isn't a path in the sandbox"):
            executor(outside).execute_code(
                CONTEXT, CodeExecutionInput(code="print(1)", input_files=[File(name, "x")])
            )
        assert outside.runs == [] and len(outside.deleted) == 1


def test_a_sandbox_that_isnt_deleted_is_logged_not_raised(caplog: pytest.LogCaptureFixture):
    for deletion in (
        RpcFailure(grpc.StatusCode.UNAVAILABLE, "gone"),
        DeletionResult(DeletionOutcome.UNSPECIFIED, None),
    ):
        gateway = Gateway(stdout=b"ok\n", deletion=deletion)
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            ran = executor(gateway).execute_code(CONTEXT, CodeExecutionInput(code="print('ok')"))
        assert ran.stdout == "ok\n"
    assert [record.getMessage().split(": ")[-1] for record in caplog.records] == [
        "gone",
        "the gateway said UNSPECIFIED",
    ]


def test_an_image_and_a_policy_file_replace_the_defaults(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        "filesystem:\n"
        "  include_workdir: true\n"
        "  read_only: [/usr, /etc]\n"
        "  read_write: [/sandbox, /tmp]\n"
        "networkPolicies:\n"
        "  pypi:\n"
        "    name: pypi\n"
        "    endpoints: [{host: pypi.org, port: 443}]\n"
    )
    gateway = Gateway()
    executor(gateway, image="python:3.13-slim", policy=str(policy)).execute_code(
        CONTEXT, CodeExecutionInput(code="print(1)")
    )
    spec = gateway.created[0]["spec"]
    assert spec.template.image == "python:3.13-slim"
    assert spec.policy.network_policies["pypi"].endpoints[0].host == "pypi.org"
    assert list(spec.policy.filesystem.read_only) == ["/usr", "/etc"]

    # A broken one stops the runtime from starting, not a call.
    policy.write_text("filesystem: {no_such_field: 1}\n")
    with pytest.raises(ValueError, match=r"policy .*policy.yaml can't be used"):
        executor(Gateway(), policy=str(policy))
    with pytest.raises(ValueError, match="can't be used"):
        load_policy(str(tmp_path / "missing.yaml"))


def test_it_keeps_no_state_between_calls():
    with pytest.raises(ValueError, match="stateful"):
        OpenShellCodeExecutor(stateful=True)
    with pytest.raises(ValueError, match="optimize_data_file"):
        OpenShellCodeExecutor(optimize_data_file=True)


def test_it_reaches_a_registered_gateway_or_a_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    asked: list[str | None] = []

    def registered(*, cluster: str | None = None, timeout: float = 30.0) -> str:
        asked.append(cluster)
        if cluster == "nowhere":
            raise SandboxError("gateway 'nowhere' not found")
        return f"client of {cluster or 'the active gateway'}"

    monkeypatch.setattr(SandboxClient, "from_active_cluster", registered)
    assert connect(OpenShellSettings()) == "client of the active gateway"
    assert connect(OpenShellSettings(gateway="openshell")) == "client of openshell"
    assert asked == [None, "openshell"]

    # By URL, with the CLI's TLS folder.
    for name in ("ca.crt", "tls.crt", "tls.key"):
        (tmp_path / name).write_text("pem")
    client = connect(
        OpenShellSettings(gateway="https://gateway.example:28080", tls_dir=str(tmp_path))
    )
    assert isinstance(client, SandboxClient)
    assert cast(Any, client)._endpoint == "gateway.example:28080"
    client.close()

    with pytest.raises(ValueError, match="isn't an https:// or http:// URL"):
        connect(OpenShellSettings(gateway="ftp://gateway.example"))
    # A gateway it can't use fails the call, not the runtime's start.
    unknown = OpenShellCodeExecutor(settings=OpenShellSettings(gateway="nowhere"))
    with pytest.raises(SandboxUnavailable, match="can't be used: gateway 'nowhere' not found"):
        unknown.execute_code(CONTEXT, CodeExecutionInput(code="print(1)"))
