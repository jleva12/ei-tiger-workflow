"""
The workflows the scenarios run (forge.agent/v1 documents), and runs of them
as the admin API starts them (``RunStore.create`` with a ``RunPayload``).
Every effect they have goes to the mock server, by a name the test counts.
"""

from __future__ import annotations

import uuid
from typing import Any

from forge_task_adk_workflows.run_store import Actor, RunStore

ORGANIZATION = "org-it"
MEMBER = Actor("user-it", "Ida Tester")
DECIDER = Actor("user-decider", "Dee Cider")
CANCELLER = Actor("user-canceller", "Cass Canceller")

#: The way out a step that succeeded takes, by kind.
_WAYS = {"start": "next", "http": "success", "bash": "success", "llm": "next", "transform": "next", "delay": "next"}


def node(node_id: str, kind: str, config: dict[str, Any] | None = None, name: str = "") -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "name": name or node_id.replace("_", " ").title(),
        "config": config or {},
        "outputs": [],
    }


def start(*, allow_file_system: bool = False) -> dict[str, Any]:
    return node("start", "start", {"input_schema": {}, "allow_file_system": allow_file_system})


def document(
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str, str]],
    *,
    agent_id: str = "ag_it",
    name: str = "IT workflow",
    version: int | None = None,
) -> dict[str, Any]:
    """A forge.agent/v1 document: its nodes, and its edges as (source, way, target)."""
    found: dict[str, Any] = {
        "format": "forge.agent/v1",
        "id": agent_id,
        "name": name,
        "description": "",
        "organization_id": ORGANIZATION,
        "nodes": nodes,
        "edges": [{"id": f"{s}:{o}->{t}", "source": s, "source_output": o, "target": t} for s, o, t in edges],
        "layout": {},
    }
    if version is not None:
        found["version"] = version
    return found


def line(*steps: dict[str, Any], allow_file_system: bool = False, **more: Any) -> dict[str, Any]:
    """A workflow whose steps run one after another from its start, each down its success way."""
    nodes = [start(allow_file_system=allow_file_system), *steps]
    edges = [(a["id"], _WAYS.get(a["kind"], "next"), b["id"]) for a, b in zip(nodes, nodes[1:], strict=False)]
    return document(nodes, edges, **more)


def http(node_id: str, url: str, *, method: str = "POST", body: str = "", **config: Any) -> dict[str, Any]:
    return node(node_id, "http", {"method": method, "url": url, "body": body, "timeout_seconds": 300, **config})


def bash(node_id: str, command: str, **config: Any) -> dict[str, Any]:
    return node(node_id, "bash", {"command": command, "timeout_seconds": 300, **config})


def approval(node_id: str, message: str = "Ship it?") -> dict[str, Any]:
    return node(node_id, "approval", {"message": message, "approvers": "org:member", "timeout_hours": 0})


def end(node_id: str, result: str = "", outcome: str = "succeeded") -> dict[str, Any]:
    return node(node_id, "end", {"outcome": outcome, "result": result})


def llm(node_id: str, instruction: str, *, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """An inline LLM agent, on the worker's default model (the mock's)."""
    return node(
        node_id,
        "llm",
        {
            "description": "",
            "instruction": instruction,
            "model": {"provider": "", "name": ""},
            "thinking_level": "",
            "output_schema": {},
            "include_contents": "default",
            "disallow_transfer_to_parent": False,
            "disallow_transfer_to_peers": False,
            "max_output_tokens": None,
            "sub_agents": [],
            "tools": tools or [],
            "mode": "single_turn",
        },
    )


def workflow_tool(workflow: str, version: int, name: str = "Child") -> dict[str, Any]:
    """An LLM agent's tool: a saved workflow (``workflow@version`` in the run's ``saved``), run as a run of its own."""
    return {
        "id": f"tool_{workflow}",
        "kind": "adk_workflow",
        "name": name,
        "config": {"workflow": workflow, "version": version},
    }


async def start_run(
    store: RunStore,
    workflow: dict[str, Any],
    value: Any = None,
    *,
    saved: dict[str, dict[str, Any]] | None = None,
    run_as: Actor = MEMBER,
) -> dict[str, Any]:
    """A run, queued, as the admin API starts one (its job is the caller's to queue)."""
    session_id = str(uuid.uuid4())
    payload = {
        "tenant_id": ORGANIZATION,
        "agent_id": workflow["id"],
        "revision": 1,
        "version": "draft",
        "name": workflow["name"],
        "document": workflow,
        "saved": saved or {},
        "input": value if value is not None else {},
        "session_id": session_id,
        "run_as": run_as.id,
        "run_as_name": run_as.name,
        "trigger": {"type": "manual"},
    }
    return await store.create(
        organization_id=ORGANIZATION,
        agent_id=workflow["id"],
        agent_name=workflow["name"],
        revision=1,
        session_id=session_id,
        payload=payload,
        requested_by=run_as,
        version="draft",
    )
