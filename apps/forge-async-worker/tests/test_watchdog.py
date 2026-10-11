"""The watchdog ending a process of its own (watchdog.py): at a deadline its
job outlived, by its thread; and, should another thread hold the interpreter
lock (Python's re backtracking), by faulthandler's backstop, which never needs
it. Each case runs in a process of its own, which it ends."""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time

from forge_async_worker.watchdog import BACKSTOP_SECONDS, HALTED

#: Seconds after it holds its deadline a child's job is due to have ended.
DUE = 0.3


async def ended(job: str) -> tuple[int, float, str]:
    """
    A process that holds a deadline DUE seconds off with the worker's own
    watchdog, then does ``job``.

    :return: Its exit status, seconds from its deadline to its end, and its stderr.
    """
    code = textwrap.dedent(
        f"""
        import re, sys, time
        from forge_async_worker.watchdog import Watchdog

        watchdog = Watchdog()
        due = time.monotonic() + {DUE}
        watchdog.hold("job", due, "its job went on past its deadline")
        print(due, flush=True)
        """
    ) + textwrap.dedent(job)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    at = time.monotonic()  # one clock for every process of the machine
    due = float(out.decode().splitlines()[0])
    assert process.returncode is not None
    return process.returncode, at - due, err.decode()


async def test_the_watchdog_ends_a_process_whose_job_outlived_its_deadline() -> None:
    status, late, err = await ended("time.sleep(30)")

    assert status == HALTED
    assert "the watchdog ends the process: its job went on past its deadline" in err
    assert 0 <= late < 1.0


async def test_the_backstop_ends_a_process_whose_interpreter_lock_another_thread_holds() -> None:
    # Python's re backtracks in C, holding the lock for minutes: the watchdog's thread can't run.
    status, late, err = await ended('re.match(r"^(a|aa)+$", "a" * 45 + "b")')

    assert status == 1  # faulthandler's
    assert "Timeout" in err and "re/__init__.py" in err  # every thread's stack, the stuck one's among them
    assert BACKSTOP_SECONDS <= late < BACKSTOP_SECONDS + 1.5


async def test_a_deadline_released_in_time_ends_nothing() -> None:
    status, _, err = await ended(
        f"""
        watchdog.release("job")
        time.sleep({DUE + BACKSTOP_SECONDS + 0.5})
        """
    )

    assert (status, err) == (0, "")
