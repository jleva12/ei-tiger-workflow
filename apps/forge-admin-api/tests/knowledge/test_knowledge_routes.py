"""An organization's knowledge bases' routes, without MySQL: the tables on
SQLite (foreign keys enforced, as MySQL does), the organization's access in
memory (the default grants), and fakes for the documents bucket, the async
worker's documents queue and the search (knowledge_fakes.py). Uploads go to
the fake bucket and are submitted to the fake worker, which records what it
was asked and reports each job as told.

The people: an organization admin, a member and a viewer of the
organization, a member of another, and an outsider.
"""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Any
from uuid import uuid4

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_task_documents.retrieval import citation_ref
from httpx import Response
from knowledge_fakes import MODEL, FakeQueue, FakeSearch, FakeStore
from pydantic import SecretStr
from saq.job import Status
from sqlalchemy import MetaData, event, func, select
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
from forge_admin.knowledge.queue import document_job_key
from forge_admin.knowledge.search import Passage, SearchError
from forge_admin.knowledge.storage import StorageError
from forge_admin.models import (
    KnowledgeBase,
    KnowledgeCollection,
    KnowledgeDocument,
    Organization,
    User,
)

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
KBS = f"/organizations/{ORG}/knowledge-bases"
OTHER_KBS = f"/organizations/{OTHER_ORG}/knowledge-bases"
RUNBOOK = b"# Refunds\n\n## Approval limits\n\nA finance lead approves refunds over 500 USD.\n"
MANAGE_REQUIRED = "Requires knowledge_bases:manage"
NO_DOCUMENT = "The knowledge base has no such document"
NO_COLLECTION = "The knowledge base has no such collection"
NO_KNOWLEDGE_BASE = "The organization has no such knowledge base"
NAME_TAKEN = "A collection with this name is already there"
WORKER_DOWN = "The async worker is unavailable; try again"

