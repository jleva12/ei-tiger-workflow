"""
The integration suite: ADK workflow runs on the real machinery, against a
real MySQL 8 (the admin API's migrations applied) and Redis, with real worker
processes, several at once. Opt-in: every test here is marked ``integration``
and skipped unless the suite's services are named:

    FORGE_IT_MYSQL_URL   mysql+aiomysql://forge_admin:local-it@127.0.0.1:13346/forge_admin
    FORGE_IT_REDIS_URL   redis://127.0.0.1:16409/0
    FORGE_IT_COMPOSE     the docker compose command of their project (the Redis outage stops and starts it)

``make async-worker-test-integration`` brings them up (tests/integration/compose.yaml),
migrates the database and runs the suite (IT_ARGS="-k claim" picks scenarios).
Each test clears the tables and Redis first: never point these at a database
or Redis anything else uses.

Each scenario's timeline (the test's actions, the mock server's requests,
the runs' activity and the workers' warnings, by time) and what it observed
are written to FORGE_IT_REPORT_DIR (default tests/integration/.reports), and
shown with a failure. A known failure is marked xfail, its reason naming the
finding (IT-n, listed in the repo's CURRENT_STATE.md): a fix turns its test
green (XPASS, strict).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from .harness.mock_server import MockServer
from .harness.scenario import Scenario, Services, opened

HERE = Path(__file__).resolve().parent
MYSQL_URL = os.environ.get("FORGE_IT_MYSQL_URL", "")
REDIS_URL = os.environ.get("FORGE_IT_REDIS_URL", "")
COMPOSE = os.environ.get("FORGE_IT_COMPOSE", f"docker compose -f {HERE / 'compose.yaml'}")
REPORTS = Path(os.environ.get("FORGE_IT_REPORT_DIR") or HERE / ".reports")
NOT_SET = "needs the integration suite's MySQL and Redis (make async-worker-test-integration)"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason=NOT_SET)
    for item in items:
        if HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)
            if not (MYSQL_URL and REDIS_URL):
                item.add_marker(skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    report = yield
    # Kept for the scenario fixture's teardown, which reports how the test went.
    if report.when == "call":
        item.stash[_OUTCOME] = "xfailed" if hasattr(report, "wasxfail") and report.skipped else report.outcome
    return report


_OUTCOME = pytest.StashKey[str]()


@pytest.fixture(scope="session")
def services() -> Services:
    return Services(mysql_url=MYSQL_URL, redis_url=REDIS_URL, compose=tuple(shlex.split(COMPOSE)))


@pytest.fixture(scope="session")
def reports() -> Path:
    """Where scenarios' reports go."""
    return REPORTS


@pytest.fixture(scope="session")
def mock() -> Iterator[MockServer]:
    server = MockServer().start()
    yield server
    server.close()


@pytest.fixture
async def scenario(
    services: Services, mock: MockServer, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Scenario]:
    """One test's scenario (``harness.scenario.opened``), its report named after the test."""
    async with opened(
        services,
        mock,
        tmp_path,
        name=request.node.name.replace("/", "_"),
        reports=REPORTS,
        outcome=lambda: request.node.stash.get(_OUTCOME, "error"),
    ) as world:
        yield world
    request.node.add_report_section("teardown", "timeline", world.report)
