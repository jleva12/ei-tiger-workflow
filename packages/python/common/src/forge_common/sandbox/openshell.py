"""Agents' code in NVIDIA OpenShell sandboxes: :class:`OpenShellCodeExecutor`,
an ADK code executor. Needs ``forge-common[sandbox-openshell]``.

Each call gets a sandbox of its own: made on the gateway, given the call's
files in ``/sandbox``, sent the code on ``python3``'s standard input, then
deleted, whatever happened. Nothing carries over from one call to the next,
as with ADK's ``UnsafeLocalCodeExecutor``, and nothing of the runtime's goes
in: not its environment, not its files. What the code may reach is the
sandbox's policy: by default (:func:`default_policy`) no network, the system
read-only, and only ``/sandbox`` and ``/tmp`` writable::

    settings = OpenShellSettings(gateway="openshell")
    executor = OpenShellCodeExecutor(settings=settings, timeout_seconds=60)
    result = executor.execute_code(context, CodeExecutionInput(code="print(6 * 7)"))

The gateway is reached on first use, so a runtime starts while it's down; a
call that can't get a sandbox raises :class:`SandboxUnavailable` (nothing
ran), with what the gateway said: e.g. a Docker host whose kernel has no
Landlock can't start one.
"""

from __future__ import annotations

import logging
import posixpath
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import grpc
import yaml
from google.adk.agents.invocation_context import InvocationContext
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import (
    CodeExecutionInput,
    CodeExecutionResult,
    File,
)
from google.protobuf import json_format
from openshell import ExecChunk, ExecResult, SandboxClient, SandboxError, SandboxRef, TlsConfig
from openshell._proto import openshell_pb2, sandbox_pb2
from openshell.mutations import DeletionOutcome
from pydantic import Field, PrivateAttr

from forge_common.sandbox import OpenShellSettings, SandboxUnavailable

__all__ = [
    "OpenShellCodeExecutor",
    "SandboxUnavailable",
    "connect",
    "default_policy",
    "delete_sandbox",
    "load_policy",
    "reason",
    "wait_ready",
]

log = logging.getLogger(__name__)

#: The sandbox's writable workspace, which OpenShell prepares; code runs there.
WORKDIR = "/sandbox"
#: What runs the code, in the sandbox's image.
PYTHON = "python3"
#: Every sandbox this makes carries it, so a gateway's operator can find them.
LABELS = {"forge": "code-execution"}
#: What its sandboxes' names start with; a random part makes each its own
#: (OpenShell's names are at most 19 characters).
NAME_PREFIX = "forge-"
#: Seconds each call to the gateway may take (making, looking at, deleting a
#: sandbox; not running the code).
GATEWAY_SECONDS = 30.0
#: Seconds between looks at a starting sandbox.
POLL_SECONDS = 0.5
#: The most of what the gateway said that an error repeats.
REASON_CHARS = 600


def default_policy() -> sandbox_pb2.SandboxPolicy:
    """
    :return: A sandbox that reaches no network (OpenShell denies what no
        network policy allows), reads the system and writes only
        :data:`WORKDIR`, ``/tmp`` and ``/dev/null``. Landlock confines what
        the kernel supports of it.
    """
    return sandbox_pb2.SandboxPolicy(
        version=1,
        filesystem=sandbox_pb2.FilesystemPolicy(
            include_workdir=True,
            read_only=["/bin", "/usr", "/lib", "/etc", "/proc", "/dev/urandom"],
            read_write=[WORKDIR, "/tmp", "/dev/null"],
        ),
        landlock=sandbox_pb2.LandlockPolicy(compatibility="best_effort"),
    )