GRANTS = {
    "org:admin": ("organizations:read", "knowledge_bases:manage"),
    "org:member": ("organizations:read", "knowledge_bases:manage"),
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


def documents_of(knowledge_base_id: str, organization_id: str = ORG) -> str:
    return (
        f"/organizations/{organization_id}/knowledge-bases/{knowledge_base_id}"
        "/documents"
    )


def collections_of(knowledge_base_id: str) -> str:
    return f"{KBS}/{knowledge_base_id}/document-collections"


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    queue: FakeQueue
    store: FakeStore
    search: FakeSearch
    settings: Settings
    tokens: dict[str, str]

    @property
    def state(self) -> Any:
        return self.client.app.state  # type: ignore[attr-defined]

    def call(
        self,
        method: str,
        path: str,
        user: str = MEMBER,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        **options: Any,
    ) -> Response:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"} | (headers or {}),
            json=body,
            **options,
        )

    def knowledge_base(
        self,
        name: str = "Support",
        user: str = MEMBER,
        organization_id: str = ORG,
        description: str = "",
    ) -> str:
        """
        Add a knowledge base straight to the database, as its creation
        route does (test_creating_a_knowledge_base_answers_it_empty), in
        any organization.

        :return: Its ID.
        """

        async def add(session: AsyncSession) -> str:
            made = KnowledgeBase(
                organization_id=organization_id,
                name=name,
                description=description,
                created_by=user,
                updated_by=user,
            )
            session.add(made)
            await session.commit()
            return made.id

        return self.run(add)

    def upload(
        self,
        knowledge_base_id: str,
        name: str = "Refund runbook.md",
        body: bytes = RUNBOOK,
        media_type: str = "text/markdown",
        user: str = MEMBER,
        collection_id: str | None = None,
    ) -> Response:
        return self.call(
            "POST",
            documents_of(knowledge_base_id),
            user,
            files={"file": (name, body, media_type)},
            data={"collection_id": collection_id} if collection_id else None,
        )

    def uploaded(self, knowledge_base_id: str, **options: Any) -> dict[str, Any]:
        answer = self.upload(knowledge_base_id, **options)
        assert answer.status_code == 202, answer.json()
        return dict(answer.json())

    def names(self, knowledge_base_id: str, **params: Any) -> list[str]:
        listed = self.call(
            "GET", documents_of(knowledge_base_id), VIEWER, params=params
        )
        assert listed.status_code == 200, listed.json()
        return [d["filename"] for d in listed.json()]

    def run[T](self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async def main() -> T:
            async with self.sessions() as session:
                return await work(session)

        assert self.client.portal is not None
        return self.client.portal.call(main)

    def count(self, model: type[Base], **where: Any) -> int:
        async def work(session: AsyncSession) -> int:
            query = select(func.count()).select_from(model)
            for name, value in where.items():
                query = query.where(getattr(model, name) == value)
            return int(await session.scalar(query) or 0)

        return self.run(work)


def sqlite_tables() -> MetaData:
    """The tables the routes use, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in (
        "organizations",
        "users",
        "knowledge_bases",
        "knowledge_collections",
        "knowledge_documents",
    ):
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
        # As MySQL does: deleting a knowledge base cascades.
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    queue, store, search = FakeQueue(), FakeStore(), FakeSearch()
    app.state.documents = store
    app.state.knowledge_queue = queue
    app.state.knowledge_search = search
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
                    User(
                        id=MEMBER,
                        first_name="Mia",
                        last_name="Member",
                        email="mia@x.io",
                        msid="mia",
                    ),
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
        yield Workspace(client, sessions, queue, store, search, configured, tokens)
        client.portal.call(engine.dispose)


@pytest.fixture
def kb(workspace: Workspace) -> str:
    """A knowledge base of the organization, empty."""
    return workspace.knowledge_base()


# ------------------------------------------------------- knowledge bases


def with_a_document(workspace: Workspace, name: str = "Support") -> str:
    """:return: A new knowledge base holding one document, queued."""
    made = workspace.knowledge_base(name)
    workspace.uploaded(made, name=f"{name}.md")
    return made


def test_creating_a_knowledge_base_answers_it_empty(workspace: Workspace) -> None:
    made = workspace.call(
        "POST", KBS, MEMBER, {"name": "  Support  ", "description": "Runbooks"}
    )
    assert made.status_code == 201, made.json()
    body = made.json()
    assert {
        k: body[k]
        for k in (
            "organization_id",
            "name",
            "description",
            "documents",
            "ready",
            "failed",
            "chunks",
            "size_bytes",
            "created_by",
        )
    } == {
        "organization_id": ORG,
        "name": "Support",
        "description": "Runbooks",
        "documents": 0,
        "ready": 0,
        "failed": 0,
        "chunks": 0,
        "size_bytes": 0,
        "created_by": MEMBER,
    }
    assert workspace.call("GET", f"{KBS}/{body['id']}", VIEWER).json() == body
    # Another organization may have one by that name.
    elsewhere = workspace.call("POST", OTHER_KBS, NEIGHBOUR, {"name": "Support"})
    assert elsewhere.status_code == 201, elsewhere.json()


def test_an_empty_knowledge_base_is_read_listed_and_changed(
    workspace: Workspace, kb: str
) -> None:
    listed = workspace.call("GET", KBS, VIEWER)
    assert listed.status_code == 200, listed.json()
    assert [(k["id"], k["documents"]) for k in listed.json()] == [(kb, 0)]
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).status_code == 200
    renamed = workspace.call("PATCH", f"{KBS}/{kb}", MEMBER, {"name": "Docs"})
    assert renamed.status_code == 200, renamed.json()


def test_names_are_unique_in_an_organization(workspace: Workspace) -> None:
    workspace.knowledge_base("Support")
    again = workspace.call("POST", KBS, ADMIN, {"name": "Support"})
    assert (again.status_code, again.json()["detail"]) == (
        409,
        "The organization already has a knowledge base by that name",
    )
    assert workspace.call("POST", KBS, MEMBER, {"name": "  "}).status_code == 422
    assert workspace.call("POST", KBS, MEMBER, {"name": "x" * 201}).status_code == 422
    assert workspace.count(KnowledgeBase) == 1


def test_the_list_counts_each_knowledge_bases_documents(workspace: Workspace) -> None:
    support = workspace.knowledge_base("Support")
    hr = workspace.knowledge_base("HR policies")
    done = workspace.uploaded(support, name="done.md")
    broken = workspace.uploaded(support, name="broken.md", body=b"x" * 10)
    workspace.uploaded(support, name="waiting.md", body=b"y" * 5)
    workspace.uploaded(hr, name="leave.md", body=b"z" * 7)
    workspace.queue.finish(
        document_job_key(support, done["id"]),
        result={"status": "ok", "detail": {"chunk_count": 3}},
    )
    workspace.queue.finish(
        document_job_key(support, broken["id"]),
        result={"status": "failed", "error": "BadZipFile"},
    )
    # The counts are of the phases as last read: reading the documents
    # reads the worker's jobs back.
    workspace.names(support)
    # Another organization's aren't listed.
    workspace.knowledge_base("Globex docs", NEIGHBOUR, OTHER_ORG)

    listed = workspace.call("GET", KBS, VIEWER)
    assert listed.status_code == 200, listed.json()
    counts = [
        (kb["name"], kb["documents"], kb["ready"], kb["failed"], kb["chunks"])
        + (kb["size_bytes"],)
        for kb in listed.json()
    ]
    # By name.
    assert counts == [
        ("HR policies", 1, 0, 0, 0, 7),
        ("Support", 3, 1, 1, 3, len(RUNBOOK) + 15),
    ]
    one = workspace.call("GET", f"{KBS}/{support}", VIEWER).json()
    assert {
        k: one[k] for k in ("documents", "ready", "failed", "chunks", "size_bytes")
    } == {
        "documents": 3,
        "ready": 1,
        "failed": 1,
        "chunks": 3,
        "size_bytes": len(RUNBOOK) + 15,
    }
    assert (one["organization_id"], one["name"]) == (ORG, "Support")


def finish_ingest(
    workspace: Workspace, kb: str, document: dict[str, Any], model: str | None
) -> None:
    """Finish a document's ingest as the worker reports it: with the model its
    chunks are embedded with (none, as a worker from before reported)."""
    detail: dict[str, Any] = {"chunk_count": 2}
    if model is not None:
        detail["embedding_model"] = model
    workspace.queue.finish(
        document_job_key(kb, document["id"]), result={"status": "ok", "detail": detail}
    )


def test_documents_embedded_with_another_model_are_stale(workspace: Workspace) -> None:
    kb = workspace.knowledge_base("Claims knowledge")
    current = workspace.uploaded(kb, name="current.md")
    old = workspace.uploaded(kb, name="old.md", body=b"o" * 9)
    unknown = workspace.uploaded(kb, name="unknown.md", body=b"u" * 4)
    workspace.uploaded(kb, name="waiting.md", body=b"w" * 3)
    finish_ingest(workspace, kb, current, MODEL)
    finish_ingest(workspace, kb, old, "text-embedding-3-small@1024")
    finish_ingest(workspace, kb, unknown, None)

    listed = workspace.call("GET", documents_of(kb), VIEWER).json()
    models = {d["filename"]: d["embedding_model"] for d in listed}
    assert models == {
        "current.md": MODEL,
        "old.md": "text-embedding-3-small@1024",
        "unknown.md": "",
        "waiting.md": "",
    }
    one = workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()
    # The other model's, and the one whose model is unknown; not one embedded
    # with the model searches use, nor one still being ingested.
    assert (one["ready"], one["stale"], one["embedding_model"]) == (3, 2, MODEL)
    assert workspace.call("GET", KBS, VIEWER).json()[0]["stale"] == 2
    # Without search there's no model to compare with.
    workspace.state.knowledge_search = None
    unset = workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()
    assert (unset["stale"], unset["embedding_model"]) == (0, None)


def test_reindexing_ingests_the_stale_documents_again(workspace: Workspace) -> None:
    kb = workspace.knowledge_base("Claims knowledge")
    current = workspace.uploaded(kb, name="current.md")
    old = workspace.uploaded(kb, name="old.md", body=b"o" * 9)
    failed = workspace.uploaded(kb, name="failed.md", body=b"f" * 4)
    workspace.uploaded(kb, name="waiting.md", body=b"w" * 3)
    finish_ingest(workspace, kb, current, MODEL)
    finish_ingest(workspace, kb, old, "text-embedding-3-small@1024")
    workspace.queue.finish(
        document_job_key(kb, failed["id"]),
        result={"status": "failed", "error": "BadZipFile"},
    )
    workspace.names(kb)  # the jobs read back
    workspace.queue.documents.clear()
    path = f"{KBS}/{kb}/reindex"

    assert workspace.call("POST", path, VIEWER).status_code == 403
    done = workspace.call("POST", path, MEMBER)
    assert (done.status_code, done.json()) == (202, {"submitted": 1})
    # Ingested again with what the worker has now, even though the file didn't change.
    assert [(d["document_id"], d["force"]) for d in workspace.queue.documents] == [
        (old["id"], True)
    ]
    again = next(
        d
        for d in workspace.call("GET", documents_of(kb), VIEWER).json()
        if d["id"] == old["id"]
    )
    # Its chunks are still searchable, as they were, until new ones replace them.
    assert (again["phase"], again["chunk_count"]) == ("QUEUED", 2)
    finish_ingest(workspace, kb, old, MODEL)
    workspace.names(kb)
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()["stale"] == 0

    # Every finished one (after the parsers or chunking changed), failed ones
    # too, but not one still being ingested.
    workspace.queue.documents.clear()
    every = workspace.call("POST", path, MEMBER, {"stale_only": False})
    assert (every.status_code, every.json()) == (202, {"submitted": 3})
    submitted = {d["document_id"] for d in workspace.queue.documents}
    assert submitted == {current["id"], old["id"], failed["id"]}


def test_reindexing_needs_the_worker_and_for_stale_ones_search(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base("Claims knowledge")
    finish_ingest(workspace, kb, workspace.uploaded(kb), MODEL)
    workspace.names(kb)
    path = f"{KBS}/{kb}/reindex"
    workspace.state.knowledge_search = None
    unset = workspace.call("POST", path, MEMBER)
    assert unset.status_code == 503 and "no document is stale" in unset.json()["detail"]
    workspace.queue.refuse = True
    down = workspace.call("POST", path, MEMBER, {"stale_only": False})
    assert (down.status_code, down.json()["detail"]) == (
        503,
        "The async worker is unavailable after 0 of 1 documents; try again",
    )
    workspace.queue.refuse = False
    up = workspace.call("POST", path, MEMBER, {"stale_only": False})
    assert (up.status_code, up.json()) == (202, {"submitted": 1})
    workspace.state.knowledge_queue = None
    assert (
        workspace.call("POST", path, MEMBER, {"stale_only": False}).status_code == 503
    )


def test_only_the_organizations_people_read_its_knowledge_bases(
    workspace: Workspace,
) -> None:
    kb = with_a_document(workspace)
    assert [k["id"] for k in workspace.call("GET", KBS, VIEWER).json()] == [kb]
    for stranger in (OUTSIDER, NEIGHBOUR):
        denied = workspace.call("GET", KBS, stranger)
        assert (denied.status_code, denied.json()["detail"]) == (
            403,
            "Requires organizations:read",
        )
        assert workspace.call("GET", f"{KBS}/{kb}", stranger).status_code == 403
    # Not found through another organization, nor when there's none.
    other = workspace.call("GET", f"{OTHER_KBS}/{kb}", NEIGHBOUR)
    assert (other.status_code, other.json()["detail"]) == (404, NO_KNOWLEDGE_BASE)
    none = workspace.call("GET", f"{KBS}/{uuid4()}", VIEWER)
    assert (none.status_code, none.json()["detail"]) == (404, NO_KNOWLEDGE_BASE)
    assert workspace.call("GET", f"{KBS}/not-an-id", VIEWER).status_code == 422


def test_only_those_who_manage_knowledge_bases_change_them(
    workspace: Workspace,
) -> None:
    kb = with_a_document(workspace)
    path = f"{KBS}/{kb}"
    for method, target, body in (
        ("POST", KBS, {"name": "Mine"}),
        ("PATCH", path, {"name": "Mine"}),
        ("DELETE", path, None),
    ):
        denied = workspace.call(method, target, VIEWER, body)
        assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)
        assert workspace.call(method, target, OUTSIDER, body).status_code == 403
    assert workspace.call("GET", path, VIEWER).json()["name"] == "Support"
    assert workspace.count(KnowledgeBase) == 1
    assert workspace.queue.deletions == []


def test_a_knowledge_base_is_renamed_and_described(workspace: Workspace) -> None:
    kb = with_a_document(workspace)
    workspace.knowledge_base("HR policies")
    path = f"{KBS}/{kb}"
    renamed = workspace.call("PATCH", path, ADMIN, {"name": "Customer support"})
    assert renamed.status_code == 200, renamed.json()
    assert (renamed.json()["name"], renamed.json()["updated_by"]) == (
        "Customer support",
        ADMIN,
    )
    assert renamed.json()["documents"] == 1
    described = workspace.call("PATCH", path, MEMBER, {"description": "Runbooks"})
    assert (described.json()["name"], described.json()["description"]) == (
        "Customer support",
        "Runbooks",
    )
    taken = workspace.call("PATCH", path, MEMBER, {"name": "HR policies"})
    assert (taken.status_code, taken.json()["detail"]) == (
        409,
        "The organization already has a knowledge base by that name",
    )
    assert workspace.call("GET", path, VIEWER).json()["name"] == "Customer support"
    elsewhere = workspace.call("PATCH", f"{OTHER_KBS}/{kb}", NEIGHBOUR, {"name": "x"})
    assert (elsewhere.status_code, elsewhere.json()["detail"]) == (
        404,
        NO_KNOWLEDGE_BASE,
    )


def test_deleting_a_knowledge_base_removes_everything_in_it(
    workspace: Workspace, kb: str
) -> None:
    runbooks = workspace.call(
        "POST", collections_of(kb), MEMBER, {"name": "Runbooks"}
    ).json()
    workspace.call(
        "POST",
        collections_of(kb),
        MEMBER,
        {"name": "payments", "parent_id": runbooks["id"]},
    )
    first = workspace.uploaded(kb, collection_id=runbooks["id"])
    second = workspace.uploaded(kb, name="notes.txt")
    kept = workspace.knowledge_base("HR policies")
    other = workspace.uploaded(kept, name="leave.md")

    assert workspace.call("DELETE", f"{KBS}/{kb}", MEMBER).status_code == 204
    # Each document's file and chunks, then its records and collections.
    assert list(workspace.store.objects) == [
        f"forge/org-{ORG}/kb-{kept}/documents/{other['id']}/leave.md"
    ]
    assert sorted(d["document_id"] for d in workspace.queue.deletions) == sorted(
        [first["id"], second["id"]]
    )
    assert {d["knowledge_base_id"] for d in workspace.queue.deletions} == {kb}
    gone = workspace.call("GET", f"{KBS}/{kb}", VIEWER)
    assert (gone.status_code, gone.json()["detail"]) == (404, NO_KNOWLEDGE_BASE)
    assert workspace.call("GET", documents_of(kb), VIEWER).status_code == 404
    assert workspace.count(KnowledgeDocument, knowledge_base_id=kb) == 0
    assert workspace.count(KnowledgeCollection, knowledge_base_id=kb) == 0
    assert [k["id"] for k in workspace.call("GET", KBS, VIEWER).json()] == [kept]
    assert workspace.call("DELETE", f"{KBS}/{kb}", MEMBER).status_code == 404


def test_a_knowledge_base_deletion_that_fails_part_way_is_finished_again(
    workspace: Workspace, kb: str
) -> None:
    workspace.uploaded(kb)
    workspace.uploaded(kb, name="notes.txt")
    workspace.queue.refuse = True
    down = workspace.call("DELETE", f"{KBS}/{kb}", MEMBER)
    assert (down.status_code, down.json()["detail"]) == (503, WORKER_DOWN)
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()["documents"] == 2

    workspace.queue.refuse = False
    assert workspace.call("DELETE", f"{KBS}/{kb}", MEMBER).status_code == 204
    assert workspace.store.objects == {}
    assert len(workspace.queue.deletions) == 2
    assert workspace.count(KnowledgeBase) == 0


def test_deleting_a_knowledge_base_with_documents_needs_uploads_set_up(
    workspace: Workspace, kb: str
) -> None:
    workspace.uploaded(kb)
    workspace.state.documents = None
    unset = workspace.call("DELETE", f"{KBS}/{kb}", MEMBER)
    assert unset.status_code == 503
    assert "FORGE_ADMIN_DOCUMENTS_BUCKET" in unset.json()["detail"]
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).status_code == 200

    workspace.state.documents = workspace.store
    workspace.state.knowledge_queue = None
    assert workspace.call("DELETE", f"{KBS}/{kb}", MEMBER).status_code == 503
    workspace.state.knowledge_queue = workspace.queue

    workspace.store.refuse = StorageError("AccessDenied", "no")
    refused = workspace.call("DELETE", f"{KBS}/{kb}", MEMBER)
    assert (refused.status_code, refused.json()["detail"]) == (
        502,
        "Document storage refused the removal",
    )
    workspace.store.refuse = StorageError("unreachable", "connect timeout")
    unreachable = workspace.call("DELETE", f"{KBS}/{kb}", MEMBER)
    assert (unreachable.status_code, unreachable.json()["detail"]) == (
        503,
        "Document storage is unavailable; try again",
    )
    assert workspace.queue.deletions == []
    assert workspace.count(KnowledgeDocument) == 1


def test_an_empty_knowledge_base_is_deleted_without_uploads_set_up(
    workspace: Workspace, kb: str
) -> None:
    workspace.call("POST", collections_of(kb), MEMBER, {"name": "Runbooks"})
    workspace.state.documents = None
    workspace.state.knowledge_queue = None
    assert workspace.call("DELETE", f"{KBS}/{kb}", MEMBER).status_code == 204
    assert workspace.call("GET", KBS, VIEWER).json() == []
    assert workspace.count(KnowledgeBase) == 0
    assert workspace.count(KnowledgeCollection) == 0


# ---------------------------------------------------------------- search


def test_a_search_answers_passages_with_their_documents_names(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    gone = str(uuid4())
    workspace.search.passages = [
        Passage(
            chunk_id="c1",
            document_id=document["id"],
            title="Refunds",
            section_path=["Refunds", "Approval limits"],
            text="A finance lead approves refunds over 500 USD.",
            score=0.92,
        ),
        Passage(chunk_id="c2", document_id=gone, title="Old policy", score=0.5),
    ]
    found = workspace.call(
        "POST",
        f"{KBS}/{kb}/search",
        VIEWER,
        {"query": "  who approves refunds?  ", "limit": 5},
    )
    assert found.status_code == 200, found.json()
    assert found.json() == {
        "hits": [
            {
                "chunk_id": "c1",
                # What an answer cites it by.
                "ref": citation_ref("c1"),
                "document_id": document["id"],
                # Its name as uploaded, from its record.
                "filename": "Refund runbook.md",
                "section_path": ["Refunds", "Approval limits"],
                "location": "",
                "text": "A finance lead approves refunds over 500 USD.",
                "score": 0.92,
            },
            {
                "chunk_id": "c2",
                "ref": citation_ref("c2"),
                "document_id": gone,
                # Its record is gone: its title.
                "filename": "Old policy",
                "section_path": [],
                "location": "",
                "text": "",
                "score": 0.5,
            },
        ]
    }
    assert workspace.search.calls == [
        {
            "knowledge_base_ids": [kb],
            "query": "who approves refunds?",
            "limit": 5,
            "document_ids": None,
        }
    ]
    # Eight by default.
    workspace.call("POST", f"{KBS}/{kb}/search", MEMBER, {"query": "refunds"})
    assert workspace.search.calls[-1]["limit"] == 8


def test_a_search_finding_nothing_answers_no_hits(
    workspace: Workspace, kb: str
) -> None:
    found = workspace.call("POST", f"{KBS}/{kb}/search", VIEWER, {"query": "x"})
    assert (found.status_code, found.json()) == (200, {"hits": []})


def test_a_search_asks_for_the_knowledge_base_its_query_and_limit(
    workspace: Workspace, kb: str
) -> None:
    # What it answers is checked above; this is what it asks, whatever that.
    path = f"{KBS}/{kb}/search"
    workspace.call("POST", path, VIEWER, {"query": "  who approves?  ", "limit": 5})
    workspace.call("POST", path, MEMBER, {"query": "refunds"})
    assert workspace.search.calls == [
        {
            "knowledge_base_ids": [kb],
            "query": "who approves?",
            "limit": 5,
            "document_ids": None,
        },
        # Eight by default.
        {
            "knowledge_base_ids": [kb],
            "query": "refunds",
            "limit": 8,
            "document_ids": None,
        },
    ]


def test_searching_is_checked_like_reading(workspace: Workspace, kb: str) -> None:
    path = f"{KBS}/{kb}/search"
    assert workspace.call("POST", path, OUTSIDER, {"query": "x"}).status_code == 403
    assert workspace.call("POST", path, NEIGHBOUR, {"query": "x"}).status_code == 403
    elsewhere = workspace.call(
        "POST", f"{OTHER_KBS}/{kb}/search", NEIGHBOUR, {"query": "x"}
    )
    assert (elsewhere.status_code, elsewhere.json()["detail"]) == (
        404,
        NO_KNOWLEDGE_BASE,
    )
    for body in (
        {"query": ""},
        {"query": "   "},
        {},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 21},
        {"query": "x" * 2001},
    ):
        assert workspace.call("POST", path, VIEWER, body).status_code == 422, body
    assert workspace.search.calls == []


def test_a_search_needs_search_set_up_and_available(
    workspace: Workspace, kb: str
) -> None:
    path = f"{KBS}/{kb}/search"
    workspace.search.error = SearchError("connection refused")
    down = workspace.call("POST", path, VIEWER, {"query": "refunds"})
    assert (down.status_code, down.json()["detail"]) == (
        503,
        "Knowledge base search is unavailable; try again",
    )
    workspace.state.knowledge_search = None
    unset = workspace.call("POST", path, VIEWER, {"query": "refunds"})
    assert (unset.status_code, unset.json()["detail"]) == (
        503,
        "Knowledge base search isn't set up: set FORGE_ADMIN_MONGO_URI",
    )


def test_an_agent_searches_only_its_knowledge_bases_ranked_together(
    workspace: Workspace,
) -> None:
    """A chat agent given "Member knowledge" and "Claims knowledge" searches
    those two, in one ranking, and none of the organization's others; on
    the documents task's search over what the worker would have embedded."""
    from forge_embeddings.embedding import HashingEmbedder
    from forge_embeddings.tokenizers import HeuristicTokenizer
    from forge_task_documents.chunking import ChunkEngine
    from forge_task_documents.enrichment import BreadcrumbEnricher, CompositeEnricher
    from forge_task_documents.ingestion import IngestionPipeline
    from forge_task_documents.models import SourceFile
    from forge_task_documents.parsers import default_registry
    from forge_task_documents.retrieval import KnowledgeBaseSearch
    from forge_task_documents.storage.memory import InMemoryStorage

    from forge_admin.knowledge.search import KnowledgeSearch
    from forge_admin.knowledge.tools import OrganizationKnowledgeBases

    members = workspace.knowledge_base("Member knowledge")
    claims = workspace.knowledge_base("Claims knowledge")
    vendors = workspace.knowledge_base("Vendor contracts")
    files = {
        members: {
            "Member benefits.md": b"# Benefits\n\n## Gym\nMembers save 20 percent "
            b"at partner gyms. Bring your member card to claim it.\n",
        },
        claims: {
            "Appeals guide.md": b"# Appeals\n\n## Denied claims\nTo appeal a denied "
            b"claim, file the appeal within 180 days of the denial.\n",
            "Filing claims.md": b"# Filing\n\n## Filing a claim\nFile a claim within "
            b"90 days. If the claim is denied, appeal the denied claim.\n",
        },
        # It would match too, but the agent wasn't given it.
        vendors: {
            "Vendor appeals.md": b"# Vendors\n\n## Appeals\nA vendor can appeal a "
            b"denied claim for payment.\n",
        },
    }
    storage = InMemoryStorage()
    embedder = HashingEmbedder(dimensions=64)
    pipeline = IngestionPipeline(
        registry=default_registry(load_plugins=False),
        chunker=ChunkEngine(HeuristicTokenizer()),
        enricher=CompositeEnricher([BreadcrumbEnricher()]),
        embedder=embedder,
        storage=storage,
    )
    for knowledge_base_id, documents in files.items():
        for name, body in documents.items():
            row = workspace.uploaded(knowledge_base_id, name=name, body=body)
            source = SourceFile(
                tenant_id=knowledge_base_id, doc_id=row["id"], filename=name, data=body
            )
            workspace.client.portal.call(pipeline.ingest, source)  # type: ignore[union-attr]
    tools = OrganizationKnowledgeBases(
        workspace.sessions,
        KnowledgeSearch(KnowledgeBaseSearch(storage, embedder=embedder)),
    )

    def passages(*knowledge_base_ids: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = workspace.client.portal.call(  # type: ignore[union-attr]
            partial(
                tools.search,
                list(knowledge_base_ids),
                "appeal a denied claim",
                organization_id=ORG,
                limit=5,
            )
        )
        return found

    def search(*knowledge_base_ids: str) -> list[str]:
        return [passage["document"] for passage in passages(*knowledge_base_ids)]

    assert set(search(members)) == {"Member benefits.md"}
    assert set(search(claims)) == {"Appeals guide.md", "Filing claims.md"}
    both = search(members, claims)
    # Both claims documents answer it, so they come first; the vendors'
    # isn't searched at all.
    assert set(both[:2]) == {"Appeals guide.md", "Filing claims.md"}
    assert set(both) <= {"Member benefits.md", "Appeals guide.md", "Filing claims.md"}
    # Each passage names the knowledge base it's from, so the agent can tell
    # the members' from the claims'.
    sources = {(p["knowledge_base"], p["document"]) for p in passages(members, claims)}
    assert ("Claims knowledge", "Appeals guide.md") in sources
    assert {kb for kb, _ in sources} <= {"Member knowledge", "Claims knowledge"}
    first = passages(claims)[0]
    assert list(first) == [
        "ref",
        "knowledge_base",
        "knowledge_base_id",
        "document",
        "document_id",
        "section",
        "location",
        "text",
        "score",
    ]
    # What a chat UI cites and links it by.
    assert (first["knowledge_base_id"], first["location"][:4]) == (claims, "line")
    # Another organization's knowledge base isn't found.
    theirs = workspace.knowledge_base("Theirs", organization_id=OTHER_ORG)
    with pytest.raises(LookupError):
        search(claims, theirs)


# ------------------------------------------------------------- documents


def test_a_member_uploads_and_the_worker_ingests_it_from_storage(
    workspace: Workspace, kb: str
) -> None:
    uploaded = workspace.upload(kb)
    assert uploaded.status_code == 202, uploaded.json()
    document = uploaded.json()
    assert {
        k: document[k]
        for k in (
            "knowledge_base_id",
            "collection_id",
            "filename",
            "media_type",
            "size_bytes",
            "sha256",
            "phase",
            "error",
            "chunk_count",
            "finished_at",
            "created_by",
        )
    } == {
        "knowledge_base_id": kb,
        "collection_id": None,
        "filename": "Refund runbook.md",
        "media_type": "text/markdown",
        "size_bytes": len(RUNBOOK),
        "sha256": hashlib.sha256(RUNBOOK).hexdigest(),
        "phase": "QUEUED",
        "error": "",
        "chunk_count": 0,
        "finished_at": None,
        "created_by": MEMBER,
    }

    # Stored under the knowledge base's place in the hierarchy, so each
    # level is a prefix; then handed to the worker with where it is.
    key = f"forge/org-{ORG}/kb-{kb}/documents/{document['id']}/Refund-runbook.md"
    assert workspace.store.objects == {key: (RUNBOOK, "text/markdown")}
    assert workspace.queue.documents == [
        {
            "knowledge_base_id": kb,
            "document_id": document["id"],
            "uri": f"s3://forge-documents/{key}",
            "filename": "Refund runbook.md",
            "media_type": "text/markdown",
            "uploaded_by": MEMBER,
        }
    ]

    # The organization follows its job: queued, then what the worker reports.
    listed = workspace.call("GET", documents_of(kb), VIEWER)
    assert [(d["id"], d["phase"]) for d in listed.json()] == [
        (document["id"], "QUEUED")
    ]
    one = f"{documents_of(kb)}/{document['id']}"
    workspace.queue.finish(
        document_job_key(kb, document["id"]), status=Status.ACTIVE, result=None
    )
    assert workspace.call("GET", one, VIEWER).json()["phase"] == "RUNNING"
    workspace.queue.finish(
        document_job_key(kb, document["id"]),
        result={"status": "ok", "detail": {"status": "ready", "chunk_count": 3}},
    )
    done = workspace.call("GET", one, VIEWER).json()
    assert (done["phase"], done["chunk_count"], done["error"]) == ("SUCCEEDED", 3, "")
    assert done["finished_at"] is not None

    # A finished one isn't read back again.
    reads = len(workspace.queue.reads)
    workspace.queue.refuse = True
    assert workspace.call("GET", one, VIEWER).json()["phase"] == "SUCCEEDED"
    assert len(workspace.call("GET", documents_of(kb), VIEWER).json()) == 1
    assert len(workspace.queue.reads) == reads


def test_the_organization_sees_why_a_document_failed(
    workspace: Workspace, kb: str
) -> None:
    ids = [workspace.uploaded(kb, name=f"doc-{n}.md")["id"] for n in range(3)]
    keys = [document_job_key(kb, i) for i in ids]
    workspace.queue.finish(
        keys[0],
        result={"status": "failed", "error": "UnsupportedFormatError: no parser"},
    )
    workspace.queue.finish(
        keys[1],
        status=Status.FAILED,
        error="Traceback (most recent call last):\n  ...\nKeyError: 'unexpected'\n",
    )
    workspace.queue.forget(keys[2])

    listed = {d["id"]: d for d in workspace.call("GET", documents_of(kb)).json()}
    assert [(listed[i]["phase"], listed[i]["error"]) for i in ids] == [
        ("FAILED", "UnsupportedFormatError: no parser"),
        ("FAILED", "KeyError: 'unexpected'"),
        ("MISSING", ""),
    ]
    assert all(listed[i]["finished_at"] is not None for i in ids)
    # Final phases stay as they were, without asking the worker again.
    workspace.queue.refuse = True
    one = workspace.call("GET", f"{documents_of(kb)}/{ids[0]}", VIEWER)
    assert one.json()["phase"] == "FAILED"


def test_a_worker_that_doesnt_answer_leaves_the_phase_as_it_was(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    workspace.queue.refuse = True
    one = workspace.call("GET", f"{documents_of(kb)}/{document['id']}", VIEWER)
    assert (one.status_code, one.json()["phase"]) == (200, "QUEUED")
    assert workspace.call("GET", documents_of(kb), VIEWER).status_code == 200


def test_a_member_retries_a_document_whose_ingestion_failed(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    one = f"{documents_of(kb)}/{document['id']}"
    key = document_job_key(kb, document["id"])
    # Not while its job is queued.
    queued = workspace.call("POST", f"{one}/retry", MEMBER)
    assert (queued.status_code, queued.json()["detail"]) == (
        409,
        "Only a document whose ingestion failed can be retried",
    )

    workspace.queue.finish(
        key,
        status=Status.FAILED,
        error="Traceback (most recent call last):\n  ...\nTimeoutError: too slow\n",
    )
    assert workspace.call("GET", one, VIEWER).json()["phase"] == "FAILED"
    denied = workspace.call("POST", f"{one}/retry", VIEWER)
    assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)

    # A worker that can't take it leaves it failed.
    workspace.queue.refuse = True
    down = workspace.call("POST", f"{one}/retry", MEMBER)
    assert (down.status_code, down.json()["detail"]) == (503, WORKER_DOWN)
    workspace.queue.refuse = False
    assert (
        workspace.call("GET", one, VIEWER).json()["error"] == "TimeoutError: too slow"
    )

    # Retried by someone else, it's still submitted as its uploader's.
    retried = workspace.call("POST", f"{one}/retry", ADMIN)
    assert retried.status_code == 202, retried.json()
    assert {
        k: retried.json()[k] for k in ("phase", "error", "chunk_count", "finished_at")
    } == {"phase": "QUEUED", "error": "", "chunk_count": 0, "finished_at": None}
    # The stored file again, under its job's key, as its uploader sent it.
    first, again = workspace.queue.documents
    assert again == first
    workspace.queue.finish(key, result={"status": "ok", "detail": {"chunk_count": 2}})
    done = workspace.call("GET", one, VIEWER).json()
    assert (done["phase"], done["chunk_count"]) == ("SUCCEEDED", 2)
    assert workspace.call("POST", f"{one}/retry", MEMBER).status_code == 409

    # One the worker no longer has can be retried too.
    lost = workspace.uploaded(kb, name="Lost.md")
    workspace.queue.forget(document_job_key(kb, lost["id"]))
    lost_retry = workspace.call("POST", f"{documents_of(kb)}/{lost['id']}/retry")
    assert lost_retry.status_code == 202, lost_retry.json()

    # And none without the worker set up.
    workspace.queue.forget(key)
    workspace.state.knowledge_queue = None
    assert workspace.call("POST", f"{one}/retry", MEMBER).status_code == 503


def test_only_those_who_may_upload_can(workspace: Workspace, kb: str) -> None:
    denied = workspace.upload(kb, user=VIEWER)
    assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)
    assert workspace.store.objects == {} and workspace.queue.documents == []

    document = workspace.uploaded(kb)
    one = f"{documents_of(kb)}/{document['id']}"
    for stranger in (OUTSIDER, NEIGHBOUR):
        assert workspace.call("GET", documents_of(kb), stranger).status_code == 403
        assert workspace.call("GET", one, stranger).status_code == 403
        assert workspace.upload(kb, user=stranger).status_code == 403
    missing = workspace.call("GET", f"{documents_of(kb)}/{uuid4()}", VIEWER)
    assert (missing.status_code, missing.json()["detail"]) == (404, NO_DOCUMENT)

    # Not through another of the organization's knowledge bases.
    other = workspace.knowledge_base("HR policies")
    through = workspace.call("GET", f"{documents_of(other)}/{document['id']}", VIEWER)
    assert (through.status_code, through.json()["detail"]) == (404, NO_DOCUMENT)
    # Nor another organization's knowledge base through this one.
    theirs = workspace.knowledge_base("Globex", NEIGHBOUR, OTHER_ORG)
    for user in (MEMBER, VIEWER):
        refused = workspace.call("GET", documents_of(theirs), user)
        assert (refused.status_code, refused.json()["detail"]) == (
            404,
            NO_KNOWLEDGE_BASE,
        )
    assert workspace.upload(theirs).status_code == 404
    # Nor this organization's through the other's path.
    assert (
        workspace.call("GET", documents_of(kb, OTHER_ORG), NEIGHBOUR).status_code == 404
    )
    assert len(workspace.queue.documents) == 1


def test_uploads_are_checked_before_anything_is_stored(
    workspace: Workspace, kb: str
) -> None:
    empty = workspace.upload(kb, body=b"")
    assert (empty.status_code, empty.json()["detail"]) == (422, "The file is empty")
    workspace.state.settings = workspace.settings.model_copy(
        update={"documents_max_bytes": 16}
    )
    too_large = workspace.upload(kb)
    assert (too_large.status_code, too_large.json()["detail"]) == (
        413,
        "Documents can be up to 16 bytes",
    )
    assert workspace.store.objects == {} and workspace.queue.documents == []
    workspace.state.settings = workspace.settings

    # A path from the browser keeps only the file's name.
    named = workspace.upload(kb, name="C:\\fakepath\\notes.txt", media_type="")
    assert named.status_code == 202, named.json()
    assert (named.json()["filename"], named.json()["media_type"]) == ("notes.txt", "")
    assert len(workspace.store.objects) == 1 and len(workspace.queue.documents) == 1
    (key,) = workspace.store.objects
    assert key.endswith(f"/{named.json()['id']}/notes.txt")


def test_an_unavailable_worker_or_bucket_leaves_nothing_behind(
    workspace: Workspace, kb: str
) -> None:
    workspace.queue.refuse = True
    down = workspace.upload(kb)
    assert (down.status_code, down.json()["detail"]) == (503, WORKER_DOWN)
    # The upload is undone: no row, no object.
    workspace.queue.refuse = False
    assert workspace.call("GET", documents_of(kb), VIEWER).json() == []
    assert workspace.store.objects == {}
    assert workspace.count(KnowledgeDocument) == 0

    workspace.store.refuse = StorageError("unreachable", "connect timeout")
    unreachable = workspace.upload(kb)
    assert (unreachable.status_code, unreachable.json()["detail"]) == (
        503,
        "Document storage is unavailable; try again",
    )
    workspace.store.refuse = StorageError("NoSuchBucket", "The bucket does not exist")
    refused = workspace.upload(kb)
    assert (refused.status_code, refused.json()["detail"]) == (
        502,
        "Document storage refused the upload",
    )
    assert workspace.queue.documents == []
    assert workspace.count(KnowledgeDocument) == 0


def test_uploads_need_the_bucket_and_the_worker(workspace: Workspace, kb: str) -> None:
    workspace.state.documents = None
    answer = workspace.upload(kb)
    assert answer.status_code == 503
    assert "FORGE_ADMIN_DOCUMENTS_BUCKET" in answer.json()["detail"]
    # Reading still works, from what the admin recorded.
    assert workspace.call("GET", documents_of(kb), VIEWER).status_code == 200

    workspace.state.documents = workspace.store
    document = workspace.uploaded(kb)
    workspace.state.knowledge_queue = None
    answer = workspace.upload(kb)
    assert answer.status_code == 503
    assert "FORGE_ADMIN_EMBEDDING_REDIS_URL" in answer.json()["detail"]
    listed = workspace.call("GET", documents_of(kb), VIEWER)
    assert [(d["id"], d["phase"]) for d in listed.json()] == [
        (document["id"], "QUEUED")
    ]
    summary = workspace.call("GET", f"{documents_of(kb)}/summary", VIEWER)
    assert summary.status_code == 200
    assert len(workspace.store.objects) == 1


def test_a_member_files_documents_in_collections(workspace: Workspace, kb: str) -> None:
    collections = collections_of(kb)
    created = workspace.call("POST", collections, MEMBER, {"name": "Runbooks"})
    assert created.status_code == 201, created.json()
    runbooks = created.json()
    assert (runbooks["document_count"], runbooks["size_bytes"]) == (0, 0)
    assert (runbooks["knowledge_base_id"], runbooks["parent_id"]) == (kb, None)
    taken = workspace.call("POST", collections, MEMBER, {"name": "Runbooks"})
    assert (taken.status_code, taken.json()["detail"]) == (409, NAME_TAKEN)
    # Viewers read them; only those who manage knowledge bases change them.
    denied = workspace.call("POST", collections, VIEWER, {"name": "Mine"})
    assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)

    filed = workspace.uploaded(kb, name="Refunds.md", collection_id=runbooks["id"])
    loose = workspace.uploaded(kb, name="Notes.txt")
    assert (filed["collection_id"], loose["collection_id"]) == (runbooks["id"], None)
    elsewhere = workspace.upload(kb, name="x.md", collection_id=str(uuid4()))
    assert (elsewhere.status_code, elsewhere.json()["detail"]) == (422, NO_COLLECTION)
    # Another knowledge base's collection isn't this one's.
    other = workspace.knowledge_base("HR policies")
    theirs = workspace.call(
        "POST", collections_of(other), MEMBER, {"name": "Leave"}
    ).json()
    across = workspace.upload(kb, name="x.md", collection_id=theirs["id"])
    assert (across.status_code, across.json()["detail"]) == (422, NO_COLLECTION)

    listed = workspace.call("GET", collections, VIEWER).json()
    assert [(c["name"], c["document_count"], c["size_bytes"]) for c in listed] == [
        ("Runbooks", 1, len(RUNBOOK))
    ]
    assert workspace.names(kb, collection=runbooks["id"]) == ["Refunds.md"]
    assert workspace.names(kb, collection="none") == ["Notes.txt"]

    # Moving files it; null unfiles it.
    one = f"{documents_of(kb)}/{loose['id']}"
    moved = workspace.call("PATCH", one, MEMBER, {"collection_id": runbooks["id"]})
    assert moved.json()["collection_id"] == runbooks["id"]
    assert (
        workspace.call("PATCH", one, VIEWER, {"collection_id": None}).status_code == 403
    )
    wrong = workspace.call("PATCH", one, MEMBER, {"collection_id": theirs["id"]})
    assert (wrong.status_code, wrong.json()["detail"]) == (422, NO_COLLECTION)
    # A body without it leaves it where it is.
    assert (
        workspace.call("PATCH", one, MEMBER, {}).json()["collection_id"]
        == runbooks["id"]
    )
    renamed = workspace.call(
        "PATCH",
        f"{collections}/{runbooks['id']}",
        MEMBER,
        {"name": "Support runbooks"},
    ).json()
    assert (renamed["name"], renamed["document_count"]) == ("Support runbooks", 2)
    assert (
        workspace.call(
            "PATCH", f"{collections}/{runbooks['id']}", VIEWER, {"name": "x"}
        ).status_code
        == 403
    )

    # Deleting the collection leaves its documents in the knowledge base, unfiled.
    assert (
        workspace.call("DELETE", f"{collections}/{runbooks['id']}", VIEWER).status_code
        == 403
    )
    assert (
        workspace.call("DELETE", f"{collections}/{runbooks['id']}", MEMBER).status_code
        == 204
    )
    assert workspace.call("GET", collections, VIEWER).json() == []
    left = workspace.call("GET", documents_of(kb), VIEWER).json()
    assert sorted((d["filename"], d["collection_id"]) for d in left) == [
        ("Notes.txt", None),
        ("Refunds.md", None),
    ]
    gone = workspace.call("DELETE", f"{collections}/{runbooks['id']}", MEMBER)
    assert (gone.status_code, gone.json()["detail"]) == (404, NO_COLLECTION)
    # Nor is another knowledge base's collection found through this one.
    assert (
        workspace.call("DELETE", f"{collections}/{theirs['id']}", MEMBER).status_code
        == 404
    )


def test_the_list_filters_and_pages(workspace: Workspace, kb: str) -> None:
    names = ["Refund policy.docx", "Refund 100%.md", "Pricing.xlsx", "notes.txt"]
    ids = {name: workspace.uploaded(kb, name=name)["id"] for name in names}
    workspace.queue.finish(
        document_job_key(kb, ids["Pricing.xlsx"]),
        result={"status": "failed", "error": "BadZipFile"},
    )
    # Not read back yet: filtering by phase reads the worker first.
    assert workspace.names(kb, phase=["FAILED"]) == ["Pricing.xlsx"]

    # Most recent first.
    assert workspace.names(kb) == list(reversed(names))
    assert workspace.names(kb, q="refund") == ["Refund 100%.md", "Refund policy.docx"]
    # LIKE's wildcards are only themselves.
    assert workspace.names(kb, q="100%") == ["Refund 100%.md"]
    assert workspace.names(kb, q="_") == []
    assert workspace.names(kb, q="   ") == workspace.names(kb)
    assert len(workspace.names(kb, phase=["QUEUED", "RUNNING"])) == 3
    assert (
        workspace.names(kb, limit=2) + workspace.names(kb, limit=2, offset=2)
        == workspace.names(kb)[:4]
    )
    assert workspace.names(kb, offset=4) == []
    assert workspace.names(kb, extension=["DOCX", "xlsx"]) == [
        "Pricing.xlsx",
        "Refund policy.docx",
    ]
    assert workspace.names(kb, not_extension=["docx", "xlsx", "md"]) == ["notes.txt"]
    assert workspace.names(kb, uploaded_by=MEMBER) == workspace.names(kb)
    assert workspace.names(kb, uploaded_by="someone-else") == []
    path = documents_of(kb)
    for params in (
        {"extension": "."},
        {"phase": "LOST"},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
    ):
        answer = workspace.call("GET", path, VIEWER, params=params)
        assert answer.status_code == 422, params


def test_the_summary_adds_up_the_knowledge_bases_documents(
    workspace: Workspace, kb: str
) -> None:
    first = workspace.uploaded(kb, name="Refunds.md")
    workspace.uploaded(kb, name="Pricing.XLSX", body=b"x" * 10)
    workspace.uploaded(kb, name="README")
    workspace.queue.finish(
        document_job_key(kb, first["id"]),
        result={"status": "ok", "detail": {"status": "ready", "chunk_count": 4}},
    )

    summary = workspace.call("GET", f"{documents_of(kb)}/summary", VIEWER)
    assert summary.status_code == 200, summary.json()
    body = summary.json()
    total = 2 * len(RUNBOOK) + 10
    md, other = len(RUNBOOK), len(RUNBOOK)

    def sums(documents: int, size: int, chunks: int = 0, ready: int = 0) -> Any:
        return {
            "documents": documents,
            "size_bytes": size,
            "chunks": chunks,
            "ready": ready,
            "failed": 0,
        }

    assert body["totals"] == sums(3, total, 4, 1)
    assert body["by_phase"] == {
        "SUCCEEDED": sums(1, md, 4, 1),
        "QUEUED": sums(2, other + 10),
    }
    assert body["by_extension"] == {
        "md": sums(1, md, 4, 1),
        "xlsx": sums(1, 10),
        "": sums(1, other),
    }
    (who,) = body["contributors"]
    assert (who["user"], who["documents"], who["size_bytes"]) == (MEMBER, 3, total)
    assert (
        workspace.call("GET", f"{documents_of(kb)}/summary", OUTSIDER).status_code
        == 403
    )


def test_the_summary_ranks_contributors_by_documents(
    workspace: Workspace, kb: str
) -> None:
    workspace.uploaded(kb, name="a.md", user=ADMIN)
    workspace.uploaded(kb, name="b.md")
    workspace.uploaded(kb, name="c.md")
    empty = workspace.knowledge_base("HR policies")

    body = workspace.call("GET", f"{documents_of(kb)}/summary", VIEWER).json()
    assert [(c["user"], c["documents"]) for c in body["contributors"]] == [
        (MEMBER, 2),
        (ADMIN, 1),
    ]
    nothing = workspace.call("GET", f"{documents_of(empty)}/summary", VIEWER).json()
    assert nothing == {
        "totals": {
            "documents": 0,
            "size_bytes": 0,
            "chunks": 0,
            "ready": 0,
            "failed": 0,
        },
        "by_phase": {},
        "by_extension": {},
        "contributors": [],
    }


def test_removing_a_document_clears_the_bucket_and_the_worker(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    one = f"{documents_of(kb)}/{document['id']}"
    denied = workspace.call("DELETE", one, VIEWER)
    assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)

    # A removal that fails part way leaves the record, to be removed again.
    workspace.queue.refuse = True
    down = workspace.call("DELETE", one, MEMBER)
    assert (down.status_code, down.json()["detail"]) == (503, WORKER_DOWN)
    workspace.queue.refuse = False
    assert workspace.call("GET", one, VIEWER).status_code == 200

    assert workspace.call("DELETE", one, MEMBER).status_code == 204
    assert workspace.store.objects == {}
    assert workspace.queue.deletions == [
        {"knowledge_base_id": kb, "document_id": document["id"]}
    ]
    assert workspace.call("GET", one, VIEWER).status_code == 404
    assert workspace.call("GET", documents_of(kb), VIEWER).json() == []
    again = workspace.call("DELETE", one, MEMBER)
    assert (again.status_code, again.json()["detail"]) == (404, NO_DOCUMENT)


def test_removing_needs_the_bucket_and_the_worker(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    one = f"{documents_of(kb)}/{document['id']}"
    workspace.state.documents = None
    unset = workspace.call("DELETE", one, MEMBER)
    assert unset.status_code == 503
    assert "FORGE_ADMIN_DOCUMENTS_BUCKET" in unset.json()["detail"]
    workspace.state.documents = workspace.store

    workspace.store.refuse = StorageError("AccessDenied", "no")
    refused = workspace.call("DELETE", one, MEMBER)
    assert (refused.status_code, refused.json()["detail"]) == (
        502,
        "Document storage refused the removal",
    )
    workspace.store.refuse = StorageError("unreachable", "connect timeout")
    unreachable = workspace.call("DELETE", one, MEMBER)
    assert (unreachable.status_code, unreachable.json()["detail"]) == (
        503,
        "Document storage is unavailable; try again",
    )
    assert workspace.queue.deletions == []
    assert workspace.call("GET", one, VIEWER).status_code == 200


# --------------------------------------------------------------- content


def test_the_content_is_streamed_back_as_uploaded(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    content = f"{documents_of(kb)}/{document['id']}/content"
    read = workspace.call("GET", content, VIEWER)
    assert read.status_code == 200
    assert read.content == RUNBOOK
    etag = f'"{hashlib.sha256(RUNBOOK).hexdigest()}"'
    assert read.headers["ETag"] == etag
    assert read.headers["Cache-Control"] == "private, no-cache"
    assert read.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert read.headers["X-Content-Type-Options"] == "nosniff"
    assert read.headers["Content-Length"] == str(len(RUNBOOK))
    assert read.headers["Content-Type"].split(";")[0] == "text/markdown"
    assert read.headers["Content-Disposition"] == (
        "inline; filename=\"Refund runbook.md\"; filename*=UTF-8''Refund%20runbook.md"
    )
    (stored,) = workspace.store.opened
    assert stored.closed

    saved = workspace.call("GET", content, VIEWER, params={"download": "true"})
    assert saved.headers["Content-Disposition"].startswith("attachment; ")
    assert saved.content == RUNBOOK


def test_a_current_copy_isnt_sent_again(workspace: Workspace, kb: str) -> None:
    document = workspace.uploaded(kb)
    content = f"{documents_of(kb)}/{document['id']}/content"
    etag = f'"{document["sha256"]}"'
    for held in (etag, f'"stale", {etag}'):
        current = workspace.call(
            "GET", content, VIEWER, headers={"If-None-Match": held}
        )
        assert current.status_code == 304
        assert current.content == b""
        assert current.headers["ETag"] == etag
    # Answered without reading the bucket.
    assert workspace.store.opened == []
    stale = workspace.call("GET", content, VIEWER, headers={"If-None-Match": '"old"'})
    assert (stale.status_code, stale.content) == (200, RUNBOOK)


def test_files_a_browser_would_run_are_sent_as_bytes(
    workspace: Workspace, kb: str
) -> None:
    page = workspace.uploaded(
        kb, name="page.html", body=b"<script>alert(1)</script>", media_type="text/html"
    )
    read = workspace.call("GET", f"{documents_of(kb)}/{page['id']}/content", VIEWER)
    assert read.headers["Content-Type"] == "application/octet-stream"

    named = workspace.uploaded(kb, name="Résumé final.pdf", media_type="")
    read = workspace.call("GET", f"{documents_of(kb)}/{named['id']}/content", VIEWER)
    assert read.headers["Content-Type"] == "application/pdf"
    assert read.headers["Content-Disposition"] == (
        'inline; filename="R_sum_ final.pdf"; '
        "filename*=UTF-8''R%C3%A9sum%C3%A9%20final.pdf"
    )


def test_reading_the_content_is_checked_and_needs_the_bucket(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)
    content = f"{documents_of(kb)}/{document['id']}/content"
    assert workspace.call("GET", content, OUTSIDER).status_code == 403
    assert workspace.call("GET", content, NEIGHBOUR).status_code == 403
    missing = workspace.call("GET", f"{documents_of(kb)}/{uuid4()}/content", VIEWER)
    assert (missing.status_code, missing.json()["detail"]) == (404, NO_DOCUMENT)

    workspace.store.refuse = StorageError("unreachable", "connect timeout")
    unreachable = workspace.call("GET", content, VIEWER)
    assert (unreachable.status_code, unreachable.json()["detail"]) == (
        503,
        "Document storage is unavailable; try again",
    )
    workspace.store.refuse = StorageError("AccessDenied", "no")
    refused = workspace.call("GET", content, VIEWER)
    assert (refused.status_code, refused.json()["detail"]) == (
        502,
        "Document storage refused the read",
    )
    workspace.store.refuse = None

    workspace.store.objects.clear()
    gone = workspace.call("GET", content, VIEWER)
    assert (gone.status_code, gone.json()["detail"]) == (
        404,
        "The document's file is gone",
    )
    workspace.state.documents = None
    unset = workspace.call("GET", content, VIEWER)
    assert (unset.status_code, unset.json()["detail"]) == (
        503,
        "Document storage isn't set up: set FORGE_ADMIN_DOCUMENTS_BUCKET",
    )


def test_a_file_stored_outside_the_bucket_is_gone(
    workspace: Workspace, kb: str
) -> None:
    document = workspace.uploaded(kb)

    async def move(session: AsyncSession) -> None:
        row = await session.get(KnowledgeDocument, document["id"])
        assert row is not None
        row.storage_uri = "s3://another-bucket/x.md"
        await session.commit()

    workspace.run(move)
    gone = workspace.call("GET", f"{documents_of(kb)}/{document['id']}/content", VIEWER)
    assert (gone.status_code, gone.json()["detail"]) == (
        404,
        "The document's file is gone",
    )
    # Removing it still removes its record and chunks.
    assert (
        workspace.call(
            "DELETE", f"{documents_of(kb)}/{document['id']}", MEMBER
        ).status_code
        == 204
    )
    assert len(workspace.queue.deletions) == 1


# ----------------------------------------------------------- collections


def test_collections_nest_like_the_folders_uploaded(
    workspace: Workspace, kb: str
) -> None:
    collections = collections_of(kb)
    paths = f"{collections}/paths"

    # A folder's tree, recreated: every folder that holds files, and its parents.
    made = workspace.call(
        "POST",
        paths,
        MEMBER,
        {
            "paths": [
                ["Runbooks"],
                ["Runbooks", "payments"],
                ["Runbooks", "payments", "EU"],
            ]
        },
    )
    assert made.status_code == 200, made.json()
    assert [c["path"] for c in made.json()["collections"]] == [
        ["Runbooks"],
        ["Runbooks", "payments"],
        ["Runbooks", "payments", "EU"],
    ]
    runbooks, payments, eu = (c["collection"] for c in made.json()["collections"])
    assert (runbooks["parent_id"], payments["parent_id"], eu["parent_id"]) == (
        None,
        runbooks["id"],
        payments["id"],
    )
    # Uploading it again reuses them, whatever the case.
    again = workspace.call(
        "POST",
        paths,
        MEMBER,
        {"paths": [["runbooks", "PAYMENTS"], ["Runbooks", "cards"]]},
    )
    reused, cards = (c["collection"] for c in again.json()["collections"])
    assert reused["id"] == payments["id"]
    assert cards["parent_id"] == runbooks["id"]
    # Into a collection: under it.
    inside = workspace.call(
        "POST", paths, MEMBER, {"parent_id": eu["id"], "paths": [["2026"]]}
    )
    assert inside.json()["collections"][0]["collection"]["parent_id"] == eu["id"]
    denied = workspace.call("POST", paths, VIEWER, {"paths": [["Mine"]]})
    assert (denied.status_code, denied.json()["detail"]) == (403, MANAGE_REQUIRED)
    elsewhere = workspace.call(
        "POST", paths, MEMBER, {"parent_id": str(uuid4()), "paths": [["x"]]}
    )
    assert (elsewhere.status_code, elsewhere.json()["detail"]) == (422, NO_COLLECTION)
    assert workspace.call("POST", paths, MEMBER, {"paths": []}).status_code == 422
    assert workspace.call("POST", paths, MEMBER, {"paths": [[]]}).status_code == 422

    # A name is unique among siblings only: "payments" can be in two places.
    elsewhere_payments = workspace.call(
        "POST", collections, MEMBER, {"name": "payments", "parent_id": cards["id"]}
    )
    assert elsewhere_payments.status_code == 201, elsewhere_payments.json()
    twin = workspace.call(
        "POST", collections, MEMBER, {"name": "Payments", "parent_id": runbooks["id"]}
    )
    assert (twin.status_code, twin.json()["detail"]) == (409, NAME_TAKEN)
    top_twin = workspace.call("POST", collections, MEMBER, {"name": "RUNBOOKS"})
    assert top_twin.status_code == 409
    orphan = workspace.call(
        "POST", collections, MEMBER, {"name": "x", "parent_id": str(uuid4())}
    )
    assert (orphan.status_code, orphan.json()["detail"]) == (422, NO_COLLECTION)
    # A rename to a sibling's name, in any case, is refused too.
    clash = workspace.call(
        "PATCH", f"{collections}/{cards['id']}", MEMBER, {"name": "PAYMENTS"}
    )
    assert (clash.status_code, clash.json()["detail"]) == (409, NAME_TAKEN)
    # Its own name, in another case, isn't a clash.
    recased = workspace.call(
        "PATCH", f"{collections}/{cards['id']}", MEMBER, {"name": "Cards"}
    )
    assert recased.json()["name"] == "Cards"

    # Files land in the folders' collections; a folder and those in it list together.
    top = workspace.uploaded(kb, name="on-call.md", collection_id=runbooks["id"])
    deep = workspace.uploaded(kb, name="refunds.md", collection_id=payments["id"])
    loose = workspace.uploaded(kb, name="loose.md")
    subtree = workspace.names(kb, collection=[runbooks["id"], payments["id"], eu["id"]])
    assert sorted(subtree) == ["on-call.md", "refunds.md"]
    both = workspace.call(
        "GET", documents_of(kb), VIEWER, params={"collection": [eu["id"], "none"]}
    )
    assert [d["id"] for d in both.json()] == [loose["id"]]

    # Deleting payments moves what's in it up to Runbooks.
    path = f"{collections}/{payments['id']}"
    assert workspace.call("DELETE", path, MEMBER).status_code == 204
    listed = {c["id"]: c for c in workspace.call("GET", collections, VIEWER).json()}
    assert payments["id"] not in listed
    assert listed[eu["id"]]["parent_id"] == runbooks["id"]
    moved = workspace.call("GET", f"{documents_of(kb)}/{deep['id']}", VIEWER).json()
    assert moved["collection_id"] == runbooks["id"]
    assert listed[runbooks["id"]]["document_count"] == 2
    # Deleting a top collection unfiles its documents and lifts its collections.
    path = f"{collections}/{runbooks['id']}"
    assert workspace.call("DELETE", path, MEMBER).status_code == 204
    listed = {c["id"]: c for c in workspace.call("GET", collections, VIEWER).json()}
    assert listed[eu["id"]]["parent_id"] is None
    assert listed[cards["id"]]["parent_id"] is None
    lifted = workspace.call("GET", f"{documents_of(kb)}/{top['id']}", VIEWER).json()
    assert lifted["collection_id"] is None


def test_deleting_a_collection_whose_child_would_clash_is_refused(
    workspace: Workspace, kb: str
) -> None:
    paths = f"{collections_of(kb)}/paths"
    made = workspace.call(
        "POST", paths, MEMBER, {"paths": [["Archive", "Runbooks"], ["runbooks"]]}
    ).json()
    inner, top = (c["collection"] for c in made["collections"])
    # Archive's Runbooks would move up next to the top's runbooks.
    clash = workspace.call(
        "DELETE", f"{collections_of(kb)}/{inner['parent_id']}", MEMBER
    )
    assert (clash.status_code, clash.json()["detail"]) == (409, NAME_TAKEN)
    listed = {c["id"] for c in workspace.call("GET", collections_of(kb)).json()}
    assert listed == {inner["id"], inner["parent_id"], top["id"]}


def test_a_collection_can_hold_one_named_like_it(workspace: Workspace, kb: str) -> None:
    collections = collections_of(kb)
    made = workspace.call(
        "POST", f"{collections}/paths", MEMBER, {"paths": [["Specs", "Specs"]]}
    )
    inner = made.json()["collections"][0]["collection"]
    assert inner["parent_id"] is not None
    # Deleting the outer one lifts the inner one into its place.
    outer = f"{collections}/{inner['parent_id']}"
    assert workspace.call("DELETE", outer, MEMBER).status_code == 204
    listed = workspace.call("GET", collections, VIEWER).json()
    assert [(c["id"], c["name"], c["parent_id"]) for c in listed] == [
        (inner["id"], "Specs", None)
    ]
