"""The kinds of node the builders offer: a workflow's and the Agents canvas's."""

import pytest
from fastapi.testclient import TestClient
from forge_agent_runtime.document import KINDS
from forge_task_adk_workflows.graph import FACTORIES

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.config import Settings

WORKFLOW = "/api/v1/workflow-node-kinds"
CHAT = "/api/v1/chat-agent-node-kinds"


def test_the_workflow_catalog_lists_every_kind_the_worker_builds(
    settings: Settings,
) -> None:
    signed_in = settings.model_copy(update={"local_user_id": "member-1"})
    server = ApiServer(signed_in, routers=ROUTERS, public_routers=PUBLIC_ROUTERS)
    with TestClient(server.create_app()) as client:
        response = client.get(WORKFLOW)
    assert response.status_code == 200
    catalog = response.json()
    assert [kind["kind"] for kind in catalog["kinds"]] == list(FACTORIES)
    assert [group["id"] for group in catalog["groups"]] == [
        "start",
        "agents",
        "people",
        "actions",
        "github",
        "logic",
        "finish",
    ]
    switch = next(kind for kind in catalog["kinds"] if kind["kind"] == "switch")
    assert switch["rows"] == {
        "setting": "cases",
        "label": "value",
        "empty": "Empty value",
        "ways": None,
    }
    # A board's choice has two ways out of its own.
    board = next(kind for kind in catalog["kinds"] if kind["kind"] == "decision_board")
    assert [way["id"] for way in board["rows"]["ways"]] == ["accepted", "rejected"]
    assert board["person"] is True
    assert switch["outputs"] == [
        {
            "id": "default",
            "label": "Default",
            "branch": True,
            "fallback": True,
            "body": False,
        }
    ]
    assert switch["defaults"]["cases"] == [
        {"id": "case_1", "value": ""},
        {"id": "case_2", "value": ""},
    ]


def test_the_chat_agent_catalog_lists_every_kind_the_runtime_builds(
    settings: Settings,
) -> None:
    signed_in = settings.model_copy(update={"local_user_id": "member-1"})
    server = ApiServer(signed_in, routers=ROUTERS, public_routers=PUBLIC_ROUTERS)
    with TestClient(server.create_app()) as client:
        response = client.get(CHAT)
    assert response.status_code == 200
    catalog = response.json()
    assert {kind["kind"] for kind in catalog["kinds"]} == KINDS
    assert [group["id"] for group in catalog["groups"]] == ["agent", "agents", "tools"]
    by_kind = {kind["kind"]: kind for kind in catalog["kinds"]}
    assert [way["id"] for way in by_kind["agent"]["outputs"]] == ["tools", "agents"]
    assert by_kind["mcp"]["outputs"] == []
    assert by_kind["saved_agent"]["handed_to"] is True
    assert by_kind["memory"]["workflow_tool"] is False
    # An intake agent is handed off to, or used as a tool (its requests).
    intake = by_kind["intake_agent"]
    assert intake["handed_to"] is True and intake.get("tool", True) is True
    assert [way["id"] for way in intake["outputs"]] == ["tools", "on_complete"]


@pytest.mark.parametrize("path", [WORKFLOW, CHAT])
def test_no_one_signed_in_is_refused(settings: Settings, path: str) -> None:
    server = ApiServer(settings, routers=ROUTERS, public_routers=PUBLIC_ROUTERS)
    with TestClient(server.create_app()) as client:
        assert client.get(path).status_code == 401
