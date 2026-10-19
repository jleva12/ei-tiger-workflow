"""
Programs tied to their starter (forge_common.processes): started as
themselves, gone with their starter however it goes, and found again by
their tag while it lives on, frozen.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forge_common.processes import Tie, stop_tagged, tagged_groups


async def tied(command: str, tag: str = "") -> tuple[int, int, str]:
    """Runs bash ``command`` tied to this process: its pid, exit status and stdout."""
    tie = Tie(tag)
    process = await asyncio.create_subprocess_exec(
        *tie.argv(["bash", "-c", command]),
        pass_fds=tie.fds,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
    )
    tie.started()
    out, _ = await process.communicate()
    os.killpg(process.pid, signal.SIGKILL)  # its watcher, as its starter ends a command
    tie.close()
    assert process.returncode is not None
    return process.pid, process.returncode, out.decode().strip()


async def test_the_program_is_its_process_as_if_started_alone() -> None:
    pid, code, said = await tied("echo $$; exit 3")
    assert (code, said) == (3, str(pid))
    # A signal it dies of is its own.
    assert (await tied("kill -TERM $$"))[1] == -signal.SIGTERM
    # SIGPIPE as programs expect it (Python ignores it).
    assert (await tied("yes | head -1 >/dev/null; echo ${PIPESTATUS[0]}"))[2] == "141"


def test_a_program_that_isnt_there_is_refused_before_it_starts() -> None:
    with pytest.raises(FileNotFoundError):
        Tie().argv(["no-such-program-here"], path="/nonexistent")
    with pytest.raises(ValueError, match="starts with 'forge-'"):
        Tie("step-1")


def starter(tmp_path: Path, tag: str, command: str) -> tuple[subprocess.Popen[str], int]:
    """A Python process that starts ``command`` tied to it, and says its pid."""
    code = (
        "import asyncio, sys\n"
        "from forge_common.processes import Tie\n"
        "async def main():\n"
        f"    tie = Tie({tag!r})\n"
        f"    p = await asyncio.create_subprocess_exec(*tie.argv(['bash', '-c', {command!r}]),\n"
        "        pass_fds=tie.fds, start_new_session=True)\n"
        "    tie.started()\n"
        "    print(p.pid, flush=True)\n"
        "    await asyncio.sleep(60)\n"
        "asyncio.run(main())\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, cwd=tmp_path
    )
    assert process.stdout is not None
    return process, int(process.stdout.readline())


def running(pid: int) -> bool:
    """Whether a process is there and not a zombie (dead, its parent yet to reap it)."""
    status = Path(f"/proc/{pid}/stat")
    if status.parent.parent.joinpath("self").exists():
        try:
            return status.read_text().rpartition(")")[2].split()[0] != "Z"
        except OSError:
            return False
    found = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(found.stdout.strip()) and not found.stdout.strip().startswith("Z")


def gone(pid: int, seconds: float = 5) -> bool:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if not running(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_program_and_what_it_started_go_with_their_starter(tmp_path: Path) -> None:
    done = tmp_path / "done"
    process, pid = starter(tmp_path, "forge-test=dies", f"sleep 30 & sleep 1; touch {done}; wait")
    time.sleep(0.3)

    process.kill()  # kill -9: nothing of the starter's runs again
    process.wait()

    assert gone(pid)
    time.sleep(1.2)
    assert not done.exists()
    assert tagged_groups("forge-test=dies") == set()


async def test_what_a_frozen_starter_left_running_is_found_and_stopped(tmp_path: Path) -> None:
    done = tmp_path / "done"
    process, pid = starter(tmp_path, "forge-test=frozen", f"sleep 1; touch {done}")
    try:
        await asyncio.sleep(0.3)
        process.send_signal(signal.SIGSTOP)  # its pipe stays open: the tie can't tell

        assert tagged_groups("forge-test=frozen") == {pid}
        assert await stop_tagged("forge-test=frozen") == 1

        assert gone(pid)
        await asyncio.sleep(1.2)
        assert not done.exists()
        assert await stop_tagged("forge-test=frozen") == 0
    finally:
        process.kill()
        process.wait()
