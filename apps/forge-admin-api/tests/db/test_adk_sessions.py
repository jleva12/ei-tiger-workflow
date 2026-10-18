"""ADK's sessions in the admin database (``adk_session_engine``): the
conversations of chat agents and the assistant, and ADK workflow runs'
sessions, on the admin engine's pool at READ COMMITTED.

At MySQL's REPEATABLE READ, a new member's first sessions made together
failed but one: ADK's ``create_session`` makes the member's state row, and
the one that loses the insert re-reads a snapshot from before the winner
committed. The tests marked mysql run on a real MySQL (make
admin-test-mysql); the rest without one.
"""

import asyncio
from uuid import uuid4

import pytest
from google.adk.sessions import DatabaseSessionService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.config import Settings
from forge_admin.db.session import (
    ADK_SESSION_ISOLATION,
    adk_session_engine,
    create_engine,
)

FIRST_SESSIONS = 12


def isolation_of(engine: AsyncEngine) -> str | None:
    return engine.get_execution_options().get("isolation_level")


def test_adk_sessions_are_read_committed_on_the_admin_engines_pool(
    settings: Settings,
) -> None:
    engine = create_engine(settings)  # MySQL, never connected

    sessions = adk_session_engine(engine)

    assert ADK_SESSION_ISOLATION == "READ COMMITTED"
    assert isolation_of(sessions) == ADK_SESSION_ISOLATION
    assert sessions.sync_engine.pool is engine.sync_engine.pool
    assert isolation_of(engine) is None  # every other store: the database's own


def test_sqlite_keeps_its_engine() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")

    assert adk_session_engine(engine) is engine


def test_the_assistants_conversations_are_kept_read_committed(
    settings: Settings,
) -> None:
    runtime = AgentRuntime.create(settings, create_engine(settings))

    assert isinstance(runtime.sessions, DatabaseSessionService)
    assert isolation_of(runtime.sessions.db_engine) == ADK_SESSION_ISOLATION


async def first_sessions(settings: Settings) -> list[BaseException]:
    """A new member's first sessions, FIRST_SESSIONS of them at once (the app's
    state row made before): what failed."""
    engine = create_engine(settings)
    try:
        sessions = DatabaseSessionService(db_engine=adk_session_engine(engine))
        app = f"adk-sessions-{uuid4().hex[:8]}"
        await sessions.create_session(app_name=app, user_id="someone-else")
        member = f"newcomer-{uuid4().hex[:8]}"
        made = await asyncio.gather(
            *(
                sessions.create_session(app_name=app, user_id=member)
                for _ in range(FIRST_SESSIONS)
            ),
            return_exceptions=True,
        )
        listed = await sessions.list_sessions(app_name=app, user_id=member)
        assert len(listed.sessions) == FIRST_SESSIONS - sum(
            isinstance(each, BaseException) for each in made
        )
        return [each for each in made if isinstance(each, BaseException)]
    finally:
        await engine.dispose()


@pytest.mark.mysql
def test_a_new_members_first_sessions_made_together_are_all_made(
    mysql_settings: Settings,
) -> None:
    assert asyncio.run(first_sessions(mysql_settings)) == []


@pytest.mark.mysql
def test_connections_go_back_to_the_pool_at_the_databases_own_isolation(
    mysql_settings: Settings,
) -> None:
    async def levels() -> list[str]:
        # One connection, so ADK's and the next store's are the same one.
        one = mysql_settings.model_copy(
            update={"mysql_pool_size": 1, "mysql_max_overflow": 0}
        )
        engine = create_engine(one)
        query = text("SELECT @@transaction_isolation")
        try:
            seen = []
            for each in (engine, adk_session_engine(engine), engine):
                async with each.connect() as connection:
                    seen.append(str((await connection.execute(query)).scalar_one()))
            return seen
        finally:
            await engine.dispose()

    assert asyncio.run(levels()) == [
        "REPEATABLE-READ",
        "READ-COMMITTED",
        "REPEATABLE-READ",
    ]
