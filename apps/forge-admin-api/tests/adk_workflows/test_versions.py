"""Which version of a workflow runs, by reference, inside a run and from
outside; and what keeps a draft from being published."""

import asyncio
from typing import Any

import pytest

from forge_admin.adk_workflows.runs import check_publishable
from forge_admin.adk_workflows.versioned_store import versioned
from forge_admin.adk_workflows.versions import (
    WorkflowGone,
    WorkflowNotFound,
    resolve_workflow,
    workflow_finder,
)

ORG = "3f6c0000-0000-4000-8000-000000000001"


def node(node_id: str, kind: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": node_id.title(),
        "config": config,
        "outputs": ["next"],
    }


def document(agent_id: str, *nodes: dict[str, Any]) -> dict[str, Any]:
    """A workflow of a start, the nodes given, one after the other, and an end."""
    chain = [
        node("start", "start", {"input_schema": {}}),
        *nodes,
        node("done", "end", {"outcome": "succeeded", "result": ""}),
    ]
    return {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": f"Workflow {agent_id}",
        "nodes": chain,
        "edges": [
            {
                "id": f"{a['id']}->{b['id']}",
                "source": a["id"],
                "source_output": "next",
                "target": b["id"],
            }
            for a, b in zip(chain, chain[1:], strict=False)
        ],
    }


def saved(node_id: str, agent: str, version: Any = None) -> dict[str, Any]:
    return node(node_id, "saved", {"agent": agent, "version": version})


class Store:
    """The workflows: their records and published versions."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.published: dict[tuple[str, int], dict[str, Any]] = {}

    def add(
        self,
        agent_id: str,
        draft: dict[str, Any] | None,
        *published: dict[str, Any],
        legacy: bool = False,
    ) -> None:
        for number, doc in enumerate(published, start=1):
            self.published[(agent_id, number)] = {
                "document": {**doc, "version": number}
            }
        record: dict[str, Any] = {
            "_id": agent_id,
            "organization_id": ORG,
            "revision": 1,
            "document": draft if draft is not None else published[-1],
        }
        if not legacy:
            record |= {
                "has_draft": draft is not None,
                "draft_version": len(published) + 1 if draft is not None else None,
                "published_version": len(published) or None,
                "published_at": None,
            }
        self.records[agent_id] = record

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
        return self.published.get((agent_id, number))


def resolve(store: Store, ref: str, **options: Any) -> Any:
    return asyncio.run(resolve_workflow(store, ref, organization_id=ORG, **options))  # type: ignore[arg-type]


@pytest.fixture
def store() -> Store:
    found = Store()
    v1, v2 = document("ag_both000001"), document("ag_both000001")
    found.add("ag_both000001", document("ag_both000001"), v1, v2)
    found.add("ag_draft00001", document("ag_draft00001"))
    found.add("ag_legacy0001", document("ag_legacy0001"), legacy=True)
    found.add("ag_pub0000001", None, document("ag_pub0000001"))
    return found


def test_a_reference_without_a_version_is_the_latest_published(store: Store) -> None:
    both = resolve(store, "ag_both000001")
    assert (both.version, both.document["version"]) == (2, 2)
    assert both.ref == "ag_both000001@2"
    assert resolve(store, "ag_both000001@1").document["version"] == 1
    assert resolve(store, "ag_both000001@draft").version == "draft"
    assert resolve(store, "ag_pub0000001").version == 1


def test_never_published_runs_its_draft_inside_but_not_from_outside(
    store: Store,
) -> None:
    assert resolve(store, "ag_draft00001").version == "draft"
    # Saved before workflows had versions: the same.
    assert resolve(store, "ag_legacy0001").version == "draft"
    with pytest.raises(WorkflowNotFound, match="ag_draft00001@draft"):
        resolve(store, "ag_draft00001", outside=True)
    assert resolve(store, "ag_draft00001@draft", outside=True).version == "draft"


def test_versions_and_drafts_that_arent_are_refused(store: Store) -> None:
    with pytest.raises(WorkflowNotFound, match="no version 9"):
        resolve(store, "ag_both000001@9")
    with pytest.raises(WorkflowNotFound, match="no draft"):
        resolve(store, "ag_pub0000001@draft")
    with pytest.raises(WorkflowGone):
        resolve(store, "ag_nothing001")
    with pytest.raises(WorkflowNotFound):
        resolve(store, "ag_both000001@latest")


def test_a_run_finds_what_it_runs_or_leaves_the_build_to_say(store: Store) -> None:
    find = workflow_finder(store, ORG)  # type: ignore[arg-type]
    assert asyncio.run(find("ag_both000001"))["version"] == 2  # type: ignore[index]
    assert asyncio.run(find("ag_nothing001")) is None
    with pytest.raises(WorkflowNotFound):
        asyncio.run(find("ag_both000001@9"))


def test_a_published_version_runs_only_what_doesnt_change(store: Store) -> None:
    find = workflow_finder(store, ORG)  # type: ignore[arg-type]

    def problems(*nodes: dict[str, Any]) -> list[str]:
        return asyncio.run(check_publishable(document("ag_new00000001", *nodes), find))

    assert problems(saved("a", "ag_both000001"), saved("b", "ag_pub0000001", 1)) == []
    [draft] = problems(saved("a", "ag_both000001", "draft"))
    assert "ag_both000001's draft" in draft
    [unpublished] = problems(saved("a", "ag_draft00001"))
    assert "hasn't been published" in unpublished
    [missing] = problems(saved("a", "ag_both000001", 9))
    assert "no version 9" in missing
    no_start = asyncio.run(
        check_publishable(
            {
                "format": "forge.agent/v1",
                "id": "x",
                "name": "x",
                "nodes": [],
                "edges": [],
            },
            find,
        )
    )
    assert no_start == ["It has no start."]
