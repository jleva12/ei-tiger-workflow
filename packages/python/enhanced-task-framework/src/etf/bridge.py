"""Sync -> async bridge for hosts that are not asyncio-native.

ETF (and the default Beanie/Motor store) are asyncio-native, but many hosts are
synchronous: thread- or process-pool task workers, WSGI web apps, CLIs, schedulers.
:class:`AsyncBridge` runs a single background event loop so synchronous code can drive
ETF and reuse one Motor client / store across calls instead of spinning up a loop per
call.

**One loop per process.** The Motor client is bound to the event loop it is first used
on, so a process must have exactly one bridge: run ``store.initialize()`` through it
(``bridge.run(store.initialize())``) and route *every* ETF call through that same
bridge. Two bridges over one store produce Motor's "attached to a different loop"
failures (the store also fail-fasts with a clearer message, see ``BeanieStateStore``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class AsyncBridge:
    """Runs a private event loop on a background thread so synchronous code can drive
    ETF's asyncio core.

    One bridge per process (see the module docstring): initialize the store through it
    and route every ETF call through the same instance. A closed bridge cannot be
    restarted — create a new one.
    """

    def __init__(self, *, thread_name: str = "etf-async-bridge", cancel_grace: float = 5.0) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=thread_name, daemon=True)
        self._started = False
        self._closed = False
        #: How long an interrupted coroutine gets to unwind after cancellation, so its
        #: ``finally`` blocks (lock release, fail-fast run transition) actually run.
        self._cancel_grace = cancel_grace

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> "AsyncBridge":
        if self._closed:
            raise RuntimeError("bridge closed; create a new AsyncBridge")
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def run(self, coro: Awaitable[Any], *, timeout: float | None = None) -> Any:
        """Run ``coro`` on the bridge loop and block for its result.

        ``timeout`` is the cancellation threshold (seconds); on expiry ``TimeoutError``
        is raised after cleanup. When the host enforces its own time limit, set this
        just under it so ETF normally winds down first. When the wait is interrupted —
        timeout, a host time-limit exception, ``KeyboardInterrupt`` — the in-flight
        coroutine is cancelled and cleanup is drained before the interruption
        propagates, so a cancelled run is fail-fasted to FAILED (recoverable) instead
        of being stranded RUNNING. Cancellation-safe blocking work may take longer
        than ``timeout`` and ``cancel_grace`` to unwind; a process-level kill is the
        upper bound, backstopped by ``JobOperator.recover_stale_runs``.
        """
        if self._closed:
            raise RuntimeError("bridge closed; create a new AsyncBridge")
        if not self._started:
            self.start()

        task_box: dict[str, asyncio.Task[Any]] = {}
        cancelled_before_start = threading.Event()

        async def _tracked() -> Any:
            if cancelled_before_start.is_set():
                close = getattr(coro, "close", None)
                if close is not None:
                    close()
                raise asyncio.CancelledError
            task_box["task"] = asyncio.current_task()  # type: ignore[assignment]
            return await coro

        future = asyncio.run_coroutine_threadsafe(_tracked(), self._loop)
        try:
            return future.result(timeout)
        except BaseException as exc:
            # Cancel the submission itself even if the loop has not had a chance to
            # create ``_tracked`` yet.  Merely cancelling task_box["task"] leaves a
            # pre-start race in which timed-out work executes later.
            cancelled_before_start.set()
            task = task_box.get("task")
            if task is not None and not task.done():  # interrupted, not failed
                # Cancel with the reason before cancelling the future: the future's
                # own cancel reaches the task without one, and the first cancel wins.
                reason = (
                    f"AsyncBridge.run timed out after {timeout}s"
                    if isinstance(exc, TimeoutError)
                    else f"AsyncBridge.run interrupted by {type(exc).__name__}"
                )
                self._cancel_and_drain(
                    [task],
                    self._cancel_grace,
                    wait_after_grace=True,
                    reason=reason,
                )
            future.cancel()
            if task is None:
                close = getattr(coro, "close", None)
                if close is not None:
                    close()
            if isinstance(exc, concurrent.futures.CancelledError) and not isinstance(
                exc, asyncio.CancelledError
            ):
                # Python 3.14 split these classes (aliases on <=3.13); normalize so
                # callers can always catch asyncio.CancelledError.
                raise asyncio.CancelledError() from exc
            raise

    def _cancel_and_drain(
        self,
        tasks: list[asyncio.Task[Any]],
        grace: float,
        *,
        wait_after_grace: bool = False,
        reason: str | None = None,
    ) -> bool:
        """Cancel loop tasks from outside the loop and drain their cleanup.

        ``grace`` is the warning threshold. When ``wait_after_grace`` is true, safety
        wins over the caller's timeout: keep waiting so cancellation-safe blocking work
        cannot outlive the synchronous call that submitted it. ``reason`` becomes the
        cancellation message, so a run failed by it records why it was cancelled.
        """

        async def _drain() -> None:
            for t in tasks:
                t.cancel(reason)
            await asyncio.gather(*tasks, return_exceptions=True)

        drain: concurrent.futures.Future[None] | None = None
        try:
            drain = asyncio.run_coroutine_threadsafe(_drain(), self._loop)
            drain.result(grace)
            return True
        except concurrent.futures.TimeoutError:
            logger.warning(
                "bridge tasks did not finish unwinding within %.1fs",
                grace,
            )
            if wait_after_grace and drain is not None:
                drain.result()
                return True
            return False
        except BaseException:
            logger.warning("bridge task draining failed", exc_info=True)
            return False

    def close(
        self,
        *,
        drain_timeout: float = 5.0,
        finalizer: Callable[[], Awaitable[Any]] | None = None,
        finalizer_timeout: float = 5.0,
    ) -> None:
        """Drain work, run an async resource finalizer, then stop the loop.

        Idempotent. After ``close()`` the bridge is unusable: ``start()``/``run()``
        raise ``RuntimeError`` (the loop thread cannot be restarted). ``finalizer`` is
        invoked only after in-flight tasks have unwound, so a store remains available
        to their fail-fast/checkpoint cleanup and closes immediately before loop stop.
        """
        if self._closed:
            return
        self._closed = True
        if not self._started:
            if finalizer is not None:
                self._loop.run_until_complete(finalizer())
            self._loop.close()
            return

        async def _pending() -> list[asyncio.Task[Any]]:
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        try:
            tasks = asyncio.run_coroutine_threadsafe(_pending(), self._loop).result(1.0)
            if tasks:
                self._cancel_and_drain(
                    tasks,
                    drain_timeout,
                    wait_after_grace=True,
                    reason="AsyncBridge closing",
                )
        except BaseException:
            logger.warning("could not drain bridge tasks during close", exc_info=True)
        finalizer_error: BaseException | None = None
        if finalizer is not None:

            async def _finalize() -> Any:
                return await finalizer()

            try:
                asyncio.run_coroutine_threadsafe(
                    _finalize(),
                    self._loop,
                ).result(finalizer_timeout)
            except BaseException as exc:
                finalizer_error = exc
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()
        self._started = False
        if finalizer_error is not None:
            raise finalizer_error


__all__ = ["AsyncBridge"]
