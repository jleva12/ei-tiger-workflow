"""Integration tests against a real MySQL.

make admin-test-mysql starts the local admin-mysql service and runs these
with FORGE_ADMIN_TEST_MYSQL=1, connecting as .env and the FORGE_ADMIN_MYSQL_*
environment describe.
"""

import asyncio
import os

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from forge_admin.api.server import ApiServer
from forge_admin.config import Settings
from forge_admin.db.migrate import alembic_config, upgrade
from forge_admin.db.session import create_engine

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(
        os.environ.get("FORGE_ADMIN_TEST_MYSQL") != "1",
        reason="needs MySQL; run make admin-test-mysql",
    ),
]


@pytest.fixture
def settings() -> Settings:
    return Settings(migrate_on_start=False)  # pyright: ignore[reportCallIssue]


async def current_revision(settings: Settings) -> str:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            return str(result.scalar_one())
    finally:
        await engine.dispose()


def test_migrations_reach_head_and_repeat_safely(settings: Settings) -> None:
    upgrade(settings)
    upgrade(settings)
    head = ScriptDirectory.from_config(alembic_config(settings)).get_current_head()
    assert asyncio.run(current_revision(settings)) == head


def test_ready_with_mysql(settings: Settings) -> None:
    with TestClient(ApiServer(settings).create_app()) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
