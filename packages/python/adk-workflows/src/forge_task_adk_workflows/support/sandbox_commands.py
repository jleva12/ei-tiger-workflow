"""
Bash steps' commands in NVIDIA OpenShell sandboxes (the worker's ``commands``
is ``openshell``): each command gets a sandbox of its own on the worker's
OpenShell gateway (``forge_common.sandbox.openshell``), made for it and
deleted after it, whatever happened. It runs as ``support.commands`` runs one
on the worker's machine (as written, by bash ``--noprofile --norc -e -o
pipefail``, stdin empty, the end of its output kept), with these differences:

- **The run's folder is copied in, and its changes back.** The folder stays on
  the worker's volume, where every other step finds it (GitHub steps, Pi
  agents, a saved workflow's steps, the run's next attempt on another
  worker). Before the command, its contents go into the sandbox as a tar, in
  pieces (the gateway takes up to 1 MiB a call), to ``/sandbox/workspace``;
  after it, whether it succeeded, failed or ran out of time, they come back,
  and only what the command changed is made in the run's folder
  (``support.folder_sync``): another branch's step working there meanwhile
  keeps its changes. The folder may hold up to ``max_folder_bytes`` going in
  and coming out; past it, the step fails, and nothing comes back.
- **Paths read the same.** In the sandbox the run's folder is
  ``/sandbox/workspace`` (its ``WORKSPACE``), and the command starts in its
  step's folder there. The run's folder's path on the worker
  (``state.workspace``) in its variables is changed to the sandbox's, and the
  sandbox's in what it writes back to the worker's, so its output (``pwd``,
  a path it prints) means what it would on the worker.
- **Its environment.** The sandbox's own (its PATH, HOME, the CA its proxy
  signs with), the variables the worker lets tools read
  (``tool_environment``: e.g. a token for ``gh``), the step's own, and
  ``WORKSPACE``; none of the worker's machine's (its PATH, its HOME).
- **What it may reach.** The sandbox's policy: the deployment's file, else
  OpenShell's default here (``default_policy``: no network, the system
  read-only, ``/sandbox`` and ``/tmp`` writable), and a rule letting any
  program in it reach the allowed hosts (``host`` or ``host:port``, 443 by
  default; ``*.example.com`` for a domain's), so ``git clone`` from GitHub
  takes ``github.com``. Its image needs bash and tar (``python:3.13-slim``
  has both; ``buildpack-deps:bookworm-scm`` git and curl too).
- **How it ends.** bash runs in a process group of its own; when it ends,
  whatever it left running is killed, before the folder is copied back. Past
  its time the gateway stops it, its processes get SIGTERM, 2 seconds, then
  SIGKILL. A run stopped (abandoned) deletes its sandbox, which stops the
  command; a command still waiting for a thread never starts, and one whose
  sandbox is being made has it deleted as soon as it exists, nothing run in
  it. A process killed by a signal exits with 128 and the signal's number,
  as bash reports it.

A sandbox per command rather than one per run: the folder stays where every
other step and every attempt of the run finds it, a paused run (waiting on a
person for days, then carried on by another worker) holds no sandbox, and
nothing outlives its step that would need finding and deleting after a crash
(its sandboxes carry the label ``forge=bash-step``), but for a command whose
worker died as it ran: its sandbox carries its step's key too
(``forge-step``), and the run's next attempt deletes it (:meth:`OpenShellCommands.stop`)
before it decides what became of the step. Starting a sandbox and copying
the folder is the price: a few seconds per step on the VM driver, and the
folder's size each way.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import grpc
from forge_common.sandbox import OpenShellSettings, SandboxUnavailable
from forge_common.sandbox.openshell import (
    connect,
    default_policy,
    delete_sandbox,
    load_policy,
    reason,
    wait_ready,
)
from openshell import ExecChunk, ExecResult, SandboxError
from openshell._proto import openshell_pb2, sandbox_pb2

from forge_task_adk_workflows.support.commands import Finished, Tail, finished, last_line
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.folder_sync import Applied, Manifest, SyncError, apply, pack

__all__ = ["SANDBOX_WORKSPACE", "OpenShellCommands", "allowed_hosts_rule"]

log = logging.getLogger(__name__)

#: Where the run's folder is in the sandbox (its ``WORKSPACE``), and where
#: what the command needs beside it is kept (the folder's copy on its way in,
#: the command's process group): in the sandbox's writable folder, apart
#: from its HOME's files (``/sandbox`` too), which never come back.
SANDBOX_WORKSPACE = "/sandbox/workspace"
STAGING = "/sandbox/.forge"
#: Every sandbox this makes carries it, so a gateway's operator can find them.
LABELS = {"forge": "bash-step"}
#: The label a sandbox carries its step's key in (``graph.factories.base.Attempt``).
KEY_LABEL = "forge-step"
#: What its sandboxes' names start with; a random part makes each its own
#: (OpenShell's names are at most 19 characters).
NAME_PREFIX = "forge-bash-"
#: The most of the folder's copy sent in one call: the gateway takes up to
#: 1 MiB a request, the script and its arguments included.
CHUNK_BYTES = 960 * 1024
#: Seconds each call copying the folder in, or copying it out, may take.
COPY_SECONDS = 600
#: Seconds stopping what a command left running, past its time, may take.
STOP_SECONDS = 30
#: How many commands one worker runs in sandboxes at once (each holds a
#: thread while the gateway runs it); more wait their turn.
THREADS = 32
#: The rule (in the policy's network_policies) for the allowed hosts, and
#: the programs it lets reach them: any.
ALLOWED_HOSTS_RULE = "forge_allowed_hosts"
ANY_PROGRAM = "/**"
#: What the wrapper says when the image has no bash.
NO_BASH = "forge-sandbox: the image has no bash"
#: A path in the run's folder ends where a name can't go on.
_PATH_END = r"(?![\w.-])"

#: Signals a command's process group: at once where the kernel lets it,
#: else each of its processes, found by group or session in /proc
#: (OpenShell's seccomp filter refuses kill() on a group). True when any got it.
_SIGNAL = r"""
members() {
  for f in /proc/[0-9]*/stat; do
    { read -r line < "$f"; } 2>/dev/null || continue
    rest=${line##*") "}
    set -- $rest
    [ "$1" != Z ] || continue
    [ "$3" = "$group" ] || [ "$4" = "$group" ] || continue
    p=${f#/proc/}
    echo "${p%/stat}"
  done
}
signal() {
  group=$2
  kill "-$1" "-$group" 2>/dev/null && return 0
  sent=1
  for p in $(members); do kill "-$1" "$p" 2>/dev/null && sent=0; done
  return $sent
}
"""
#: Runs the command ($1) by bash in its folder ($2), in a session (or group)
#: of its own whose ID it keeps in $3; whatever it leaves running is killed
#: when it ends.
_RUN = (
    _SIGNAL
    + f"""
command -v bash >/dev/null 2>&1 || {{ echo "{NO_BASH}" >&2; exit 127; }}
mkdir -p "$2" && cd "$2" || exit 1
if command -v setsid >/dev/null 2>&1; then
  setsid bash --noprofile --norc -e -o pipefail -c "$1" </dev/null &
else
  set -m 2>/dev/null
  bash --noprofile --norc -e -o pipefail -c "$1" </dev/null &
fi
child=$!
# Its stderr is the command's: the shell's own notices (a job done) aren't.
exec 2>/dev/null
echo "$child" > "$3/command"
wait "$child"
code=$?
signal 9 "$child"
exit "$code"
"""
)
#: Stops what a command stopped past its time left running: SIGTERM, up to
#: 2 seconds, SIGKILL.
_STOP = (
    _SIGNAL
    + """
group=$(cat "$1/command" 2>/dev/null) || exit 0
signal 15 "$group" || exit 0
i=0
while [ "$i" -lt 20 ] && signal 0 "$group"; do sleep 0.1; i=$((i+1)); done
signal 9 "$group"
exit 0
"""
)
#: The folder's copy coming in: a piece added ($1, the staging folder), the
#: last piece added and the copy unpacked in the workspace ($2), or the copy
#: unpacked as it comes when it's one piece. Modes are kept as they were.
_APPEND = 'mkdir -p "$1" && cat >> "$1/in.tar"'
_UNPACK_LAST = 'cat >> "$1/in.tar" && mkdir -p "$2" && tar -xpf "$1/in.tar" -C "$2" && rm -f "$1/in.tar"'
_UNPACK_ONLY = 'mkdir -p "$1" "$2" && tar -xpf - -C "$2"'
#: The workspace going back out ($1), as a tar on stdout.
_PACK = 'mkdir -p "$1" && cd "$1" && tar -cf - .'


def _host_port(entry: str) -> tuple[str, int]:
    """:return: An allowed host's name and port (443 when it names none).
    :raises ValueError: When it isn't ``host`` or ``host:port``."""
    text = entry.strip()
    host, port = text, "443"
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        if rest:
            port = rest.removeprefix(":")
    elif text.count(":") == 1:
        host, port = text.split(":")
    if not host or any(c.isspace() or c in "/@?#" for c in host) or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(
            f"The allowed host {entry!r} isn't a host or host:port (e.g. github.com, or *.example.com:8443)."
        )
    return host, int(port)


def allowed_hosts_rule(hosts: Iterable[str]) -> sandbox_pb2.NetworkPolicyRule | None:
    """
    :param hosts: ``host`` or ``host:port`` each (443 by default); OpenShell's
        wildcards (``*.example.com``) too.
    :return: The network rule letting any program in the sandbox reach them;
        None for none.
    :raises ValueError: When one isn't a host.
    """
    endpoints = [sandbox_pb2.NetworkEndpoint(host=host, port=port) for host, port in map(_host_port, hosts)]
    if not endpoints:
        return None
    return sandbox_pb2.NetworkPolicyRule(
        name=ALLOWED_HOSTS_RULE,
        endpoints=endpoints,
        binaries=[sandbox_pb2.NetworkBinary(path=ANY_PROGRAM)],
    )


class _TooBig(Exception):
    """The folder's copy went past its limit."""


class _CopyBack(Exception):
    """The command's changes couldn't be copied back: the message says why."""


class _Stopped(Exception):
    """Its run stopped: the job goes no further (nobody waits for what it says)."""


class _Paths:
    """The run's folder's path on the worker and in the sandbox, each
    changed to the other."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        forms = sorted({workspace, str(Path(workspace).resolve())}, key=len, reverse=True)
        self._worker = re.compile("(?:" + "|".join(map(re.escape, forms)) + ")" + _PATH_END)
        self._sandbox = re.compile(re.escape(SANDBOX_WORKSPACE) + _PATH_END)

    def inward(self, text: str) -> str:
        return self._worker.sub(lambda _: SANDBOX_WORKSPACE, text)

    def outward(self, text: str) -> str:
        return self._sandbox.sub(lambda _: self.workspace, text)


class OpenShellCommands:
    """Runs Bash steps' commands, each in a new OpenShell sandbox."""

    def __init__(
        self,
        settings: OpenShellSettings,
        *,
        image: str = "",
        policy: str = "",
        allowed_hosts: Sequence[str] = (),
        max_folder_bytes: int,
        client: Any = None,
    ) -> None:
        """
        :param settings: The gateway, and its sandboxes' workspace and the
            seconds one has to start.
        :param image: The sandboxes' image (it needs bash and tar); empty,
            the gateway's default.
        :param policy: A YAML or JSON file with their policy (OpenShell's
            ``SandboxPolicy``); empty, ``default_policy``.
        :param allowed_hosts: What any program in them may reach besides.
        :param max_folder_bytes: The most the run's folder's copy may hold,
            going in and coming out.
        :param client: The gateway's client; made from ``settings`` on first use.
        :raises ValueError: The policy file can't be used, or an allowed host
            isn't one (so a worker set up wrong doesn't start).
        """
        self.settings = settings
        self.max_folder_bytes = max_folder_bytes
        self.client = client
        rules = load_policy(policy) if policy else default_policy()
        rule = allowed_hosts_rule(allowed_hosts)
        if rule is not None:
            rules.network_policies[ALLOWED_HOSTS_RULE].CopyFrom(rule)
        image = image.strip()
        self.spec = openshell_pb2.SandboxSpec(
            policy=rules,
            template=openshell_pb2.SandboxTemplate(image=image) if image else None,
        )
        self._lock = threading.Lock()
        self._threads = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="forge-bash-sandbox")

    def gateway(self) -> Any:
        """:return: The gateway's client, made on first use.
        :raises StepFailed: When it can't be (a name no gateway has)."""
        with self._lock:
            if self.client is None:
                try:
                    self.client = connect(self.settings)
                except (SandboxError, ValueError) as error:
                    raise StepFailed(f"OpenShell can't be used: {reason(error)}") from None
            return self.client

    def close(self) -> None:
        self._threads.shutdown(wait=False, cancel_futures=True)
        close = getattr(self.client, "close", None)
        if close is not None:
            close()

    async def run(
        self,
        command: str,
        *,
        workspace: str,
        folder: Path,
        env: Mapping[str, str],
        seconds: float,
        max_bytes: int,
        key: str = "",
    ) -> Finished:
        """
        Run a command by bash in a new sandbox, to its end, the run's folder
        copied in and its changes back.

        :param command: The script.
        :param workspace: The run's folder on the worker (``state.workspace``).
        :param folder: Where it starts: a folder of the run's (``folder_in``).
        :param env: Its variables (the sandbox has its own PATH and HOME).
        :param seconds: How long it may take.
        :param max_bytes: The most of stdout, and of stderr, kept.
        :param key: Its step's key, which its sandbox carries (:meth:`stop`).
        :return: How it ended, whatever its exit code.
        :raises StepFailed: When it can't run (no sandbox, a folder too big
            to copy), doesn't finish in time, or what it changed can't be
            copied back (with what it wrote).
        """
        job = _Job(
            self, command, workspace=workspace, folder=folder, env=env, seconds=seconds, max_bytes=max_bytes, key=key
        )
        queued = self._threads.submit(job.run)
        running = asyncio.wrap_future(queued)
        running.add_done_callback(_settled)
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            # Its run stopped (abandoned): a job still waiting for a thread
            # never starts; else its sandbox goes now (once made, when it's
            # being made), which stops the command; nothing it did comes back.
            job.stopped.set()
            if not queued.cancel():
                await asyncio.shield(asyncio.to_thread(job.discard))
            raise

    async def stop(self, key: str) -> int:
        """
        Delete the sandboxes a step's command left behind: an earlier
        attempt's, whose worker died as it ran (a sandbox lives on without
        it), found by the step's key.

        :return: How many there were; 0 too when the gateway can't say.
        """
        return await asyncio.to_thread(self._stop, key)

    def _stop(self, key: str) -> int:
        try:
            client = self.gateway()
            found = client.list_all(workspace=self.settings.workspace, label_selector=f"{KEY_LABEL}={key}")
        except (StepFailed, grpc.RpcError, SandboxError) as error:
            log.warning("adk_workflows: the sandboxes of step %s couldn't be looked for: %s", key, error)
            return 0
        for sandbox in found:
            delete_sandbox(client, sandbox.name, self.settings.workspace)
        return len(found)


def _settled(future: Future[Any] | asyncio.Future[Any]) -> None:
    # A job whose run stopped ends on its own: what it raised is nobody's.
    if not future.cancelled():
        future.exception()


class _Sender:
    """The folder's copy on its way into the sandbox: written as a stream,
    sent in pieces, the last of them unpacking it."""

    def __init__(self, job: _Job, client: Any) -> None:
        self.job = job
        self.client = client
        self.buffer = bytearray()
        self.sent = 0
        self.pieces = 0
        self.broken = False

    def write(self, data: bytes) -> int:
        if self.broken:
            return len(data)
        self.sent += len(data)
        if self.sent > self.job.owner.max_folder_bytes:
            self.broken = True
            raise _TooBig
        self.buffer += data
        while len(self.buffer) > CHUNK_BYTES:
            piece = bytes(self.buffer[:CHUNK_BYTES])
            del self.buffer[:CHUNK_BYTES]
            self._send(_APPEND, piece)
        return len(data)

    def finish(self) -> None:
        self._send(_UNPACK_LAST if self.pieces else _UNPACK_ONLY, bytes(self.buffer))
        self.buffer.clear()

    def _send(self, script: str, piece: bytes) -> None:
        self.job._going()
        try:
            done: ExecResult = self.client.exec(
                self.job.name,
                ["sh", "-c", script, "sh", STAGING, SANDBOX_WORKSPACE],
                workspace=self.job.sandbox_workspace,
                stdin=piece,
                timeout_seconds=COPY_SECONDS,
                no_login_shell=True,
            )
        except (grpc.RpcError, SandboxError) as error:
            self.broken = True
            raise StepFailed(f"The run's folder couldn't be copied into its sandbox: {reason(error)}") from None
        if done.exit_code != 0:
            self.broken = True
            said = last_line(done.stderr) or f"it exited with {done.exit_code}"
            if done.exit_code == 127 and "tar" in said:
                said = "its image has no tar: give Bash steps one that has (HYBRID_ADK_WORKFLOWS__COMMAND_IMAGE)"
            raise StepFailed(f"The run's folder couldn't be copied into its sandbox: {said}")
        self.pieces += 1


class _Job:
    """One command, in its sandbox: made, the folder copied in, the command
    run, its changes copied back, deleted. Runs on a thread of its own (the
    gateway's client blocks)."""

    def __init__(
        self,
        owner: OpenShellCommands,
        command: str,
        *,
        workspace: str,
        folder: Path,
        env: Mapping[str, str],
        seconds: float,
        max_bytes: int,
        key: str = "",
    ) -> None:
        self.owner = owner
        self.command = command
        self.root = Path(workspace).resolve()
        self.paths = _Paths(workspace)
        inside = folder.resolve().relative_to(self.root).as_posix()
        self.cwd = SANDBOX_WORKSPACE if inside == "." else f"{SANDBOX_WORKSPACE}/{inside}"
        self.env = {name: self.paths.inward(value) for name, value in env.items()}
        self.env["WORKSPACE"] = SANDBOX_WORKSPACE
        self.seconds = seconds
        self.max_bytes = max_bytes
        self.name = NAME_PREFIX + uuid.uuid4().hex[:8]
        self.labels = {**LABELS, KEY_LABEL: key} if key else LABELS
        self.sandbox_workspace = owner.settings.workspace
        self.stopped = threading.Event()
        #: Held while the sandbox is made, so a discard meanwhile waits for it to exist.
        self._making = threading.Lock()
        #: The client that made (or was making) the sandbox; None before.
        self._made_by: Any = None

    def discard(self) -> None:
        """Delete the sandbox now (its run stopped): at once when it's made,
        as soon as it exists when it's being made; when it isn't yet, it
        never will be."""
        self.stopped.set()
        with self._making:
            client = self._made_by
        if client is not None:
            delete_sandbox(client, self.name, self.sandbox_workspace)

    def _going(self) -> None:
        """:raises _Stopped: Its run stopped: nothing more is asked of the sandbox."""
        if self.stopped.is_set():
            raise _Stopped

    def run(self) -> Finished:
        client = self.owner.gateway()
        with self._making:
            self._going()
            self._made_by = client
            try:
                client.create(
                    workspace=self.sandbox_workspace, spec=self.owner.spec, name=self.name, labels=self.labels
                )
            except (grpc.RpcError, SandboxError) as error:
                raise StepFailed(f"OpenShell couldn't make its sandbox: {reason(error)}") from None
        try:
            self._going()
            try:
                wait_ready(client, self.name, self.owner.settings)
            except SandboxUnavailable as error:
                raise StepFailed(str(error)) from None
            self._going()
            manifest = self._copy_in(client)
            self._going()
            ended, timed_out = self._command(client)
            self._going()
            if timed_out:
                self._stop(client)
            try:
                applied = self._copy_out(client, manifest)
            except _CopyBack as error:
                how = f"It didn't finish in {self.seconds:g} seconds, and what" if timed_out else "What"
                details: dict[str, Any] = {"stdout": ended.stdout, "stderr": ended.stderr}
                if not timed_out:
                    details = {"exit_code": ended.exit_code, **details}
                raise StepFailed(
                    f"{how} it changed in the run's folder couldn't be copied back: {error}", details=details
                ) from None
            log.debug(
                "adk_workflows: sandbox %s changed %d and removed %d in the run's folder",
                self.name,
                len(applied.written),
                len(applied.removed),
            )
            if timed_out:
                raise StepFailed(
                    f"It didn't finish in {self.seconds:g} seconds",
                    details={"stdout": ended.stdout, "stderr": ended.stderr},
                )
            return ended
        finally:
            delete_sandbox(client, self.name, self.sandbox_workspace)

    def _copy_in(self, client: Any) -> Manifest:
        sender = _Sender(self, client)
        try:
            manifest = pack(self.root, sender)  # type: ignore[arg-type]  # write() is all tarfile writes
            sender.finish()
        except _TooBig:
            raise StepFailed(
                f"The run's folder holds more than {self.owner.max_folder_bytes} bytes, more than a sandboxed "
                "command may take (HYBRID_ADK_WORKFLOWS__COMMAND_MAX_FOLDER_BYTES)"
            ) from None
        except SyncError as error:
            raise StepFailed(f"The run's folder couldn't be copied into its sandbox: {error}") from None
        return manifest

    def _command(self, client: Any) -> tuple[Finished, bool]:
        """:return: How the command ended, and whether it ran out of time."""
        out, err = Tail(self.max_bytes), Tail(self.max_bytes)
        exit_code: int | None = None
        timed_out = False
        started = time.monotonic()
        stream = client.exec_stream(
            self.name,
            ["sh", "-c", _RUN, "sh", self.command, self.cwd, STAGING],
            workspace=self.sandbox_workspace,
            env=self.env,
            timeout_seconds=math.ceil(self.seconds),
            no_login_shell=True,
        )
        try:
            for item in stream:
                if isinstance(item, ExecChunk):
                    (out if item.stream == "stdout" else err).add(item.data)
                elif isinstance(item, ExecResult):
                    exit_code = item.exit_code
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                raise StepFailed(f"OpenShell stopped running it: {reason(error)}") from None
            timed_out = True
        except SandboxError as error:
            raise StepFailed(f"OpenShell stopped running it: {reason(error)}") from None
        # The gateway stops a command past its time with 124 (timeout's).
        if exit_code == 124 and time.monotonic() - started >= self.seconds:
            timed_out = True
        if exit_code == 127 and NO_BASH in err.text():
            raise StepFailed(
                "Its sandbox's image has no bash: give Bash steps one that has "
                "(HYBRID_ADK_WORKFLOWS__COMMAND_IMAGE, e.g. python:3.13-slim)"
            )
        ended = finished(124 if exit_code is None else exit_code, out, err)
        return (
            dataclasses.replace(
                ended, stdout=self.paths.outward(ended.stdout), stderr=self.paths.outward(ended.stderr)
            ),
            timed_out,
        )

    def _stop(self, client: Any) -> None:
        """Stop what a command stopped past its time left running."""
        try:
            client.exec(
                self.name,
                ["sh", "-c", _STOP, "sh", STAGING],
                workspace=self.sandbox_workspace,
                timeout_seconds=STOP_SECONDS,
                no_login_shell=True,
            )
        except (grpc.RpcError, SandboxError) as error:
            log.warning(
                "adk_workflows: what sandbox %s's command left running wasn't stopped: %s", self.name, reason(error)
            )

    def _copy_out(self, client: Any, manifest: Manifest) -> Applied:
        if self.stopped.is_set():
            raise _CopyBack("its run stopped")
        with tempfile.TemporaryFile() as archive:
            self._receive(client, archive)
            if self.stopped.is_set():
                raise _CopyBack("its run stopped")
            archive.seek(0)
            try:
                return apply(archive, self.root, manifest)
            except SyncError as error:
                raise _CopyBack(str(error)) from None

    def _receive(self, client: Any, archive: BinaryIO) -> None:
        """Write the workspace's tar to ``archive``."""
        said = Tail(4096)
        received = 0
        exit_code: int | None = None
        stream = client.exec_stream(
            self.name,
            ["sh", "-c", _PACK, "sh", SANDBOX_WORKSPACE],
            workspace=self.sandbox_workspace,
            timeout_seconds=COPY_SECONDS,
            no_login_shell=True,
        )
        try:
            for item in stream:
                if isinstance(item, ExecChunk):
                    if item.stream != "stdout":
                        said.add(item.data)
                        continue
                    received += len(item.data)
                    if received > self.owner.max_folder_bytes:
                        raise _CopyBack(
                            f"the run's folder grew past {self.owner.max_folder_bytes} bytes "
                            "(HYBRID_ADK_WORKFLOWS__COMMAND_MAX_FOLDER_BYTES)"
                        )
                    archive.write(item.data)
                elif isinstance(item, ExecResult):
                    exit_code = item.exit_code
        except (grpc.RpcError, SandboxError) as error:
            raise _CopyBack(f"OpenShell stopped sending it: {reason(error)}") from None
        finally:
            with contextlib.suppress(Exception):
                stream.close()
        # tar's 1: a file changed while it was read (it's in the copy as it was then).
        if exit_code not in (0, 1):
            raise _CopyBack(last_line(said.text()) or f"tar exited with {exit_code}")
