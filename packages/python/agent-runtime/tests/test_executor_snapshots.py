"""Authorization and execution share one graph even while agents are published."""

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent

from forge_agent_runtime import AgentExecutor, AgentRef, BuildError, ResolvedAgent, parse_document
from forge_agent_runtime.a2a import create_a2a_router
from forge_agent_runtime.server import create_router
from tests.conftest import text

PARENT, CHILD, SECRET = "ca_parent0001", "ca_child00001", "ca_secret001"


def document(agent_id: str, *, child: str | None = None, version: int = 1) -> Any:
    nodes = [
        {"id": "root", "kind": "agent", "name": agent_id, "config": {"instruction": f"Version {version}."}}
    ]
    edges = []
    if child is not None:
        nodes.append(
            {"id": "saved", "kind": "saved_agent", "name": f"Uses {child}", "config": {"agent": child}}
        )
        edges.append({"id": "edge", "source": "root", "source_output": "agents", "target": "saved"})
    return parse_document(
        {
            "format": "forge.chat_agent/v1",
            "id": agent_id,
            "name": agent_id,
            "organization_id": "org_test",
            "version": version,
            "nodes": nodes,
            "edges": edges,
        }
    )


class ChangingSource:
    def __init__(self) -> None:
        self.documents = {
            PARENT: document(PARENT, child=CHILD),
            CHILD: document(CHILD, child=SECRET),
            SECRET: document(SECRET),
        }
        self.stamps: dict[str, str] = {}

    async def get(self, ref: AgentRef) -> ResolvedAgent:
        doc = self.documents[ref.agent_id]
        return ResolvedAgent(doc, doc.version, f"{doc.id}@{doc.version}:{self.stamps.get(doc.id, '')}")


async def test_dependency_publication_and_transitive_resource_stamps_invalidate_runners(models, web):
    source = ChangingSource()
    executor = AgentExecutor(source, models=models, http=web())
    try:
        before, graph = await executor.runner(PARENT)
        assert graph.used_agent_ids == {CHILD, SECRET}
        assert cast(LlmAgent, before.agent).sub_agents[0].sub_agents[0].name == f"uses_{SECRET}"
        same, _ = await executor.runner(PARENT)
        assert same is before

        source.stamps[SECRET] = "skills:changed"
        skill_update, _ = await executor.runner(PARENT)
        assert skill_update is not before

        source.documents[CHILD] = document(CHILD, version=2)
        after, graph = await executor.runner(PARENT)
        assert after is not skill_update
        assert graph.used_agent_ids == {CHILD}
        assert cast(LlmAgent, after.agent).sub_agents[0].sub_agents == []
    finally:
        await executor.close()


async def test_build_uses_captured_documents_even_when_source_is_mutated_in_place(models, web):
    source = ChangingSource()
    executor = AgentExecutor(source, models=models, http=web())
    try:
        graph = await executor.resolve(PARENT)
        source.documents[CHILD].nodes.clear()
        source.documents[SECRET].name = "Replaced"
        built, _ = await executor.runner(PARENT, resolved=graph)
        assert cast(LlmAgent, built.agent).sub_agents[0].sub_agents[0].name == f"uses_{SECRET}"
        assert graph.used_agent_ids == {CHILD, SECRET}
    finally:
        await executor.close()


async def test_embedded_dependencies_are_checked_instead_of_latest_and_stay_tenant_scoped(models, web):
    source = ChangingSource()
    source.documents[CHILD] = document(CHILD)
    source.documents[PARENT].dependencies = {
        f"{CHILD}@1": document(CHILD, child=SECRET).to_json(),
        f"{SECRET}@1": document(SECRET).to_json(),
    }
    executor = AgentExecutor(source, models=models, http=web())
    try:
        graph = await executor.resolve(PARENT)
        assert graph.used_agent_ids == {CHILD, SECRET}
        source.documents[PARENT].dependencies[f"{SECRET}@1"]["organization_id"] = "org_other"
        with pytest.raises(BuildError, match="organization"):
            await executor.resolve(PARENT)
    finally:
        await executor.close()


@pytest.mark.parametrize("protocol", ["sse", "a2a"])
def test_routes_execute_the_authorized_snapshot_after_publication(protocol, models, web, llm, monkeypatch):
    source = ChangingSource()
    source.documents[CHILD] = document(CHILD)
    executor = AgentExecutor(source, models=models, http=web())
    authorized: list[ResolvedAgent] = []
    executed: list[ResolvedAgent | None] = []
    original_run = executor.run

    async def authorize(request, agent, *, action, user_id):
        if action in ("run", "a2a"):
            authorized.append(agent)
            source.documents[CHILD] = document(CHILD, child=SECRET, version=2)

    async def run(request, *, resolved=None, lease=None):
        executed.append(resolved)
        async for event in original_run(request, resolved=resolved, lease=lease):
            yield event

    monkeypatch.setattr(executor, "run", run)
    llm.turns = [[text("Done.")]]
    app = FastAPI()
    app.include_router(create_router(executor, authorize=authorize))
    app.include_router(create_a2a_router(executor, path="/a2a", authorize=authorize))
    with TestClient(app) as client:
        if protocol == "sse":
            sid = client.post(f"/apps/{PARENT}/users/ada/sessions", json={}).json()["id"]
            response = client.post(
                "/run_sse",
                json={
                    "appName": PARENT,
                    "userId": "ada",
                    "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text": "Hi"}]},
                },
            )
        else:
            response = client.post(
                f"/a2a/{PARENT}",
                headers={"A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": "m1",
                            "role": "ROLE_USER",
                            "parts": [{"text": "Hi"}],
                            "contextId": "c1",
                        }
                    },
                },
            )
        assert response.status_code == 200, response.text
        assert "Done." in response.text
        assert len(executed) == 1
        assert executed[0] is authorized[0]
        assert authorized[0].used_agent_ids == {CHILD}
        assert client.portal is not None
        client.portal.call(executor.close)