def load_policy(path: str) -> sandbox_pb2.SandboxPolicy:
    """
    :param path: A YAML or JSON file with OpenShell's ``SandboxPolicy``
        (its field names, as in the proto or in camelCase).
    :return: The policy.
    :raises ValueError: The file isn't one.
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return json_format.ParseDict(data, sandbox_pb2.SandboxPolicy())
    except (OSError, yaml.YAMLError, json_format.ParseError) as error:
        raise ValueError(f"The OpenShell sandbox policy {path} can't be used: {error}") from error


def connect(settings: OpenShellSettings) -> SandboxClient:
    """
    :param settings: The gateway: one the ``openshell`` CLI registered (by
        name, or the active one), or a URL with this client's TLS folder
        and token.
    :return: A client of the gateway (gRPC connects on its first call).
    :raises SandboxError: No gateway has that name.
    :raises ValueError: The URL isn't one.
    """
    gateway = settings.gateway.strip()
    if "://" not in gateway:
        return SandboxClient.from_active_cluster(cluster=gateway or None, timeout=GATEWAY_SECONDS)
    url = urlparse(gateway)
    if url.scheme not in ("https", "http") or not url.hostname:
        raise ValueError(f"The OpenShell gateway {gateway!r} isn't an https:// or http:// URL.")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    endpoint = f"{host}:{url.port or (443 if url.scheme == 'https' else 80)}"
    tls = None
    if url.scheme == "https":
        folder = Path(settings.tls_dir) if settings.tls_dir else None

        def found(name: str) -> Path | None:
            return folder / name if folder and (folder / name).is_file() else None

        tls = TlsConfig(
            ca_path=found("ca.crt"), cert_path=found("tls.crt"), key_path=found("tls.key")
        )
    token = settings.token.get_secret_value() if settings.token else ""
    return SandboxClient(endpoint, tls=tls, bearer_token=token or None, timeout=GATEWAY_SECONDS)


def reason(error: BaseException) -> str:
    """:return: What a failed call to the gateway said, cut to :data:`REASON_CHARS`."""
    details = getattr(error, "details", None)
    said = details() if callable(details) else None
    return str(said or error)[:REASON_CHARS]


def _why(sandbox: SandboxRef) -> str:
    """:return: Why a sandbox isn't ready, as its conditions say."""
    said = [
        f"{condition.reason}: {condition.message}" if condition.message else condition.reason
        for condition in sandbox.conditions
        if condition.status != "True" and (condition.reason or condition.message)
    ]
    phase = openshell_pb2.SandboxPhase.Name(sandbox.phase) if sandbox.phase else "unknown phase"
    return ("; ".join(said) or phase)[:REASON_CHARS]


def wait_ready(client: Any, name: str, settings: OpenShellSettings) -> None:
    """
    Waits for a sandbox just made to start, up to ``settings.ready_seconds``.

    :raises SandboxUnavailable: It didn't (with why, as its conditions say),
        or the gateway lost it.
    """
    deadline = time.monotonic() + settings.ready_seconds
    while True:
        try:
            sandbox: SandboxRef = client.get(name, workspace=settings.workspace)
        except (grpc.RpcError, SandboxError) as error:
            raise SandboxUnavailable(f"OpenShell lost the sandbox: {reason(error)}") from error
        if sandbox.phase == openshell_pb2.SANDBOX_PHASE_READY:
            return
        if sandbox.phase not in (
            openshell_pb2.SANDBOX_PHASE_UNSPECIFIED,
            openshell_pb2.SANDBOX_PHASE_PROVISIONING,
            openshell_pb2.SANDBOX_PHASE_STARTING,
        ):
            raise SandboxUnavailable(f"The OpenShell sandbox didn't start: {_why(sandbox)}")
        if time.monotonic() >= deadline:
            waited = settings.ready_seconds
            raise SandboxUnavailable(
                f"The OpenShell sandbox didn't start in {waited:g} seconds: {_why(sandbox)}"
            )
        time.sleep(POLL_SECONDS)


def delete_sandbox(client: Any, name: str, workspace: str) -> None:
    """Deletes a sandbox (one already gone is fine); a failure is logged,
    not raised over what the caller has to say."""
    try:
        deletion = client.delete(name, workspace=workspace, allow_missing=True)
    except (grpc.RpcError, SandboxError) as error:
        log.warning("openshell: sandbox %s wasn't deleted: %s", name, reason(error))
        return
    if deletion.outcome not in (
        DeletionOutcome.COMPLETED,
        DeletionOutcome.ACCEPTED,
        DeletionOutcome.ALREADY_ABSENT,
    ):
        log.warning(
            "openshell: sandbox %s wasn't deleted: the gateway said %s",
            name,
            deletion.outcome.name,
        )


