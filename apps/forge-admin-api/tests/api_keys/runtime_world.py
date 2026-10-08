"""The workflow runtime's world: the API-keys world (key_world), with a run
store on SQLite, the async worker's queue stood in, runs' files kept in
memory, and the organizations' workflows: Ship (published v1, with a draft),
Drafty (never published), Theirs (another organization's, published) and
Papers (published, its start taking PDF and text files)."""

from collections.abc import Callable
from typing import Any

from forge_task_adk_workflows.run_store import RunStore, metadata
from google.adk.artifacts import InMemoryArtifactService
from key_world import ORG, OTHER_ORG, RUNTIME, World
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.versioned_store import versioned

SHIP, DRAFTY, THEIRS = "ag_ship000001", "ag_drafty0001", "ag_theirs0001"
PAPERS = "ag_papers0001"
PAPER_TYPES = ["pdf", "text"]
INPUT = {
    "type": "object",
    "required": ["key"],
    "properties": {"key": {"type": "string"}},
}
QUESTION = {
    "type": "object",
    "required": ["send"],
    "properties": {"send": {"type": "boolean"}},
}


def workflow(agent_id: str, name: str, **start: Any) -> dict[str, Any]:
    nodes = [
        {
            "id": "start",
            "kind": "start",
            "name": "Start",
            "config": {"input_schema": INPUT, **start},
            "outputs": ["next"],
        },
        {
            "id": "done",
            "kind": "end",
            "name": "Done",
            "config": {"outcome": "succeeded", "result": ""},
            "outputs": [],
        },
    ]
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": name,
        "description": f"{name}, for tests.",
        "nodes": nodes,
        "edges": [
            {"id": "e", "source": "start", "source_output": "next", "target": "done"}
        ],
    }


class FakeEmbedding:
    """The async worker's queue: the runs a job was queued for."""

    def __init__(self) -> None:
        self.queued: list[str] = []

    async def run_adk(self, run_id: str) -> None:
        self.queued.append(run_id)

    async def aclose(self) -> None:
        pass


class Workflows:
    """The organizations' workflows: Ship published (v1) with a draft,
    Drafty never published, and Theirs (another organization's) published."""

    def __init__(self) -> None:
        self.records = {
            SHIP: self.record(SHIP, ORG, workflow(SHIP, "Ship v2"), published=1),
            DRAFTY: self.record(
                DRAFTY, ORG, workflow(DRAFTY, "Drafty"), published=None
            ),
            THEIRS: self.record(THEIRS, OTHER_ORG, None, published=1),
            PAPERS: self.record(PAPERS, ORG, None, published=1),
        }
        self.versions = {
            (SHIP, 1): {**workflow(SHIP, "Ship"), "version": 1},
            (THEIRS, 1): {**workflow(THEIRS, "Theirs"), "version": 1},
            (PAPERS, 1): {
                **workflow(PAPERS, "Papers", allow_files=True, file_types=PAPER_TYPES),
                "version": 1,
            },
        }

    @staticmethod
    def record(
        agent_id: str, org: str, draft: Any, *, published: int | None
    ) -> dict[str, Any]:
        return {
            "_id": agent_id,
            "organization_id": org,
            "revision": 4,
            "document": draft or workflow(agent_id, agent_id),
            "has_draft": draft is not None,
            "draft_version": (published or 0) + 1 if draft is not None else None,
            "published_version": published,
            "published_at": None,
        }

    async def aclose(self) -> None:
        pass

    async def get(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
        found = self.records.get(agent_id)
        return (
            versioned(found)
            if found and found["organization_id"] == organization_id
            else None
        )

    async def find(self, agent_id: str) -> dict[str, Any] | None:
        return versioned(self.records.get(agent_id))

    async def version(
        self, organization_id: str, agent_id: str, number: int
    ) -> dict[str, Any] | None:
        found = self.versions.get((agent_id, number))
        return {"document": found} if found else None


class Runtime:
    def __init__(self, world: World) -> None:
        self.world = world
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

        async def tables() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)

        self.call(tables)
        self.runs = RunStore(engine)
        self.queue = FakeEmbedding()
        self.artifacts = InMemoryArtifactService()
        state = world.client.app.state  # type: ignore[attr-defined]
        state.adk_runs = self.runs
        state.embedding = self.queue
        state.organization_agents = Workflows()
        state.workflow_artifacts = self.artifacts

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        assert self.world.client.portal is not None
        return self.world.client.portal.call(lambda: fn(*args, **kwargs))

    def send(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        user: str | None = None,
        key: str | None = None,
    ) -> Any:
        return self.world.call(
            method, f"/workflows{path}", user, body, key=key, prefix=RUNTIME
        )

    def start(
        self,
        ref: str = SHIP,
        *,
        user: str | None = None,
        key: str | None = None,
        **body: Any,
    ) -> Any:
        return self.send(
            "POST", f"/{ref}/runs", {"input": {"key": "a1"}, **body}, user=user, key=key
        )

    def paused(self, run_id: str, kind: str, **details: Any) -> str:
        """Put a run at a pause, as the worker would: the pause's ID."""
        assert self.call(self.runs.claim, run_id, owner="w1", lease_seconds=60)
        pause = self.call(
            self.runs.pause,
            run_id,
            owner="w1",
            state={},
            key="review",
            kind=kind,
            reason="Go on?",
            details={"kind": kind, "step": "review", "step_name": "Review", **details},
            deadline=None,
        )
        return str(pause["id"])
