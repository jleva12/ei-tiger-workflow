"""
The worker's watchdog: a thread of its own that ends the process when a
run's job outlives what holds it.

A job the worker stops (cancels) can refuse to stop (it swallows the
cancellation), or never get the chance (it holds the event loop), and asyncio
can't make it: only the process ending can. So each running job holds a
deadline here: before its run's lease can run out (moved on with each
renewal), and, once it's asked to stop, the time it has to. Should one pass
while its job still runs, the process ends at once (``os._exit``), before any
other worker may take the run: two attempts at a run never go on at once.
The other runs of the process are cut off with it: their leases run out,
and the maintenance gives them to other workers.

The watchdog is a thread, not a task, so that it works whatever the event
loop does: its deadlines are ``time.monotonic()``'s, the event loop's clock.
Holding and releasing a deadline takes its lock for a moment, never longer.

A thread still needs the interpreter lock to run, and C code can hold that
lock for as long as it goes on (Python's ``re`` backtracking, a C library's
call). So the soonest deadline held is also a backstop's: faulthandler's own
timer (``dump_traceback_later``), a C thread that never takes the lock, ends
the process BACKSTOP_SECONDS after it, writing every thread's stack to
stderr first (it exits with status 1) — unless the watchdog's thread got to
end it at the deadline, saying why (status HALTED).
"""

from __future__ import annotations

import faulthandler
import os
import threading
import time
from collections.abc import Callable

#: The exit status of a worker process the watchdog ended (EX_SOFTWARE).
HALTED = 70
#: Seconds past a deadline the backstop ends the process, should the
#: watchdog's thread not have got to by then (it couldn't take the
#: interpreter lock).
BACKSTOP_SECONDS = 1.0


def end_process(why: str) -> None:
    """Ends the process at once, saying why on stderr (not through logging,
    whose locks a stuck job may hold): nothing of it goes on."""
    try:
        os.write(2, f"forge-async-worker: the watchdog ends the process: {why}\n".encode())
    finally:
        os._exit(HALTED)


class Watchdog:
    """
    Ends the process when a deadline held here passes before it's released.

    :param halt: What ends the process, saying why: ``end_process``, backed
        by faulthandler's timer; tests record it instead, with no backstop
        (a watchdog that halted watches no more, as the process would be gone).
    """

    def __init__(self, halt: Callable[[str], None] | None = None) -> None:
        self._halt = halt if halt is not None else end_process
        self._backstop = halt is None
        self._held: dict[object, tuple[float, str]] = {}
        self._changed = threading.Condition()
        self._thread: threading.Thread | None = None
        #: Why it ended the process, once it did.
        self.halted: str | None = None

    def hold(self, holder: object, deadline: float, why: str) -> None:
        """
        ``holder``'s job must have ended by ``deadline`` (``time.monotonic()``),
        else the process ends saying ``why``. Held again, the new deadline
        replaces the old.
        """
        with self._changed:
            if self.halted is not None:
                return
            self._held[holder] = (deadline, why)
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="forge-watchdog", daemon=True)
                self._thread.start()
            self._arm()
            self._changed.notify()

    def release(self, holder: object) -> None:
        """``holder``'s job ended: its deadline no longer holds."""
        with self._changed:
            if self._held.pop(holder, None) is not None:
                self._arm()
                self._changed.notify()

    def holding(self, holder: object) -> float | None:
        """:return: ``holder``'s deadline, while it holds one."""
        with self._changed:
            held = self._held.get(holder)
        return held[0] if held is not None else None

    def _arm(self) -> None:
        """Sets the backstop for the soonest deadline held, or none (with the lock held)."""
        if not self._backstop:
            return
        if not self._held:
            faulthandler.cancel_dump_traceback_later()
            return
        soonest = min(deadline for deadline, _ in self._held.values())
        left = max(soonest - time.monotonic(), 0.0)
        faulthandler.dump_traceback_later(left + BACKSTOP_SECONDS, exit=True, file=2)

    def _watch(self) -> None:
        with self._changed:
            while True:
                if not self._held:
                    self._changed.wait()
                    continue
                deadline, why = min(self._held.values())
                left = deadline - time.monotonic()
                if left <= 0:
                    self._held.clear()
                    self.halted = why
                    break
                self._changed.wait(timeout=left)
        self._halt(why)
