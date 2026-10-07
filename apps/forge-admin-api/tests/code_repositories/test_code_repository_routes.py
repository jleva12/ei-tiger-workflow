"""An organization's code repositories' routes, without MySQL: the tables on
SQLite (foreign keys enforced, as MySQL does) and the organization's access
in memory (the default grants). The code graph worker is played by writing
to its queue's rows as it does.

The people: an organization admin, a member and a viewer of the
organization, a member of another, and an outsider.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import MetaData, event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.models import CodeIngestionJob, CodeRepository, Organization

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER, NEIGHBOUR, OUTSIDER = (
    "admin-1",
    "member-1",
    "viewer-1",
    "neighbour-1",
    "outsider-1",
)
REPOS = f"/organizations/{ORG}/code-repositories"
OTHER_REPOS = f"/organizations/{OTHER_ORG}/code-repositories"
WIDGETS = "https://github.com/Acme/Widgets"
WIDGETS_URL = "https://github.com/acme/widgets"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
MISSING = "3f6c0000-0000-4000-8000-00000000ffff"

GRANTS = {
    "org:admin": ("organizations:read", "repositories:manage"),
    "org:member": ("organizations:read", "repositories:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:member", OTHER_ORG),
}


class Grants(AsyncAdapter):
    """The policy casbin_rule would hold: the default grants, and each
    person's role."""

    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{org}*" for user, (role, org) in ROLES.items()]

    async def load_policy(self, model: Any) -> None:
        for line in self.lines:
            load_policy_line(line, model)

    async def save_policy(self, model: Any) -> bool:
        return True

    async def add_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: str
    ) -> None:
        pass


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    tokens: dict[str, str]

    def call(
        self, method: str, path: str, user: str = MEMBER, body: Any = None
    ) -> Response:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
        )

    def added(
        self,
        url: str = WIDGETS,
        branch: str = "main",
        *,
        ingest: bool = True,
        user: str = MEMBER,
        path: str = REPOS,
    ) -> dict[str, Any]:
        answer = self.call(
            "POST", path, user, {"url": url, "branch": branch, "ingest": ingest}
        )
        assert answer.status_code == 201, answer.json()
        return dict(answer.json())

    def run[T](self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async def main() -> T:
            async with self.sessions() as session:
                return await work(session)

        assert self.client.portal is not None
        return self.client.portal.call(main)

    def worker_writes(self, job_id: str, **values: Any) -> None:
        """Change a job's row as the code graph worker does."""

        async def write(session: AsyncSession) -> None:
            await session.execute(
                update(CodeIngestionJob)
                .where(CodeIngestionJob.id == job_id)
                .values(**values)
            )
            await session.commit()

        self.run(write)

    def jobs(self, repository_id: str) -> int:
        async def count(session: AsyncSession) -> int:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(CodeIngestionJob)
                    .where(CodeIngestionJob.repository_id == repository_id)
                )
                or 0
            )

        return self.run(count)


