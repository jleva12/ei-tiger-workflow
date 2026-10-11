"""System design knowledge bases without MySQL or Spanner: the tables on
SQLite (foreign keys enforced, as MySQL does), the organization's access in
memory (the default grants), the worker's queue played by writing to its
rows, and the code graph by the real ``CodeGraph`` over an in-memory store
(``app.state.codegraph``).

The people: an organization admin, a member and a viewer of the
organization, a member of another, and an outsider.
"""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_codegraph import citation_ref
from forge_codegraph_query import (
    CodeGraph,
    GraphError,
    InvalidRequest,
    NotFound,
    Unavailable,
    graph_id,
    to_json,
)
from forge_codegraph_query.testing import MemStore, span
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import MetaData, event, select, update
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
from forge_admin.knowledge.search import Passage
from forge_admin.knowledge.tools import OrganizationKnowledgeBases
from forge_admin.models import (
    CodeIngestionJob,
    CodeRepository,
    KnowledgeDocument,
    Organization,
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
REPOS = f"/organizations/{ORG}/code-repositories"
WIDGETS = "https://github.com/Acme/Widgets"
GADGETS = "https://github.com/acme/gadgets"
GRAPH_ID = "repo:widgets-graph"
MISSING = "3f6c0000-0000-4000-8000-00000000ffff"

GRANTS = {
    "org:admin": (
        "organizations:read",
        "knowledge_bases:manage",
        "repositories:manage",
        "repositories:read",
    ),
    "org:member": (
        "organizations:read",
        "knowledge_bases:manage",
        "repositories:manage",
        "repositories:read",
    ),
    "org:viewer": ("organizations:read", "repositories:read"),
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


SIGNER_SOURCE = "class Signer:\n    pass\n"


@dataclass
class GraphStore(MemStore):
    """The widgets' graph (and the others ``add`` gives it) in memory,
    recording what's written; set ``refuse`` to make every read and write
    fail so."""

    refuse: GraphError | None = None
    # Each owner's cross-repository links, as last written.
    written: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _check(self) -> None:
        if self.refuse is not None:
            raise self.refuse

    async def get_node(self, repo: str, node_id: str, generation: int) -> Any:
        self._check()
        return await super().get_node(repo, node_id, generation)

    async def neighbors(self, query: Any) -> Any:
        self._check()
        return await super().neighbors(query)

    async def hybrid_search(self, request: Any) -> Any:
        self._check()
        return await super().hybrid_search(request)

    async def stats(self, repo: str, model: str, dimensions: int) -> Any:
        self._check()
        return await super().stats(repo, model, dimensions)

    async def replace_cross_links(self, owner: str, links: Any) -> None:
        self._check()
        await super().replace_cross_links(owner, links)
        self.written[owner] = [to_json(link) for link in links]

    def declare(
        self,
        node_id: str,
        qualified: str,
        path: str,
        *,
        kind: str = "class",
        source: str = "",
        lines: tuple[int, int] = (0, 0),
        repo: str = "",
    ) -> dict[str, Any]:
        """A node of ``repo`` (this store's by default), with its source when
        given."""
        record: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "name": qualified.rsplit(".", 1)[-1],
            "qualified_name": qualified,
            "properties": {"file_path": {"string": path}},
        }
        held: MemStore = self
        if repo and repo != self.repo:
            held = self.others.get(repo) or self.add(repo)
        if source:
            sha = hashlib.sha256(source.encode()).hexdigest()
            held.sources[sha] = source.encode()
            record["source"] = {
                "lineage": graph_id("file", held.repo, path),
                "content_sha256": sha,
                "span": span(0, lines[0], len(source.encode()), lines[1]),
            }
        return held.add_node(record)


def signer(store: GraphStore) -> None:
    """The widgets' Signer and verify, both found by any search."""
    store.declare(
        "entity:signer",
        "widgets.sign.Signer",
        "src/widgets/sign.py",
        source=SIGNER_SOURCE,
        lines=(10, 11),
    )
    store.declare(
        "entity:verify",
        "widgets.sign.verify",
        "src/widgets/sign.py",
        kind="function",
        source="def verify():\n    pass\n",
        lines=(13, 14),
    )
    store.hits = [
        store.hit("entity:signer", 0.8, False),
        store.hit("entity:verify", 0.7, False),
    ]


@dataclass
class FakeSearch:
    """The documents' search, answering the same passages every time."""

    passages: list[Passage] = field(default_factory=list)
    model_id: str = "test-model@8"

    async def search(
        self, knowledge_base_ids: list[str], query: str, *, limit: int = 8
    ) -> list[Passage]:
        return [p for p in self.passages if p.knowledge_base_id in knowledge_base_ids]

    async def aclose(self) -> None:
        pass


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    tokens: dict[str, str]
    store: GraphStore
    graph: CodeGraph

    def call(
        self,
        method: str,
        path: str,
        user: str = MEMBER,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
            params=params,
        )

    def knowledge_base(self, name: str = "Code", kind: str = "system") -> str:
        made = self.call("POST", KBS, MEMBER, {"name": name, "kind": kind})
        assert made.status_code == 201, made.json()
        return str(made.json()["id"])

    def include(self, kb: str, body: dict[str, Any], user: str = MEMBER) -> Response:
        return self.call("POST", f"{KBS}/{kb}/repositories", user, body)

    def run[T](self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async def main() -> T:
            async with self.sessions() as session:
                return await work(session)

        assert self.client.portal is not None
        return self.client.portal.call(main)

    def ingested(self, repository_id: str, graph_id: str = GRAPH_ID) -> None:
        """Have the worker finish the repository's ingestions, as it writes them."""

        async def write(session: AsyncSession) -> None:
            await session.execute(
                update(CodeIngestionJob)
                .where(CodeIngestionJob.repository_id == repository_id)
                .values(
                    status="SUCCEEDED",
                    codegraph_repository_id=graph_id,
                    generation=3,
                    eligible_at=None,
                )
            )
            await session.commit()

        self.run(write)

    def repositories_in_organization(self) -> list[str]:
        async def urls(session: AsyncSession) -> list[str]:
            return list(
                await session.scalars(
                    select(CodeRepository.url).order_by(CodeRepository.url)
                )
            )

        return self.run(urls)


def sqlite_tables() -> MetaData:
    """The tables the routes use, as SQLite takes them: without MySQL's
    microsecond CURRENT_TIMESTAMP(6) defaults (SQLAlchemy sets the times)."""
    tables = MetaData()
    for name in (
        "organizations",
        "code_repositories",
        "code_ingestion_jobs",
        "knowledge_bases",
        "knowledge_base_groups",
        "knowledge_base_components",
        "knowledge_base_repositories",
        "knowledge_base_connections",
        "knowledge_base_code_links",
        "knowledge_collections",
        "knowledge_documents",
    ):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            if column.name != "kind":
                column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    store = GraphStore(GRAPH_ID)
    graph = CodeGraph(store)
    app.state.codegraph = graph
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection: Any, _: Any) -> None:
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
        yield Workspace(client, sessions, tokens, store, graph)
        client.portal.call(engine.dispose)


# ------------------------------------------------------------ knowledge bases


def test_a_system_design_knowledge_base_is_made_and_listed_with_its_kind(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base("Code")
    workspace.knowledge_base("Policies", kind="rag")
    listed = workspace.call("GET", KBS, VIEWER).json()
    assert [(k["name"], k["kind"]) for k in listed] == [
        ("Code", "system"),
        ("Policies", "rag"),
    ]
    read = workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()
    assert (read["repositories"], read["ingested"], read["documents"]) == (0, 0, 0)
    # The kind is fixed; anything but rag or system is refused.
    assert (
        workspace.call("POST", KBS, MEMBER, {"name": "X", "kind": "wiki"}).status_code
        == 422
    )
    # A knowledge base made without a kind is of documents.
    made = workspace.call("POST", KBS, MEMBER, {"name": "Docs"}).json()
    assert made["kind"] == "rag"


def test_a_system_design_knowledge_base_holds_no_documents_or_collections(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    for method, path in (
        ("GET", f"{KBS}/{kb}/documents"),
        ("POST", f"{KBS}/{kb}/document-collections"),
        ("POST", f"{KBS}/{kb}/reindex"),
    ):
        answer = workspace.call(method, path, MEMBER, {"name": "Folder"})
        assert answer.status_code == 409, (path, answer.json())
        assert "code repositories" in answer.json()["detail"]
    # And a RAG one holds no repositories.
    docs = workspace.knowledge_base("Docs", kind="rag")
    assert workspace.call("GET", f"{KBS}/{docs}/repositories").status_code == 409


# --------------------------------------------------------------- repositories


def test_a_github_url_is_added_to_the_organization_and_included(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    added = workspace.include(kb, {"url": WIDGETS, "branch": "main"})
    assert added.status_code == 201, added.json()
    repository = added.json()
    assert repository["url"] == "https://github.com/acme/widgets"
    # Its first ingestion is queued, as adding it on its own page would.
    assert repository["latest_ingestion"]["status"] == "QUEUED"
    assert workspace.repositories_in_organization() == [repository["url"]]
    listed = workspace.call("GET", f"{KBS}/{kb}/repositories", VIEWER).json()
    assert [r["id"] for r in listed] == [repository["id"]]
    read = workspace.call("GET", f"{KBS}/{kb}").json()
    assert (read["repositories"], read["ingested"]) == (1, 0)
    # Including it again answers it, and adds nothing.
    again = workspace.include(kb, {"url": WIDGETS, "branch": "main"})
    assert again.status_code == 200
    assert workspace.repositories_in_organization() == [repository["url"]]
    # The organization has it on main: not on another branch.
    other = workspace.include(kb, {"url": WIDGETS, "branch": "develop"})
    assert other.status_code == 409
    assert "'main'" in other.json()["detail"]


def test_one_of_the_organizations_repositories_is_included_by_id(
    workspace: Workspace,
) -> None:
    made = workspace.call(
        "POST", REPOS, MEMBER, {"url": GADGETS, "branch": "main", "ingest": False}
    ).json()
    first, second = (
        workspace.knowledge_base("Code"),
        workspace.knowledge_base("More code"),
    )
    for kb in (first, second):
        assert workspace.include(kb, {"repository_id": made["id"]}).status_code == 201
    # Another organization's isn't found.
    other = workspace.include(first, {"repository_id": MISSING})
    assert other.status_code == 404
    # One or the other, not both or neither.
    for body in (
        {},
        {"repository_id": made["id"], "url": GADGETS, "branch": "main"},
        {"url": GADGETS},
    ):
        assert workspace.include(first, body).status_code == 422


def test_removing_a_repository_or_the_knowledge_base_leaves_it_in_the_organization(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    repository = workspace.include(kb, {"url": WIDGETS, "branch": "main"}).json()
    path = f"{KBS}/{kb}/repositories/{repository['id']}"
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("DELETE", path).status_code == 404
    assert workspace.call("GET", f"{KBS}/{kb}/repositories").json() == []
    assert workspace.include(kb, {"repository_id": repository["id"]}).status_code == 201
    assert workspace.call("DELETE", f"{KBS}/{kb}").status_code == 204
    assert workspace.repositories_in_organization() == [repository["url"]]
    # Removing it from the organization removes it from every knowledge base.
    kb = workspace.knowledge_base("Again")
    workspace.include(kb, {"repository_id": repository["id"]})
    assert workspace.call("DELETE", f"{REPOS}/{repository['id']}").status_code == 204
    assert workspace.call("GET", f"{KBS}/{kb}/repositories").json() == []


def test_only_those_who_manage_knowledge_bases_include_repositories(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    for user in (VIEWER, NEIGHBOUR, OUTSIDER):
        refused = workspace.include(kb, {"url": WIDGETS, "branch": "main"}, user)
        assert refused.status_code in (403, 404), (user, refused.json())
    assert workspace.repositories_in_organization() == []
    assert (
        workspace.call("GET", f"{KBS}/{kb}/repositories", NEIGHBOUR).status_code == 403
    )


# ---------------------------------------------------------------- graph reads


def test_graph_reads_wait_for_an_ingestion_to_succeed(workspace: Workspace) -> None:
    kb = workspace.knowledge_base()
    repository = workspace.include(kb, {"url": WIDGETS, "branch": "main"}).json()
    graph = f"{REPOS}/{repository['id']}/graph"
    early = workspace.call("GET", graph, VIEWER)
    assert early.status_code == 409
    assert "ingest it first" in early.json()["detail"]

    workspace.ingested(repository["id"])
    signer(workspace.store)
    workspace.store.add_edge("calls", "entity:verify", "entity:signer", line=13)
    read = workspace.call(
        "GET",
        f"{graph}/neighbors",
        VIEWER,
        params={"node": "entity:signer", "generation": 1},
    )
    assert read.status_code == 200, read.json()
    assert read.json()["generation"] == 1
    [neighbor] = read.json()["neighbors"]
    assert neighbor["edge"]["fact"]["edge"]["kind"] == "calls"
    assert neighbor["node"]["fact"]["node"]["id"] == "entity:verify"
    view = workspace.call("GET", graph, VIEWER).json()
    assert {n["fact"]["node"]["id"] for n in view["nodes"]} == {
        "entity:signer",
        "entity:verify",
    }
    assert len(view["edges"]) == 1
    symbols = workspace.call(
        "GET", f"{graph}/symbols", VIEWER, params={"name": " signer "}
    ).json()
    assert [n["fact"]["node"]["id"] for n in symbols["nodes"]] == ["entity:signer"]
    source = workspace.call(
        "GET", f"{graph}/source", VIEWER, params={"node": "entity:signer"}
    ).json()
    assert (source["text"], source["text_start_line"]) == (SIGNER_SOURCE, 10)
    node = workspace.call(
        "GET", f"{graph}/node", VIEWER, params={"node": "entity:signer"}
    )
    assert node.json()["fact"]["node"]["qualified_name"] == "widgets.sign.Signer"
    read = workspace.call("GET", f"{KBS}/{kb}").json()
    assert (read["repositories"], read["ingested"]) == (1, 1)
    # The code graph's failures as they are: a node it doesn't have, a read
    # it refuses, a database that doesn't answer.
    missing = workspace.call(
        "GET", f"{graph}/node", VIEWER, params={"node": "entity:gone"}
    )
    assert missing.status_code == 404
    for error, expected in (
        (InvalidRequest("cursor"), 400),
        (Unavailable("the code graph database didn't answer"), 503),
    ):
        workspace.store.refuse = error
        answer = workspace.call(
            "GET", f"{graph}/node", VIEWER, params={"node": "entity:signer"}
        )
        assert answer.status_code == expected, (error, answer.json())
    # Another organization's people read none of it.
    workspace.store.refuse = None
    assert workspace.call("GET", graph, NEIGHBOUR).status_code == 403
    # Arguments out of the code graph's range are refused before reading.
    assert (
        workspace.call(
            "GET", f"{graph}/neighbors", VIEWER, params={"node": "x", "limit": 201}
        ).status_code
        == 422
    )


def test_the_stats_say_what_the_code_graph_can_tell(workspace: Workspace) -> None:
    kb = workspace.knowledge_base()
    repository = workspace.include(kb, {"url": WIDGETS, "branch": "main"}).json()
    stats = f"{REPOS}/{repository['id']}/stats"
    first = workspace.call("GET", stats, VIEWER).json()
    assert first["code_graph"]["status"] == "not_ingested"

    workspace.ingested(repository["id"])
    signer(workspace.store)
    workspace.store.searchable = 2
    workspace.store.run_records = [
        {
            "key": {"repository_id": GRAPH_ID, "run_id": f"run-{i}"},
            "request": {"branch": "main", "target_commit_sha": "abc"},
            "phase": "SUCCEEDED",
            "generation": 1,
            "metrics": {"files": 4},
        }
        for i in range(7)
    ]
    read = workspace.call("GET", stats, VIEWER, params={"runs": 5}).json()
    graph = read["code_graph"]
    assert graph["status"] == "ok"
    assert (graph["totals"]["nodes"], graph["totals"]["edges"]) == (2, 0)
    assert graph["totals"]["node_kinds"] == {"class": 1, "function": 1}
    assert graph["totals"]["searchable"] == 2
    assert [(r["run_id"], r["phase"], r["commit_sha"]) for r in graph["runs"]] == [
        (f"run-{i}", "SUCCEEDED", "abc") for i in range(5)
    ]
    # A code graph that doesn't answer leaves them unavailable, not failing.
    workspace.store.refuse = Unavailable("the code graph database didn't answer")
    down = workspace.call("GET", stats, VIEWER)
    assert down.status_code == 200
    assert down.json()["code_graph"]["status"] == "unavailable"
    # One that no longer has the repository says so.
    workspace.store.refuse = NotFound(f"repository {GRAPH_ID}")
    gone = workspace.call("GET", stats, VIEWER).json()["code_graph"]
    assert gone["status"] == "not_ingested"


# -------------------------------------------------------------------- search


def test_searching_a_system_design_knowledge_base_answers_its_code(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    repository = workspace.include(kb, {"url": WIDGETS, "branch": "main"}).json()
    search = f"{KBS}/{kb}/search"
    # Nothing ingested yet: nothing to find, and the code graph isn't asked.
    assert workspace.call("POST", search, VIEWER, {"query": "signing"}).json() == {
        "hits": []
    }
    assert workspace.store.searches == []

    workspace.ingested(repository["id"])
    signer(workspace.store)
    found = workspace.call(
        "POST", search, VIEWER, {"query": "how is it signed", "limit": 1}
    )
    assert found.status_code == 200, found.json()
    assert found.json()["hits"] == [
        {
            "chunk_id": "entity:signer",
            "ref": citation_ref(f"{GRAPH_ID}:entity:signer"),
            "document_id": f"{repository['id']}:src/widgets/sign.py",
            "filename": "Acme/Widgets: src/widgets/sign.py",
            "section_path": ["class widgets.sign.Signer"],
            "location": "src/widgets/sign.py, lines 10–11",
            "text": SIGNER_SOURCE,
            "score": 0.8,
            "repository_id": repository["id"],
            "node_id": "entity:signer",
        }
    ]
    # The code graph's own search: hybrid, over the knowledge base's graphs.
    asked = workspace.store.searches[-1]
    assert (asked.repository_ids, asked.text, asked.limit) == (
        [GRAPH_ID],
        "how is it signed",
        1,
    )
    workspace.store.refuse = Unavailable("the code graph database didn't answer")
    assert workspace.call("POST", search, VIEWER, {"query": "x"}).status_code == 503


def test_the_organizations_search_covers_its_rag_knowledge_bases(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base()
    named = workspace.call(
        "POST", f"{KBS}/search", VIEWER, {"query": "x", "knowledge_base_ids": [kb]}
    )
    assert named.status_code == 409


def test_agents_search_documents_and_code_together(workspace: Workspace) -> None:
    code = workspace.knowledge_base("Widgets code")
    docs = workspace.knowledge_base("Runbooks", kind="rag")
    repository = workspace.include(code, {"url": WIDGETS, "branch": "main"}).json()
    workspace.ingested(repository["id"])
    signer(workspace.store)

    async def upload(session: AsyncSession) -> str:
        document = KnowledgeDocument(
            knowledge_base_id=docs,
            filename="Signing.md",
            size_bytes=10,
            sha256="0" * 64,
            storage_uri="s3://b/k",
            job_key="k",
            phase="SUCCEEDED",
        )
        session.add(document)
        await session.commit()
        return document.id

    document_id = workspace.run(upload)
    search = FakeSearch(
        [
            Passage(
                chunk_id="c1",
                document_id=document_id,
                title="Signing",
                text="Rotate the signing key yearly.",
                score=0.6,
                knowledge_base_id=docs,
            )
        ]
    )
    tools = OrganizationKnowledgeBases(workspace.sessions, search, workspace.graph)

    async def ask() -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
        described = await tools.describe([code, docs], organization_id=ORG)
        passages = await tools.search(
            [code, docs], "signing", organization_id=ORG, limit=3
        )
        return described, passages

    assert workspace.client.portal is not None
    described, passages = workspace.client.portal.call(ask)
    assert described == [("Widgets code", "code of Acme/Widgets"), ("Runbooks", "")]
    # Documents' and code's best first, in turn.
    assert [(p["knowledge_base"], p["document"]) for p in passages] == [
        ("Runbooks", "Signing.md"),
        ("Widgets code", "Acme/Widgets: src/widgets/sign.py"),
        ("Widgets code", "Acme/Widgets: src/widgets/sign.py"),
    ]
    assert passages[1]["section"] == "class widgets.sign.Signer"
    assert passages[1]["node_id"] == "entity:signer"
    assert passages[2]["node_id"] == "entity:verify"

    # Without the code graph, a system design knowledge base's search says so.
    without = OrganizationKnowledgeBases(workspace.sessions, search, None)  # type: ignore[arg-type]

    async def ask_without() -> Any:
        return await without.search([code], "signing", organization_id=ORG, limit=3)

    with pytest.raises(RuntimeError, match="FORGE_ADMIN_CODEGRAPH__SPANNER__DATABASE"):
        workspace.client.portal.call(ask_without)


# ---------------------------------------------------------------- connections

ORDERS = "https://github.com/acme/orders"
ORDERS_GRAPH = "repo:orders-graph"


def node(node_id: str, name: str, path: str, kind: str = "method") -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "qualified_name": f"acme.{name}",
        "properties": {"file_path": {"string": path}},
    }


def system(workspace: Workspace) -> tuple[str, str, str]:
    """A system design knowledge base with two applications: its ID, then
    the widgets one's and the orders one's."""
    kb = workspace.knowledge_base("Checkout")
    widgets = workspace.include(kb, {"url": WIDGETS, "branch": "main"}).json()["id"]
    orders = workspace.include(kb, {"url": ORDERS, "branch": "main"}).json()["id"]
    return kb, widgets, orders


def connect(
    workspace: Workspace, kb: str, source: str, target: str, **extra: Any
) -> Response:
    return workspace.call(
        "POST",
        f"{KBS}/{kb}/connections",
        MEMBER,
        {"source_repository_id": source, "target_repository_id": target, **extra},
    )


def test_applications_are_connected_and_listed(workspace: Workspace) -> None:
    kb, widgets, orders = system(workspace)
    made = connect(
        workspace, kb, widgets, orders, kind="calls", description=" POST /v1/orders "
    )
    assert made.status_code == 201, made.json()
    connection = made.json()
    assert (
        connection["kind"],
        connection["description"],
        connection["code_links"],
    ) == (
        "calls",
        "POST /v1/orders",
        0,
    )
    listed = workspace.call("GET", f"{KBS}/{kb}/connections", VIEWER).json()
    assert [(c["source_repository_id"], c["target_repository_id"]) for c in listed] == [
        (widgets, orders)
    ]
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()["connections"] == 1
    # The same way again is a conflict; another way, or back, is another.
    assert connect(workspace, kb, widgets, orders, kind="calls").status_code == 409
    assert connect(workspace, kb, widgets, orders, kind="events").status_code == 201
    assert connect(workspace, kb, orders, widgets).status_code == 201
    # Not to itself, nor to an application the knowledge base doesn't have.
    assert connect(workspace, kb, widgets, widgets).status_code == 422
    other = workspace.call(
        "POST", REPOS, MEMBER, {"url": GADGETS, "branch": "main", "ingest": False}
    ).json()["id"]
    assert connect(workspace, kb, widgets, other).status_code == 422
    # A changed kind or note.
    changed = workspace.call(
        "PATCH",
        f"{KBS}/{kb}/connections/{connection['id']}",
        MEMBER,
        {"kind": "depends_on", "description": "the SDK"},
    ).json()
    assert (changed["kind"], changed["description"]) == ("depends_on", "the SDK")
    # Removing one.
    path = f"{KBS}/{kb}/connections/{connection['id']}"
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("DELETE", path).status_code == 404


def test_only_those_who_manage_knowledge_bases_draw_connections(
    workspace: Workspace,
) -> None:
    kb, widgets, orders = system(workspace)
    body = {"source_repository_id": widgets, "target_repository_id": orders}
    for user in (VIEWER, NEIGHBOUR, OUTSIDER):
        refused = workspace.call("POST", f"{KBS}/{kb}/connections", user, body)
        assert refused.status_code in (401, 403), (user, refused.json())
    assert (
        workspace.call("GET", f"{KBS}/{kb}/connections", NEIGHBOUR).status_code == 403
    )
    # A RAG knowledge base has no applications to connect.
    docs = workspace.knowledge_base("Docs", kind="rag")
    assert workspace.call("GET", f"{KBS}/{docs}/connections").status_code == 409


def test_removing_an_application_removes_its_connections(
    workspace: Workspace,
) -> None:
    kb, widgets, orders = system(workspace)
    connect(workspace, kb, widgets, orders)
    connect(workspace, kb, orders, widgets, kind="events")
    removed = workspace.call("DELETE", f"{KBS}/{kb}/repositories/{orders}")
    assert removed.status_code == 204
    assert workspace.call("GET", f"{KBS}/{kb}/connections").json() == []
    # Nothing was linked in the code, so the code graph wasn't asked.
    assert workspace.store.written == {}


def code_linked(workspace: Workspace) -> tuple[str, str, str, str]:
    """Checkout's two applications ingested and connected, with their
    nodes in the graph: the knowledge base, the two, and the connection."""
    kb, widgets, orders = system(workspace)
    workspace.ingested(widgets, GRAPH_ID)
    workspace.ingested(orders, ORDERS_GRAPH)
    workspace.store.add_node(
        node("entity:client", "OrdersClient.create", "src/client.py")
    )
    workspace.store.add(ORDERS_GRAPH).add_node(
        node("entity:handler", "create_order", "app/routes.py", "function")
    )
    connection = connect(workspace, kb, widgets, orders, kind="calls").json()["id"]
    return kb, widgets, orders, connection


def test_code_links_say_where_and_reach_the_code_graph(workspace: Workspace) -> None:
    kb, widgets, orders, connection = code_linked(workspace)
    path = f"{KBS}/{kb}/connections/{connection}/code-links"
    made = workspace.call(
        "POST",
        path,
        MEMBER,
        {
            "source_node_id": "entity:client",
            "target_node_id": "entity:handler",
            "label": "POST /v1/orders",
        },
    )
    assert made.status_code == 201, made.json()
    code = made.json()
    assert code["source"] == {
        "node_id": "entity:client",
        "kind": "method",
        "name": "OrdersClient.create",
        "qualified_name": "acme.OrdersClient.create",
        "path": "src/client.py",
    }
    assert code["target"]["path"] == "app/routes.py"
    assert workspace.call("GET", path, VIEWER).json()[0]["id"] == code["id"]
    listed = workspace.call("GET", f"{KBS}/{kb}/connections").json()
    assert listed[0]["code_links"] == 1
    # The knowledge base's set, in the code graph.
    assert workspace.store.written[f"kb:{kb}"] == [
        {
            "id": code["id"],
            "owner": f"kb:{kb}",
            "kind": "calls_api",
            "source": {
                "repository_id": GRAPH_ID,
                "node_id": "entity:client",
                "qualified_name": "acme.OrdersClient.create",
                "kind": "method",
            },
            "target": {
                "repository_id": ORDERS_GRAPH,
                "node_id": "entity:handler",
                "qualified_name": "acme.create_order",
                "kind": "function",
            },
            "label": "POST /v1/orders",
            "provenance": "manual",
            "created_by": MEMBER,
        }
    ]
    # Linking them again is a conflict; a node the graph lacks is refused.
    again = {"source_node_id": "entity:client", "target_node_id": "entity:handler"}
    assert workspace.call("POST", path, MEMBER, again).status_code == 409
    missing = {"source_node_id": "entity:gone", "target_node_id": "entity:handler"}
    assert workspace.call("POST", path, MEMBER, missing).status_code == 422
    # A new kind changes the link's kind in the graph.
    workspace.call(
        "PATCH", f"{KBS}/{kb}/connections/{connection}", MEMBER, {"kind": "events"}
    )
    assert workspace.store.written[f"kb:{kb}"][0]["kind"] == "sends_event"
    # Removing it takes it out.
    removed = workspace.call("DELETE", f"{path}/{code['id']}")
    assert removed.status_code == 204
    assert workspace.store.written[f"kb:{kb}"] == []


def test_code_links_wait_for_both_applications_to_be_ingested(
    workspace: Workspace,
) -> None:
    kb, widgets, orders = system(workspace)
    workspace.ingested(widgets, GRAPH_ID)
    connection = connect(workspace, kb, widgets, orders).json()["id"]
    early = workspace.call(
        "POST",
        f"{KBS}/{kb}/connections/{connection}/code-links",
        MEMBER,
        {"source_node_id": "entity:client", "target_node_id": "entity:handler"},
    )
    assert early.status_code == 409
    assert "acme/orders isn't in the code graph yet" in early.json()["detail"]


def test_a_refusing_code_graph_changes_nothing(workspace: Workspace) -> None:
    kb, widgets, orders, connection = code_linked(workspace)
    path = f"{KBS}/{kb}/connections/{connection}/code-links"
    workspace.call(
        "POST",
        path,
        MEMBER,
        {"source_node_id": "entity:client", "target_node_id": "entity:handler"},
    )
    workspace.store.refuse = Unavailable("the code graph database didn't answer")
    for method, target in (
        ("DELETE", f"{KBS}/{kb}/connections/{connection}"),
        ("DELETE", f"{KBS}/{kb}/repositories/{orders}"),
        ("DELETE", f"{KBS}/{kb}"),
        ("DELETE", f"{REPOS}/{widgets}"),
    ):
        answer = workspace.call(method, target)
        assert answer.status_code == 503, (target, answer.json())
    workspace.store.refuse = None
    assert len(workspace.call("GET", path).json()) == 1
    # What's in the code graph is what was there before.
    assert [link.owner for link in workspace.store.links] == [f"kb:{kb}"]


def test_removing_what_has_code_links_takes_them_out_of_the_code_graph(
    workspace: Workspace,
) -> None:
    def linked() -> tuple[str, str, str]:
        kb, widgets, orders, connection = code_linked(workspace)
        workspace.call(
            "POST",
            f"{KBS}/{kb}/connections/{connection}/code-links",
            MEMBER,
            {"source_node_id": "entity:client", "target_node_id": "entity:handler"},
        )
        assert len(workspace.store.written[f"kb:{kb}"]) == 1
        return kb, widgets, connection

    kb, _, connection = linked()
    workspace.call("DELETE", f"{KBS}/{kb}/connections/{connection}")
    assert workspace.store.written[f"kb:{kb}"] == []
    workspace.call("DELETE", f"{KBS}/{kb}")

    kb, widgets, _ = linked()
    workspace.call("DELETE", f"{KBS}/{kb}/repositories/{widgets}")
    assert workspace.store.written[f"kb:{kb}"] == []
    workspace.call("DELETE", f"{KBS}/{kb}")
    assert workspace.store.written[f"kb:{kb}"] == []


def test_agents_are_told_how_the_applications_connect(workspace: Workspace) -> None:
    kb, widgets, orders = system(workspace)
    connect(workspace, kb, widgets, orders, kind="calls", description="POST /orders")
    tools = OrganizationKnowledgeBases(workspace.sessions, None, workspace.graph)

    async def ask() -> list[tuple[str, str]]:
        return await tools.describe([kb], organization_id=ORG)

    assert workspace.client.portal is not None
    assert workspace.client.portal.call(ask) == [
        (
            "Checkout",
            "code of acme/orders, Acme/Widgets; how they connect: Acme/Widgets "
            "calls the API of acme/orders (POST /orders)",
        )
    ]


# ----------------------------------------------------------------- system map


def test_the_system_map_is_the_same_for_everyone(workspace: Workspace) -> None:
    kb, widgets, orders = system(workspace)
    path = f"{KBS}/{kb}/map"
    assert workspace.call("GET", path, VIEWER).json() == {
        "layout": None,
        "positions": {},
        "groups": {},
    }
    moved = workspace.call(
        "PATCH",
        path,
        MEMBER,
        {
            "layout": "breadthfirst",
            "positions": {widgets: {"x": -120.5, "y": 40}, orders: {"x": 200, "y": 0}},
        },
    )
    assert moved.status_code == 200, moved.json()
    # Someone else moves one: only that one moves, and the layout stays.
    workspace.call("PATCH", path, ADMIN, {"positions": {orders: {"x": 10, "y": 90}}})
    assert workspace.call("GET", path, VIEWER).json() == {
        "layout": "breadthfirst",
        "positions": {
            widgets: {"x": -120.5, "y": 40.0},
            orders: {"x": 10.0, "y": 90.0},
        },
        "groups": {},
    }
    # An application it doesn't include is ignored; removing one forgets
    # its place.
    workspace.call("PATCH", path, MEMBER, {"positions": {MISSING: {"x": 1, "y": 1}}})
    workspace.call("DELETE", f"{KBS}/{kb}/repositories/{orders}")
    assert list(workspace.call("GET", path).json()["positions"]) == [widgets]


def test_only_those_who_manage_knowledge_bases_move_the_map(
    workspace: Workspace,
) -> None:
    kb, widgets, _ = system(workspace)
    path = f"{KBS}/{kb}/map"
    body = {"positions": {widgets: {"x": 1, "y": 2}}}
    for user in (VIEWER, NEIGHBOUR):
        assert workspace.call("PATCH", path, user, body).status_code == 403
    assert workspace.call("GET", path, NEIGHBOUR).status_code == 403
    # Nonsense is refused: an unknown layout, a place off the map.
    assert workspace.call("PATCH", path, MEMBER, {"layout": "grid"}).status_code == 422
    far = {"positions": {widgets: {"x": 1e12, "y": 0}}}
    assert workspace.call("PATCH", path, MEMBER, far).status_code == 422
    docs = workspace.knowledge_base("Docs", kind="rag")
    assert workspace.call("GET", f"{KBS}/{docs}/map").status_code == 409


# ----------------------------------------------------- components and groups


def component(workspace: Workspace, kb: str, **body: Any) -> Response:
    return workspace.call("POST", f"{KBS}/{kb}/components", MEMBER, body)


def group(workspace: Workspace, kb: str, **body: Any) -> Response:
    return workspace.call("POST", f"{KBS}/{kb}/groups", MEMBER, body)


def test_components_are_added_listed_changed_and_removed(
    workspace: Workspace,
) -> None:
    kb, widgets, _ = system(workspace)
    made = component(
        workspace,
        kb,
        kind="database",
        name=" orders-db ",
        technology="PostgreSQL 16",
        description="Orders and their lines",
    )
    assert made.status_code == 201, made.json()
    db = made.json()
    assert (db["kind"], db["name"], db["technology"]) == (
        "database",
        "orders-db",
        "PostgreSQL 16",
    )
    component(workspace, kb, kind="external", name="Stripe")
    listed = workspace.call("GET", f"{KBS}/{kb}/components", VIEWER).json()
    assert [c["name"] for c in listed] == ["Stripe", "orders-db"]
    assert workspace.call("GET", f"{KBS}/{kb}", VIEWER).json()["components"] == 2
    # A name is the knowledge base's once; a kind must be one it knows.
    assert component(workspace, kb, kind="cache", name="orders-db").status_code == 409
    assert component(workspace, kb, kind="mainframe", name="x").status_code == 422
    assert component(workspace, kb, kind="cache", name=" ").status_code == 422
    # Another knowledge base may have its own orders-db.
    other = workspace.knowledge_base("Back office")
    assert (
        component(workspace, other, kind="database", name="orders-db").status_code
        == 201
    )
    changed = workspace.call(
        "PATCH",
        f"{KBS}/{kb}/components/{db['id']}",
        MEMBER,
        {"kind": "storage", "technology": "S3"},
    ).json()
    assert (changed["kind"], changed["name"], changed["technology"]) == (
        "storage",
        "orders-db",
        "S3",
    )
    rename = {"name": "Stripe"}
    path = f"{KBS}/{kb}/components/{db['id']}"
    assert workspace.call("PATCH", path, MEMBER, rename).status_code == 409
    # Removing it removes its connections.
    linked = connect_ends(
        workspace, kb, ("repository", widgets), ("component", db["id"])
    )
    assert linked.status_code == 201, linked.json()
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("GET", f"{KBS}/{kb}/connections").json() == []
    assert workspace.call("DELETE", path).status_code == 404
    # Another knowledge base's component isn't this one's.
    theirs = workspace.call("GET", f"{KBS}/{other}/components").json()[0]["id"]
    assert (
        workspace.call("DELETE", f"{KBS}/{kb}/components/{theirs}").status_code == 404
    )


def test_only_those_who_manage_knowledge_bases_add_components_and_groups(
    workspace: Workspace,
) -> None:
    kb = workspace.knowledge_base("Checkout")
    for user in (VIEWER, NEIGHBOUR, OUTSIDER):
        for path, body in (
            ("components", {"kind": "queue", "name": "orders"}),
            ("groups", {"name": "Payments"}),
        ):
            refused = workspace.call("POST", f"{KBS}/{kb}/{path}", user, body)
            assert refused.status_code in (401, 403), (user, path)
    assert workspace.call("GET", f"{KBS}/{kb}/groups", NEIGHBOUR).status_code == 403
    docs = workspace.knowledge_base("Docs", kind="rag")
    assert workspace.call("GET", f"{KBS}/{docs}/components").status_code == 409
    assert group(workspace, docs, name="Payments").status_code == 409


def connect_ends(
    workspace: Workspace,
    kb: str,
    source: tuple[str, str],
    target: tuple[str, str],
    **extra: Any,
) -> Response:
    """Connect two ends, each ``(repository|component, id)``."""
    return workspace.call(
        "POST",
        f"{KBS}/{kb}/connections",
        MEMBER,
        {
            f"source_{source[0]}_id": source[1],
            f"target_{target[0]}_id": target[1],
            **extra,
        },
    )


def test_applications_and_components_connect(workspace: Workspace) -> None:
    kb, widgets, orders = system(workspace)
    db = component(workspace, kb, kind="database", name="orders-db").json()["id"]
    topic = component(workspace, kb, kind="queue", name="order-events").json()["id"]
    writes = connect_ends(
        workspace, kb, ("repository", orders), ("component", db), kind="writes_to"
    )
    assert writes.status_code == 201, writes.json()
    assert {
        k: writes.json()[k]
        for k in (
            "source_repository_id",
            "source_component_id",
            "target_repository_id",
            "target_component_id",
        )
    } == {
        "source_repository_id": orders,
        "source_component_id": None,
        "target_repository_id": None,
        "target_component_id": db,
    }
    # A component to an application, and a component to a component.
    consumed = connect_ends(
        workspace, kb, ("component", topic), ("repository", widgets), kind="events"
    )
    assert consumed.status_code == 201
    assert (
        connect_ends(workspace, kb, ("component", topic), ("component", db)).status_code
        == 201
    )
    # The same way again is a conflict, ends that are components too.
    again = connect_ends(
        workspace, kb, ("repository", orders), ("component", db), kind="writes_to"
    )
    assert again.status_code == 409
    other_way = connect_ends(
        workspace, kb, ("repository", orders), ("component", db), kind="reads_from"
    )
    assert other_way.status_code == 201
    renamed = workspace.call(
        "PATCH",
        f"{KBS}/{kb}/connections/{other_way.json()['id']}",
        MEMBER,
        {"kind": "writes_to"},
    )
    assert renamed.status_code == 409
    # Each end is an application or a component, not both, nor neither.
    both = {
        "source_repository_id": orders,
        "source_component_id": db,
        "target_repository_id": widgets,
    }
    assert (
        workspace.call("POST", f"{KBS}/{kb}/connections", MEMBER, both).status_code
        == 422
    )
    neither = {"target_repository_id": widgets}
    assert (
        workspace.call("POST", f"{KBS}/{kb}/connections", MEMBER, neither).status_code
        == 422
    )
    # Not to itself, nor to another knowledge base's component.
    assert (
        connect_ends(workspace, kb, ("component", db), ("component", db)).status_code
        == 422
    )
    elsewhere = workspace.knowledge_base("Back office")
    theirs = component(workspace, elsewhere, kind="cache", name="redis").json()["id"]
    assert (
        connect_ends(
            workspace, kb, ("repository", orders), ("component", theirs)
        ).status_code
        == 422
    )
    # A component has no code to link.
    workspace.ingested(orders, ORDERS_GRAPH)
    code = workspace.call(
        "POST",
        f"{KBS}/{kb}/connections/{writes.json()['id']}/code-links",
        MEMBER,
        {"source_node_id": "entity:client", "target_node_id": "entity:handler"},
    )
    assert code.status_code == 422
    assert "component has none" in code.json()["detail"]
    # Removing the application removes its connections, components' too.
    workspace.call("DELETE", f"{KBS}/{kb}/repositories/{orders}")
    left = workspace.call("GET", f"{KBS}/{kb}/connections").json()
    assert [(c["source_component_id"], c["target_component_id"]) for c in left] == [
        (topic, None),
        (topic, db),
    ]


def test_groups_hold_applications_and_components_on_the_map(
    workspace: Workspace,
) -> None:
    kb, widgets, orders = system(workspace)
    made = group(
        workspace, kb, name="Payments", description="Charging customers, refunds"
    )
    assert made.status_code == 201, made.json()
    payments = made.json()
    assert payments["description"] == "Charging customers, refunds"
    assert group(workspace, kb, name="Payments").status_code == 409
    edge = group(workspace, kb, name="Edge").json()["id"]
    db = component(
        workspace, kb, kind="database", name="ledger", group_id=payments["id"]
    ).json()["id"]
    # A group the knowledge base doesn't have.
    stray = component(workspace, kb, kind="cache", name="redis", group_id=MISSING)
    assert stray.status_code == 404
    path = f"{KBS}/{kb}/map"
    moved = workspace.call(
        "PATCH",
        path,
        MEMBER,
        {
            "groups": {orders: payments["id"], widgets: edge},
            "positions": {db: {"x": 5, "y": 6}, edge: {"x": 300, "y": 20}},
        },
    )
    assert moved.status_code == 200, moved.json()
    seen = workspace.call("GET", path, VIEWER).json()
    assert seen["groups"] == {orders: payments["id"], widgets: edge, db: payments["id"]}
    assert seen["positions"] == {
        db: {"x": 5.0, "y": 6.0},
        edge: {"x": 300.0, "y": 20.0},
    }
    # Out of its group; into a group the knowledge base doesn't have, refused.
    workspace.call("PATCH", path, MEMBER, {"groups": {widgets: None}})
    assert widgets not in workspace.call("GET", path).json()["groups"]
    refused = workspace.call("PATCH", path, MEMBER, {"groups": {orders: MISSING}})
    assert refused.status_code == 422
    elsewhere = workspace.knowledge_base("Back office")
    theirs = group(workspace, elsewhere, name="Payments").json()["id"]
    refused = workspace.call("PATCH", path, MEMBER, {"groups": {orders: theirs}})
    assert refused.status_code == 422
    # Renamed and described again.
    changed = workspace.call(
        "PATCH",
        f"{KBS}/{kb}/groups/{payments['id']}",
        MEMBER,
        {"name": "Billing", "description": "Invoices"},
    ).json()
    assert (changed["name"], changed["description"]) == ("Billing", "Invoices")
    listed = workspace.call("GET", f"{KBS}/{kb}/groups", VIEWER).json()
    assert [g["name"] for g in listed] == ["Billing", "Edge"]
    # Removing it leaves what was in it on the map, in no group.
    removed = workspace.call("DELETE", f"{KBS}/{kb}/groups/{payments['id']}")
    assert removed.status_code == 204
    assert workspace.call("GET", path).json()["groups"] == {}
    assert len(workspace.call("GET", f"{KBS}/{kb}/components").json()) == 1


def test_agents_are_told_the_whole_design(workspace: Workspace) -> None:
    kb, widgets, orders = system(workspace)
    payments = group(workspace, kb, name="Payments", description="Charging").json()
    db = component(
        workspace,
        kb,
        kind="database",
        name="ledger",
        technology="PostgreSQL",
        group_id=payments["id"],
    ).json()["id"]
    workspace.call(
        "PATCH", f"{KBS}/{kb}/map", MEMBER, {"groups": {orders: payments["id"]}}
    )
    connect(workspace, kb, widgets, orders, kind="calls")
    connect_ends(
        workspace, kb, ("repository", orders), ("component", db), kind="writes_to"
    )
    docs = workspace.knowledge_base("Docs", kind="rag")
    tools = OrganizationKnowledgeBases(workspace.sessions, None, workspace.graph)

    async def ask() -> tuple[Any, Any, Any]:
        return (
            await tools.describe([kb], organization_id=ORG),
            await tools.design([docs, kb], organization_id=ORG, focus="ledger"),
            await tools.design([docs], organization_id=ORG),
        )

    assert workspace.client.portal is not None
    described, designed, none = workspace.client.portal.call(ask)
    assert described == [
        (
            "Checkout",
            "code of acme/orders, Acme/Widgets; other parts: ledger (database, "
            "PostgreSQL); groups: Payments (acme/orders, ledger): Charging; how "
            "they connect: Acme/Widgets calls the API of acme/orders; acme/orders "
            "writes to ledger",
        )
    ]
    (checkout,) = designed["systems"]
    assert checkout["connections"] == [
        {
            "source": "acme/orders",
            "relation": "writes to",
            "target": "ledger",
            "description": "",
        }
    ]
    assert checkout["components"][0]["group"] == "Payments"
    assert none is None
