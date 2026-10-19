"""
Bash steps' commands (``graph.factories.actions.bash``): run by bash on the
worker's machine, as its user, starting in a folder of the run's own
(``forge_task_adk_workflows.workspaces``); or, where the worker's
``commands`` is ``openshell``, in a sandbox of its own on an OpenShell gateway
(``support.sandbox_commands``), the run's folder copied in and its changes
copied back.

- **As written.** The command is never filled in with the run's data, so the
  data can't become code: it reads it from its environment variables (each a
  template the step fills in), e.g. ``gh repo clone "$REPO"``.
- **Its folder.** It starts in its step's folder: the run's folder, or a
  folder in it (a path from the run's folder, or an absolute one inside it),
  made when it isn't there, never one a link leads out of. The folder is where
  it starts, not a sandbox: bash can ``cd`` anywhere its user may.
- **Its environment.** The worker's PATH, HOME, locale, time zone and
  temporary folder; the variables the worker lets tools read
  (``tool_environment``); the step's own; and ``WORKSPACE``, the run's folder.
  Nothing else of the worker's (its database's URL, its keys). Git and gh
  never ask for anything: a command has nobody to answer, and no stdin.
- **How it ends.** bash runs it as GitHub Actions runs a step's
  (``--noprofile --norc -e -o pipefail``): it stops at the first command that
  fails. Whatever it leaves running in the background stops when it does. Past
  its time, it's sent SIGTERM, then SIGKILL; a run stopped (abandoned) stops
  it too. What it wrote to stdout and stderr is kept up to a limit each: the
  end of it.
- **It goes with its worker.** Its process group is tied to the worker's
  process (``forge_common.processes``): a worker that dies, ``kill -9`` too,
  takes its commands with it. Each carries its step's key as a tag, so the
  run's next attempt on the same machine stops what a worker that froze left
  running (:func:`stop_leftovers`) before it decides what became of the step.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from forge_common.processes import TAG_PREFIX, Tie, stop_tagged

from forge_task_adk_workflows.support.errors import StepFailed

#: How a worker that doesn't run commands is set to.
NO_COMMANDS = (
    "set HYBRID_ADK_WORKFLOWS__COMMANDS=openshell to run them in sandboxes on an OpenShell gateway, "
    "or HYBRID_ADK_WORKFLOWS__COMMANDS=local on a worker whose workflows' authors you trust with its machine"
)
#: What a step's environment variable may be called.
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The variable every command reads its run's folder from.
WORKSPACE_VARIABLE = "WORKSPACE"
#: The variable a command reads its step's key from: the same for every
#: attempt of the step's one execution (``graph.factories.base.Attempt``).
KEY_VARIABLE = "FORGE_IDEMPOTENCY_KEY"
#: Variables a step's own can't be called.
RESERVED_VARIABLES = frozenset({WORKSPACE_VARIABLE, KEY_VARIABLE})
#: The worker's own variables every command gets, when the worker has them.
PASSED = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
#: Nothing it runs asks for a password or a choice: there's nobody to answer.
UNASKED = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
#: bash's options: no profile or rc files, stopping at the first command that
#: fails, a pipeline failing when any part of it does.
BASH = ("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c")
#: Seconds a command has to stop after SIGTERM, and for its output to close
#: once it has.
GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class Finished:
    """How a command ended: its exit code (minus the signal's number when one
    killed it), and the end of what it wrote."""

    exit_code: int
    stdout: str
    stderr: str
    #: Whether stdout or stderr was longer than the limit (only its end is kept).
    truncated: bool


def passed_environment(
    names: Iterable[str] = (), environ: Mapping[str, str] | None = None, *, machine: bool = True
) -> dict[str, str]:
    """
    :param names: The worker's variables tools may read (``tool_environment``).
    :param environ: The worker's environment (``os.environ``).
    :param machine: Whether the commands run on the worker's machine, and
        get its PATH, HOME and locale (:data:`PASSED`) too; a sandbox has its own.
    :return: What every command gets of the worker's environment.
    """
    environ = os.environ if environ is None else environ
    wanted = (*PASSED, *names) if machine else tuple(names)
    return {name: environ[name] for name in wanted if name in environ}


def folder_in(workspace: str, path: str) -> Path:
    """
    The folder a command runs in, made when it isn't there.

    :param workspace: The run's folder.
    :param path: A folder of the run's folder (from it, or absolute), or the
        run's folder itself when empty.
    :return: The folder, links followed.
    :raises StepFailed: When it's outside the run's folder, or can't be made.
    """
    root = Path(workspace).resolve()
    wanted = path.strip()
    try:
        folder = (root / wanted).resolve() if wanted else root
        if not folder.is_relative_to(root):
            raise StepFailed(f"Its folder, {wanted}, is outside the run's folder ({root})")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        why = getattr(error, "strerror", None) or error
        raise StepFailed(f"Its folder, {wanted}, can't be made in the run's folder: {why}") from None
    return folder


class Tail:
    """The end of a stream: its last ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.cut = False

    def add(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self.limit:
            del self.data[: len(self.data) - self.limit]
            self.cut = True

    async def read(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while chunk := await stream.read(64 * 1024):
            self.add(chunk)

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def finished(exit_code: int, stdout: Tail, stderr: Tail) -> Finished:
    """:return: How a command ended: its exit code, and the end of what it
    wrote (stdout without its last line ends, as bash's ``$( )`` gives it)."""
    return Finished(
        exit_code=exit_code,
        stdout=stdout.text().rstrip("\r\n"),
        stderr=stderr.text(),
        truncated=stdout.cut or stderr.cut,
    )


def _signal(group: int, number: int) -> None:
    # The command's process group: bash and whatever it started.
    try:
        os.killpg(group, number)
    except (ProcessLookupError, PermissionError):
        pass


async def run_command(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    seconds: float,
    max_bytes: int,
    key: str = "",
) -> Finished:
    """
    Run a command by bash, to its end.

    :param command: The script.
    :param cwd: Where it starts (:func:`folder_in`).
    :param env: Its whole environment.
    :param seconds: How long it may take.
    :param max_bytes: The most of stdout, and of stderr, kept.
    :param key: Its step's key: what it's found by should it outlive its
        attempt (:func:`stop_leftovers`).
    :return: How it ended, whatever its exit code.
    :raises StepFailed: When it can't start, or doesn't finish in time (with
        what it wrote by then).
    """
    return await run_program((*BASH, command), cwd=cwd, env=env, seconds=seconds, max_bytes=max_bytes, key=key)


async def run_program(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    seconds: float,
    max_bytes: int,
    key: str = "",
) -> Finished:
    """
    Run a program with its arguments as they are (no shell), to its end, as
    :func:`run_command` runs bash: in a process group of its own, tied to the
    worker's process, stopped past its time, the end of what it wrote kept.

    :param argv: The program, found on ``env``'s PATH, and its arguments.
    :param key: Its step's key, its tie's tag (:func:`stop_leftovers`); empty for none.
    :return: How it ended, whatever its exit code.
    :raises StepFailed: When it can't start, or doesn't finish in time.
    """
    tie = Tie(tag_of(key) if key else "")
    try:
        process = await asyncio.create_subprocess_exec(
            *tie.argv(argv, path=env.get("PATH", os.defpath)),
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A process group of its own, without the worker's terminal; its
            # watcher in it, holding the tie's pipe.
            start_new_session=True,
            pass_fds=tie.fds,
        )
    except FileNotFoundError:
        tie.close()
        raise StepFailed(f"{argv[0]} isn't on this worker's PATH") from None
    except (OSError, ValueError) as error:
        tie.close()
        raise StepFailed(f"{argv[0]} can't start: {getattr(error, 'strerror', None) or error}") from None
    finally:
        tie.started()
    out, err = Tail(max_bytes), Tail(max_bytes)
    reading = asyncio.ensure_future(asyncio.gather(out.read(process.stdout), err.read(process.stderr)))
    timed_out = False
    try:
        try:
            async with asyncio.timeout(seconds):
                await process.wait()
        except TimeoutError:
            timed_out = True
            _signal(process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(GRACE_SECONDS):
                    await process.wait()
            except TimeoutError:
                pass
        # What it left running stops with it.
        _signal(process.pid, signal.SIGKILL)
        await process.wait()
        try:
            # Unless something that left its group holds its output open.
            async with asyncio.timeout(GRACE_SECONDS):
                await asyncio.shield(reading)
        except TimeoutError:
            pass
    finally:
        if process.returncode is None:
            # Stopped itself (its run abandoned): the command stops too.
            _signal(process.pid, signal.SIGKILL)
        reading.cancel()
        # Its group is gone (killed): its watcher has nothing left to watch.
        tie.close()
    ended = finished(process.returncode if process.returncode is not None else -signal.SIGKILL, out, err)
    if timed_out:
        raise StepFailed(
            f"It didn't finish in {seconds:g} seconds",
            details={"stdout": ended.stdout, "stderr": ended.stderr},
        )
    return ended


def tag_of(key: str) -> str:
    """:return: The tag a step's commands carry (their tie's): its key's."""
    return f"{TAG_PREFIX}step={key}"


async def stop_leftovers(key: str) -> int:
    """
    Stop what a step's earlier attempt left running on this machine (its
    worker froze, or lost the run, rather than die): every process group of
    a command tagged with its key, killed, and waited for.

    :return: How many were.
    """
    return await stop_tagged(tag_of(key))


def last_line(text: str, limit: int = 300) -> str:
    """:return: A stream's last line with something on it, cut to ``limit``."""
    for line in reversed(text.splitlines()):
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
