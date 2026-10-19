"""Runner eviction cannot race concurrent builds, turns, or shutdown."""

import asyncio
from typing import Any

import pytest
from google.adk.events import Event

import forge_agent_runtime.executor as runtime
from forge_agent_runtime import AgentExecutor, RunRequest
from tests.test_executor_snapshots import CHILD, PARENT, SECRET, ChangingSource, document


class RecordingRunner:
    def __init__(self, *, app, **kwargs: Any) -> None:
        self.app_name = app.name
        self.close_started = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.close_allowed.set()
        self.run_started = asyncio.Event()
        self.run_allowed = asyncio.Event()
        self.running = False
        self.closed = 0

    async def close(self) -> None:
        assert not self.running, "An active runner's connections were closed"
        self.close_started.set()
        await self.close_allowed.wait()
        self.closed += 1

    async def run_async(self, **kwargs: Any):
        assert not self.closed
        self.running = True
        self.run_started.set()
        try:
            yield Event(author=self.app_name)
            await self.run_allowed.wait()
            assert not self.closed
        finally:
            self.running = False


@pytest.fixture
def cached(models, web, monkeypatch):
    monkeypatch.setattr(runtime, "Runner", RecordingRunner)
    source = ChangingSource()
    source.documents = {key: document(key) for key in (PARENT, CHILD, SECRET)}
    return AgentExecutor(source, models=models, http=web(), cache_size=1)


@pytest.mark.timeout(5)
async def test_slow_eviction_does_not_race_independent_builds(cached):
    first, _ = await cached.runner(PARENT)
    first.close_allowed.clear()
    building = asyncio.create_task(cached.runner(CHILD))
    await first.close_started.wait()
    last, _ = await cached.runner(SECRET)
    first.close_allowed.set()
    middle, _ = await building
    await cached.close()
    assert first.closed == middle.closed == last.closed == 1
    assert not cached._building


@pytest.mark.timeout(5)
async def test_active_turn_is_retained_through_eviction_and_shutdown(cached):
    first, _ = await cached.runner(PARENT)

    async def turn():
        async for _ in cached.run(RunRequest(app_name=PARENT, user_id="ada", session_id="s1")):
            pass

    running = asyncio.create_task(turn())
    await first.run_started.wait()
    second, _ = await cached.runner(CHILD)
    assert not first.close_started.is_set()
    shutting_down = asyncio.create_task(cached.close())
    await asyncio.sleep(0)
    assert not shutting_down.done()
    assert not first.close_started.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        await cached.runner(SECRET)
    first.run_allowed.set()
    await running
    await shutting_down
    assert first.closed == second.closed == 1
    assert not cached._active


@pytest.mark.timeout(5)
async def test_cancelling_a_turn_releases_its_runner(cached):
    first, _ = await cached.runner(PARENT)

    async def turn():
        async for _ in cached.run(RunRequest(app_name=PARENT, user_id="ada", session_id="s1")):
            pass

    running = asyncio.create_task(turn())
    await first.run_started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not first.running
    assert not cached._active
    await cached.close()
    assert first.closed == 1


@pytest.mark.timeout(5)
async def test_cancelled_same_key_waiter_does_not_drop_the_build_lock(cached, monkeypatch):
    build = runtime.build_app
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def paused(*args, **kwargs):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return await build(*args, **kwargs)

    monkeypatch.setattr(runtime, "build_app", paused)
    first = asyncio.create_task(cached.runner(PARENT))
    await started.wait()
    waiter = asyncio.create_task(cached.runner(PARENT))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    third = asyncio.create_task(cached.runner(PARENT))
    await asyncio.sleep(0)
    assert count == 1
    release.set()
    a, b = await asyncio.gather(first, third)
    assert a[0] is b[0]
    assert not cached._building
    await cached.close()


async def test_failed_builds_release_lock_and_pending_shutdown_state(cached, monkeypatch):
    build = runtime.build_app

    async def fail(*args, **kwargs):
        raise runtime.BuildError("Broken graph")

    monkeypatch.setattr(runtime, "build_app", fail)
    with pytest.raises(runtime.BuildError, match="Broken graph"):
        await cached.runner(PARENT)
    assert not cached._building
    assert cached._pending == 0
    monkeypatch.setattr(runtime, "build_app", build)
    runner, _ = await cached.runner(PARENT)
    await cached.close()
    assert runner.closed == 1