def sqlite_tables() -> MetaData:
    """The tables the routes use, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in ("organizations", "code_repositories", "code_ingestion_jobs"):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection: Any, _: Any) -> None:
        # As MySQL does: removing a repository removes its ingestions.
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Grants())

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add_all(
                [
                    Organization(id=ORG, name="Acme", description=""),
                    Organization(id=OTHER_ORG, name="Globex", description=""),
                ]
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (ADMIN, MEMBER, VIEWER, NEIGHBOUR, OUTSIDER)
    }
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(create)
        yield Workspace(client, sessions, tokens)
        client.portal.call(engine.dispose)


def ingestions_of(repository_id: str, path: str = REPOS) -> str:
    return f"{path}/{repository_id}/ingestions"


# ------------------------------------------------------------ repositories


def test_adding_a_repository_queues_its_first_ingestion(workspace: Workspace) -> None:
    added = workspace.added()
    assert added["url"] == WIDGETS_URL
    assert (added["owner"], added["name"], added["branch"]) == (
        "Acme",
        "Widgets",
        "main",
    )
    assert added["organization_id"] == ORG
    job = added["latest_ingestion"]
    assert job["status"] == "QUEUED"
    assert (job["url"], job["branch"], job["commit_sha"]) == (WIDGETS_URL, "main", None)
    assert job["requested_by"] == MEMBER
    assert job["eligible_at"] is not None
    assert added["last_success"] is None
    listed = workspace.call("GET", REPOS, VIEWER)
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [added["id"]]


def test_a_repository_can_be_added_without_ingesting_it(workspace: Workspace) -> None:
    added = workspace.added(ingest=False)
    assert added["latest_ingestion"] is None
    assert workspace.jobs(added["id"]) == 0


def test_urls_and_branches_must_be_ones_the_worker_takes(workspace: Workspace) -> None:
    for url in (
        "https://gitlab.com/acme/widgets",
        "https://github.com/acme/widgets/tree/main",
        "git@github.com:acme/widgets.git",
    ):
        answer = workspace.call("POST", REPOS, body={"url": url, "branch": "main"})
        assert answer.status_code == 422, (url, answer.json())
    for branch in ("main@{1}", "a..b", "-x"):
        answer = workspace.call("POST", REPOS, body={"url": WIDGETS, "branch": branch})
        assert answer.status_code == 422, (branch, answer.json())


def test_an_organization_has_a_url_once(workspace: Workspace) -> None:
    workspace.added()
    again = workspace.call(
        "POST",
        REPOS,
        body={"url": "https://github.com/acme/widgets.git", "branch": "main"},
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "The organization already has this repository"


def test_organizations_share_a_repositorys_graph_and_its_branch(
    workspace: Workspace,
) -> None:
    workspace.added(branch="develop")
    other = workspace.call(
        "POST", OTHER_REPOS, NEIGHBOUR, {"url": WIDGETS, "branch": "main"}
    )
    assert other.status_code == 409
    assert "follows branch 'develop'" in other.json()["detail"]
    same = workspace.added(branch="develop", user=NEIGHBOUR, path=OTHER_REPOS)
    assert same["organization_id"] == OTHER_ORG
    # Each organization sees only its own.
    assert len(workspace.call("GET", REPOS, VIEWER).json()) == 1
    assert len(workspace.call("GET", OTHER_REPOS, NEIGHBOUR).json()) == 1


def test_access(workspace: Workspace) -> None:
    added = workspace.added()
    one = f"{REPOS}/{added['id']}"
    assert workspace.call("GET", one, VIEWER).status_code == 200
    denied = workspace.call("POST", REPOS, VIEWER, {"url": WIDGETS, "branch": "main"})
    assert (denied.status_code, denied.json()["detail"]) == (
        403,
        "Requires repositories:manage",
    )
    assert (
        workspace.call("POST", ingestions_of(added["id"]), VIEWER, {}).status_code
        == 403
    )
    assert workspace.call("DELETE", one, VIEWER).status_code == 403
    outsider = workspace.call("GET", REPOS, OUTSIDER)
    assert (outsider.status_code, outsider.json()["detail"]) == (
        403,
        "Requires organizations:read",
    )
    # Another organization's repository isn't there, even by its ID.
    elsewhere = workspace.call("GET", f"{OTHER_REPOS}/{added['id']}", NEIGHBOUR)
    assert elsewhere.status_code == 404
    assert workspace.call("GET", f"{REPOS}/{MISSING}", VIEWER).status_code == 404
    assert workspace.call("GET", f"{REPOS}/not-a-uuid", VIEWER).status_code == 422


def test_removing_a_repository_removes_its_ingestions(workspace: Workspace) -> None:
    added = workspace.added()
    assert workspace.jobs(added["id"]) == 1
    gone = workspace.call("DELETE", f"{REPOS}/{added['id']}", ADMIN)
    assert gone.status_code == 204
    assert workspace.jobs(added["id"]) == 0
    assert workspace.call("GET", REPOS, VIEWER).json() == []


# -------------------------------------------------------------- ingestions


def test_a_repository_has_one_ingestion_queued_or_running(
    workspace: Workspace,
) -> None:
    added = workspace.added()
    first = added["latest_ingestion"]
    again = workspace.call("POST", ingestions_of(added["id"]), body={})
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    other_commit = workspace.call(
        "POST", ingestions_of(added["id"]), body={"commit": SHA}
    )
    assert other_commit.status_code == 409
    # Once it ended, another is queued.
    workspace.worker_writes(first["id"], status="SUCCEEDED", eligible_at=None)
    queued = workspace.call("POST", ingestions_of(added["id"]), body={"commit": SHA})
    assert queued.status_code == 202
    assert queued.json()["commit_sha"] == SHA
    assert queued.json()["id"] != first["id"]
    bad = workspace.call("POST", ingestions_of(added["id"]), body={"commit": "main"})
    assert bad.status_code == 422


def test_ingestions_are_listed_newest_first(workspace: Workspace) -> None:
    added = workspace.added()
    first = added["latest_ingestion"]["id"]
    workspace.worker_writes(first, status="SUCCEEDED", eligible_at=None)
    second = workspace.call("POST", ingestions_of(added["id"]), body={}).json()["id"]
    listed = workspace.call("GET", ingestions_of(added["id"]), VIEWER)
    assert [j["id"] for j in listed.json()] == [second, first]
    one = workspace.call("GET", f"{ingestions_of(added['id'])}/{first}", VIEWER)
    assert one.status_code == 200 and one.json()["status"] == "SUCCEEDED"
    other = workspace.added("https://github.com/acme/gadgets")
    wrong = workspace.call("GET", f"{ingestions_of(other['id'])}/{first}", VIEWER)
    assert wrong.status_code == 404


def test_a_repository_shows_its_latest_success(workspace: Workspace) -> None:
    added = workspace.added()
    first = added["latest_ingestion"]["id"]
    workspace.worker_writes(
        first,
        status="SUCCEEDED",
        eligible_at=None,
        commit_sha=SHA,
        codegraph_repository_id="repo:abc",
        run_id="run-1",
        generation=1,
        metrics={"files": 9, "nodes": 545, "edges": 2086},
    )
    workspace.call("POST", ingestions_of(added["id"]), body={})
    shown = workspace.call("GET", f"{REPOS}/{added['id']}", VIEWER).json()
    assert shown["latest_ingestion"]["status"] == "QUEUED"
    success = shown["last_success"]
    assert success["id"] == first
    assert success["metrics"] == {"files": 9, "nodes": 545, "edges": 2086}
    assert (success["codegraph_repository_id"], success["generation"]) == (
        "repo:abc",
        1,
    )


def test_a_failed_ingestion_can_be_retried(workspace: Workspace) -> None:
    added = workspace.added()
    job = added["latest_ingestion"]["id"]
    retry = f"{ingestions_of(added['id'])}/{job}/retry"
    assert workspace.call("POST", retry).status_code == 409
    workspace.worker_writes(
        job,
        status="FAILED",
        eligible_at=None,
        attempts=6,
        claim_token=7,
        commit_sha=OTHER_SHA,
        run_id="run-1",
        error_code="repository_unreadable",
        error_message="could not read the repository",
    )
    retried = workspace.call("POST", retry)
    assert retried.status_code == 202
    body = retried.json()
    assert (body["status"], body["attempts"], body["error_code"]) == ("QUEUED", 0, "")
    # It ingests the same commit and resumes its run.
    assert (body["commit_sha"], body["run_id"]) == (OTHER_SHA, "run-1")
    assert body["eligible_at"] is not None

    async def token(session: AsyncSession) -> int:
        return int(
            await session.scalar(
                select(CodeIngestionJob.claim_token).where(CodeIngestionJob.id == job)
            )
            or 0
        )

    assert workspace.run(token) == 7
    # Not while another is queued or running.
    workspace.worker_writes(job, status="FAILED", eligible_at=None)
    workspace.call("POST", ingestions_of(added["id"]), body={})
    assert workspace.call("POST", retry).status_code == 409


def test_repository_rows_carry_the_canonical_url(workspace: Workspace) -> None:
    added = workspace.added("https://GitHub.com/Acme/Widgets.git/")

    async def stored(session: AsyncSession) -> str:
        found = await session.get(CodeRepository, added["id"])
        assert found is not None
        return found.url

    assert workspace.run(stored) == WIDGETS_URL
