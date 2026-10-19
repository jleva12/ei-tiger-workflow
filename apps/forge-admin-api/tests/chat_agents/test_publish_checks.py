"""What publishing a chat agent checks: a published version runs only
what's published, never a workflow's draft."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from forge_admin.api.routes.chat_agents import check_builds, workflow_drafts

EXAMPLE = (
    Path(__file__).parents[4]
    / "packages/python/agent-runtime/tests/fixtures/support-assistant.chat-agent.json"
)


def document(version: Any) -> dict[str, Any]:
    """The example agent, offline, with a Workflow tool at a version."""
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] == "agent"]
    doc["nodes"].append(
        {
            "id": "refund",
            "kind": "adk_workflow",
            "name": "Refund",
            "config": {"workflow": "ag_refund00001", "version": version},
        }
    )
    doc["edges"] = [
        {"id": "e", "source": "agent", "source_output": "tools", "target": "refund"}
    ]
    return doc


def request() -> Any:
    """A request to an API without chat agents' runtime: only the document is checked."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def test_a_workflows_draft_isnt_published() -> None:
    assert workflow_drafts(document("draft")) == [
        "'Refund' runs ag_refund00001's draft: pick one of its published versions"
    ]
    assert workflow_drafts(document(3)) == []
    assert workflow_drafts(document(None)) == []
    with pytest.raises(HTTPException) as refused:
        asyncio.run(check_builds(request(), document("draft"), publishing=True))
    assert refused.value.status_code == 422
    assert "draft" in str(refused.value.detail)
    # A draft may run one while it's edited.
    asyncio.run(check_builds(request(), document("draft")))
    asyncio.run(check_builds(request(), document(3), publishing=True))
