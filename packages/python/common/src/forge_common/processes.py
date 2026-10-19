"""
Programs tied to the process that starts them: a program's process group
goes when its starter does, however it goes (``kill -9`` too), on Linux and
macOS alike; and a group a starter left behind is found again, by its tag,
and stopped. Standard library only.

    tie = Tie("forge-step=1f0c…")
    process = await asyncio.create_subprocess_exec(
        *tie.argv(["bash", "-c", "make"], path=env["PATH"]),
        pass_fds=tie.fds, start_new_session=True, env=env, ...
    )
    tie.started()
    ...  # the program runs, ends; its group is killed
    tie.close()

- **How.** The program starts under a small watcher, in its process group
  (the starter starts it in a session of its own). The starter holds the
  writing end of a pipe whose reading end only the watcher has. When the
  starter goes, the kernel closes its end: the watcher reads the pipe's end
  and kills the group, the program and whatever it started. Linux's
  parent-death signal (``PR_SET_PDEATHSIG``) would reach one process, not
  what it started, and macOS has none; the pipe reaches both.
- **The program is the process.** The watcher forks itself off, then execs
  the program in its own process: its PID (``$$``), its exit status and the
  signals sent to it are the program's, as if it had been started alone. The
  watcher, a child of the program's, outlives it until the group is killed
  (its starter's job); then an init reaps it (in a container, run the starter
  with one: Compose's ``init: true``).
- **Found again.** The watcher's arguments carry the tie's tag, so a process
  on the same machine finds what a starter that stopped without dying (frozen,
  its pipe still open) left running, and stops it: :func:`stop_tagged`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = ["TAG_PREFIX", "Tie", "stop_tagged", "tagged_groups"]

#: How every tie's tag starts (``forge-step=<key>``), so a tag never matches
#: an argument of anything else.
TAG_PREFIX = "forge-"
#: Seconds :func:`stop_tagged` waits for what it killed to be gone.
GONE_SECONDS = 5.0

#: The watcher (_watcher.py), run by the starter's Python as a file, with
#: nothing of the starter's site or environment (-I -S): it forks its watching
#: half off, then execs the program.
WATCHER = Path(__file__).with_name("_watcher.py")


class Tie:
    """
    One program's tie to its starter: the pipe its watcher watches. Start the
    program with :meth:`argv` and :attr:`fds` (``pass_fds``) in a session of
    its own (``start_new_session``), call :meth:`started` once it has, and
    :meth:`close` once its group is gone.

    :param tag: What its watcher's arguments carry (:func:`stop_tagged`):
        ``TAG_PREFIX`` and something unique to what runs.
    """

    def __init__(self, tag: str = "") -> None:
        self._read: int | None = None
        self._write: int | None = None
        if tag and not tag.startswith(TAG_PREFIX):
            raise ValueError(f"a tie's tag starts with {TAG_PREFIX!r}")
        self.tag = tag or f"{TAG_PREFIX}tie"
        self._read, self._write = os.pipe()

    @property
    def fds(self) -> tuple[int, ...]:
        """:return: What the program's process is given (``pass_fds``): the pipe's reading end."""
        return () if self._read is None else (self._read,)

    def argv(self, argv: Sequence[str], *, path: str | None = None) -> list[str]:
        """
        :param argv: The program and its arguments.
        :param path: Where the program is looked for when it has no ``/``
            (its environment's PATH); this process's when None.
        :return: What to start instead: the watcher, which execs the program.
        :raises FileNotFoundError: When the program isn't there.
        """
        if self._read is None:
            raise RuntimeError("this tie's program has started already")
        program = argv[0]
        # A program with a path is the program's to find, from where it starts.
        found = program if os.sep in program else shutil.which(program, path=path)
        if found is None:
            raise FileNotFoundError(2, "No such file or directory", program)
        watcher = [sys.executable, "-I", "-S", str(WATCHER), str(self._read), self.tag]
        return [*watcher, found, *argv[1:]]

    def started(self) -> None:
        """The program has started (or failed to): only its watcher keeps the pipe's reading end."""
        if self._read is not None:
            os.close(self._read)
            self._read = None

    def close(self) -> None:
        """Let the watcher go: it kills its group, if any of it is left."""
        self.started()
        if self._write is not None:
            os.close(self._write)
            self._write = None

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            self.close()


def tagged_groups(tag: str) -> set[int]:
    """
    :param tag: A tie's tag.
    :return: The process groups on this machine a watcher with that tag is
        in: what its program left running.
    """
    if not tag.startswith(TAG_PREFIX):
        raise ValueError(f"a tie's tag starts with {TAG_PREFIX!r}")
    found: set[int] = set()
    for pid in _tagged_pids(tag):
        try:
            found.add(os.getpgid(pid))
        except (ProcessLookupError, PermissionError):
            continue
    found.discard(os.getpgid(0))
    return found


async def stop_tagged(tag: str, *, seconds: float = GONE_SECONDS) -> int:
    """
    Stop what a tie with this tag left running on this machine: each process
    group its watcher is in, killed (SIGKILL), and waited for until its
    watcher is gone, ``seconds`` at most.

    :return: How many groups were killed.
    """
    groups = await asyncio.to_thread(tagged_groups, tag)
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue
    if groups:
        for _ in range(max(1, int(seconds / 0.05))):
            if not await asyncio.to_thread(_tagged_pids, tag):
                break
            await asyncio.sleep(0.05)
    return len(groups)


def _tagged_pids(tag: str) -> list[int]:
    # The processes whose arguments carry the tag: from /proc where there is
    # one (Linux; a slim image has no ps), else from ps (macOS).
    proc = Path("/proc")
    if (proc / "self" / "cmdline").exists():
        pids = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                given = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if tag.encode() in given:
                pids.append(int(entry.name))
        return pids
    try:
        listed = subprocess.run(
            ["ps", "-axww", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in listed.splitlines():
        pid, _, arguments = line.strip().partition(" ")
        if pid.isdigit() and tag in arguments.split():
            pids.append(int(pid))
    return pids
