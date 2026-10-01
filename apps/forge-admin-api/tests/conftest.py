import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.cli import seed
from forge_admin.config import Settings
from forge_admin.db.migrate import upgrade
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.models import CasbinRule


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests marked mysql unless make admin-test-mysql runs them."""
    if os.environ.get("FORGE_ADMIN_TEST_MYSQL") == "1":
        return
    skip = pytest.mark.skip(reason="needs MySQL; run make admin-test-mysql")
    for item in items:
        if "mysql" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def settings() -> Settings:
    """Settings whose MySQL is unreachable: nothing listens on port 1."""
    return Settings(
        _env_file=None,
        mysql_host="127.0.0.1",
        mysql_port=1,
        mysql_password="unused",
        mysql_connect_timeout=1,
        ready_timeout=1,
        migrate_on_start=False,
    )


@pytest.fixture
def mysql_settings() -> Settings:
    """Settings for the local admin-mysql (from .env), migrated to head."""
    settings = Settings(migrate_on_start=False)  # pyright: ignore[reportCallIssue]
    upgrade(settings)
    return settings


def run_in_session[T](
    settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]
) -> T:
    """Run ``await work(session)`` against MySQL and return its result."""

    async def main() -> T:
        engine = create_engine(settings)
        try:
            async with create_sessionmaker(engine)() as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@pytest.fixture
def in_db(
    mysql_settings: Settings,
) -> Callable[[Callable[[AsyncSession], Awaitable[object]]], object]:
    """Run ``await work(session)`` against the test MySQL."""
    return lambda work: run_in_session(mysql_settings, work)


@pytest.fixture
def site_admin(mysql_settings: Settings) -> Iterator[str]:
    """A fresh subject holding site:admin on the site, removed afterwards."""
    admin_id = f"admin-{uuid4().hex[:8]}"
    run_in_session(
        mysql_settings, lambda session: seed.make_site_admin(session, admin_id)
    )
    yield admin_id

    async def unassign(session: AsyncSession) -> None:
        await session.execute(delete(CasbinRule).where(CasbinRule.v0 == admin_id))
        await session.commit()

    run_in_session(mysql_settings, unassign)


@pytest.fixture
def client_as(mysql_settings: Settings) -> Iterator[Callable[[str], TestClient]]:
    """Make API clients that act as a given user (the local identity)."""
    clients: list[TestClient] = []

    def make(user_id: str) -> TestClient:
        settings = mysql_settings.model_copy(update={"local_user_id": user_id})
        server = ApiServer(settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS)
        client = TestClient(server.create_app())
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
