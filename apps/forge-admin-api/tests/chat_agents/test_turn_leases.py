"""Conversations' turn leases in the admin database (``chat_turn_leases``),
on SQLite with a connection per caller, as two processes would have: one
turn at a time in a conversation, across processes."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from forge_agent_runtime.leases import TurnBusy, TurnKey, TurnLease
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlite_tables import create

from forge_admin.chat_agents.turn_leases import SqlTurnLeases
from forge_admin.models import ChatTurnLease

pytestmark = pytest.mark.asyncio

KEY = TurnKey("ca_desk", "ada", "s1")
START = datetime(2026, 10, 18, 12, 0)


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}", poolclass=NullPool
    )
    await create(made, ChatTurnLease)
    yield made
    await made.dispose()


def leases(engine: AsyncEngine, clock: Callable[[], datetime]) -> SqlTurnLeases:
    """A process's leases: its own sessions, the one database."""
    return SqlTurnLeases(
        async_sessionmaker(engine, expire_on_commit=False), clock=clock
    )


async def test_a_lease_is_one_turns_until_given_back_or_run_out(
    engine: AsyncEngine,
) -> None:
    now = [START]
    one, two = leases(engine, lambda: now[0]), leases(engine, lambda: now[0])
    assert await one.acquire(KEY, owner="a", seconds=30)
    # Another process: refused while it's held, and can't renew or give it back.
    assert not await two.acquire(KEY, owner="b", seconds=30)
    assert not await two.renew(KEY, owner="b", seconds=30)
    await two.release(KEY, owner="b")
    assert not await two.acquire(KEY, owner="b", seconds=30)
    # Its owner renews it, and takes it again.
    assert await one.renew(KEY, owner="a", seconds=30)
    assert await one.acquire(KEY, owner="a", seconds=30)
    # Another conversation is another lease.
    assert await two.acquire(TurnKey("ca_desk", "ada", "s2"), owner="b", seconds=30)
    # Run out (its process stopped): the next turn takes it; the first can't renew it.
    now[0] = START + timedelta(seconds=31)
    assert await two.acquire(KEY, owner="b", seconds=30)
    assert not await one.renew(KEY, owner="a", seconds=30)
    await two.release(KEY, owner="b")
    assert await one.acquire(KEY, owner="c", seconds=30)


async def test_turns_racing_for_a_conversation_get_one_lease(
    engine: AsyncEngine,
) -> None:
    processes = [leases(engine, lambda: START) for _ in range(6)]
    taken = await asyncio.gather(
        *(
            each.acquire(KEY, owner=f"o{n}", seconds=30)
            for n, each in enumerate(processes)
        )
    )
    assert sum(taken) == 1


async def test_a_turn_in_another_process_is_refused_until_the_first_is_over(
    engine: AsyncEngine,
) -> None:
    one, two = leases(engine, START.now), leases(engine, START.now)
    first = await TurnLease.take(one, KEY, wait=0)
    with pytest.raises(TurnBusy):
        await TurnLease.take(two, KEY, wait=0.1)
    await first.release()
    second = await TurnLease.take(two, KEY, wait=0)
    await second.release()