class OpenShellCodeExecutor(BaseCodeExecutor):
    """Runs each call's code in a new OpenShell sandbox, then deletes it."""

    # Each call's sandbox is gone after it: nothing to keep state in, and no
    # data files ADK could hand over between calls.
    stateful: bool = Field(default=False, frozen=True, exclude=True)
    optimize_data_file: bool = Field(default=False, frozen=True, exclude=True)

    #: The most seconds one call's code may run before the sandbox stops it.
    timeout_seconds: int = Field(default=60, gt=0)

    #: The gateway, and the sandboxes it makes.
    settings: OpenShellSettings = Field(default_factory=OpenShellSettings)
    #: The gateway's client: made from :attr:`settings` on first use when not given.
    client: Any = Field(default=None, exclude=True)

    _spec: openshell_pb2.SandboxSpec = PrivateAttr()
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(self, **data: Any) -> None:
        if data.get("stateful"):
            raise ValueError("Cannot set `stateful=True` in OpenShellCodeExecutor.")
        if data.get("optimize_data_file"):
            raise ValueError("Cannot set `optimize_data_file=True` in OpenShellCodeExecutor.")
        super().__init__(**data)

    def model_post_init(self, context: Any, /) -> None:
        # The policy file is read now, so a runtime with a broken one doesn't start.
        policy = load_policy(self.settings.policy) if self.settings.policy else default_policy()
        image = self.settings.image.strip()
        self._spec = openshell_pb2.SandboxSpec(
            policy=policy,
            template=openshell_pb2.SandboxTemplate(image=image) if image else None,
        )

    def _gateway(self) -> Any:
        with self._lock:
            if self.client is None:
                try:
                    self.client = connect(self.settings)
                except (SandboxError, ValueError) as error:
                    raise SandboxUnavailable(f"OpenShell can't be used: {reason(error)}") from error
            return self.client

    def execute_code(
        self,
        invocation_context: InvocationContext,
        code_execution_input: CodeExecutionInput,
    ) -> CodeExecutionResult:
        client = self._gateway()
        workspace = self.settings.workspace
        name = NAME_PREFIX + uuid.uuid4().hex[:12]
        try:
            client.create(workspace=workspace, spec=self._spec, name=name, labels=LABELS)
        except (grpc.RpcError, SandboxError) as error:
            raise SandboxUnavailable(
                f"OpenShell couldn't make a sandbox: {reason(error)}"
            ) from error
        try:
            self._wait_ready(client, name)
            for file in code_execution_input.input_files:
                self._put(client, name, file)
            return self._run(client, name, code_execution_input.code)
        finally:
            self._delete(client, name)

    def _wait_ready(self, client: Any, name: str) -> None:
        wait_ready(client, name, self.settings)

    def _put(self, client: Any, name: str, file: File) -> None:
        """Writes one of the call's files in the workspace: bytes as they
        are, text as UTF-8."""
        path = posixpath.normpath(file.name)
        if not file.name or path.startswith("/") or path in (".", "..") or path.startswith("../"):
            raise ValueError(f"The file {file.name!r} isn't a path in the sandbox's workspace.")
        content = file.content.encode("utf-8") if isinstance(file.content, str) else file.content
        try:
            written: ExecResult = client.exec(
                name,
                [
                    "sh",
                    "-c",
                    'mkdir -p "$(dirname "$1")" && cat > "$1"',
                    "sh",
                    posixpath.join(WORKDIR, path),
                ],
                workspace=self.settings.workspace,
                stdin=content,
                no_login_shell=True,
            )
        except (grpc.RpcError, SandboxError) as error:
            raise SandboxUnavailable(
                f"OpenShell couldn't take the file {path}: {reason(error)}"
            ) from error
        if written.exit_code != 0:
            raise SandboxUnavailable(
                f"The file {path} couldn't be written: {written.stderr[:REASON_CHARS]}"
            )

    def _run(self, client: Any, name: str, code: str) -> CodeExecutionResult:
        """Runs the code as UnsafeLocalCodeExecutor does: its stderr only when
        it failed, and a note when it was stopped."""
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        exit_code: int | None = None
        started = time.monotonic()
        timed_out = False
        try:
            for item in client.exec_stream(
                name,
                [PYTHON, "-"],
                workspace=self.settings.workspace,
                workdir=WORKDIR,
                env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=code.encode("utf-8"),
                timeout_seconds=self.timeout_seconds,
                no_login_shell=True,
            ):
                if isinstance(item, ExecChunk):
                    (stdout if item.stream == "stdout" else stderr).append(item.data)
                elif isinstance(item, ExecResult):
                    exit_code = item.exit_code
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                raise SandboxUnavailable(
                    f"OpenShell stopped running the code: {reason(error)}"
                ) from error
            timed_out = True
        except SandboxError as error:
            raise SandboxUnavailable(
                f"OpenShell stopped running the code: {reason(error)}"
            ) from error
        if (
            self.timeout_seconds
            and exit_code != 0
            and time.monotonic() - started >= self.timeout_seconds
        ):
            timed_out = True
        output = b"".join(stdout).decode("utf-8", errors="replace")
        error_output = b"".join(stderr).decode("utf-8", errors="replace")
        # The sandbox's shell couldn't find Python: the image is wrong, not
        # the code, so the model isn't told its program failed.
        if exit_code == 127 and not output and f"{PYTHON}: command not found" in error_output:
            raise SandboxUnavailable(
                f"The sandbox's image has no {PYTHON}: give the sandboxes an image "
                "that has it (FORGE_OPENSHELL_IMAGE, e.g. python:3.13-slim)."
            )
        if timed_out:
            note = f"Code execution timed out after {self.timeout_seconds} seconds."
            error_output = f"{error_output}\n{note}" if error_output else note
        elif exit_code == 0:
            # As ADK's local executor: a non-empty stderr marks the run failed.
            error_output = ""
        elif not error_output:
            error_output = f"Code execution exited with status {exit_code}."
        return CodeExecutionResult(stdout=output, stderr=error_output, exit_code=exit_code)

    def _delete(self, client: Any, name: str) -> None:
        delete_sandbox(client, name, self.settings.workspace)
