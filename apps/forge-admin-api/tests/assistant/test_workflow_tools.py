"""The assistant's workflow builder tools, against a stand-in for the admin
API: what each sends (as whom, where, with what), and what the model gets
back."""

import asyncio
import copy
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr
from test_admin_tools import Api, Handler, context
from test_workflow_edits import ORG, workflow

from forge_admin.api.routes.adk_workflows import AgentUpdate
from forge_admin.assistant.workflow_tools import (
    CONFIRMED,
    TOOL_NAMES,
    WorkflowToolset,
    describe_kinds,
)
from forge_admin.config import Settings

WORKFLOW = "ag_test000001"
BASE = f"/api/v1/organizations/{ORG}/agents/{WORKFLOW}"


@pytest.fixture
def signing(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 32), "api_key": SecretStr("k" * 32)}
    )


def record(revision: int = 4, *, has_draft: bool = True) -> dict[str, Any]:
    return {
        "id": WORKFLOW,
        "organization_id": ORG,
        "revision": revision,
        "document": workflow(),
        "status": "published+draft" if has_draft else "published",
        "has_draft": has_draft,
        "draft_version": 2 if has_draft else None,
        "published_version": 1,
    }


class Workflows:
    """The workflow routes, as the API answers them, saving drafts."""

    def __init__(self, *, has_draft: bool = True) -> None:
        self.current = record(has_draft=has_draft)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check"):
            return httpx.Response(
                200,
                json={
                    "revision": self.current["revision"],
                    "problems": [],
                    "publish_problems": [],
                },
            )
        if path.endswith("/versions") and request.method == "POST":
            self.current = {**record(self.current["revision"] + 1), "draft_version": 2}
            return httpx.Response(200, json=self.current)
        if path.endswith("/draft") and request.method == "PUT":
            body = AgentUpdate.model_validate_json(request.content)
            if body.revision != self.current["revision"]:
                return httpx.Response(409, json={"detail": "It was saved since."})
            self.current = {
                **self.current,
                "revision": self.current["revision"] + 1,
                "document": body.document,
            }
            return httpx.Response(200, json=self.current)
        if path.endswith(WORKFLOW):
            return httpx.Response(200, json=self.current)
        return httpx.Response(404, json={"detail": "Not found"})


def tools(signing: Settings, handler: Handler) -> tuple[WorkflowToolset, Api]:
    api = Api(signing, handler)
    return WorkflowToolset(api.person_api), api


def call(toolset: WorkflowToolset, tool_name: str, /, **args: Any) -> Any:
    async def main() -> Any:
        found = {tool.name: tool for tool in await toolset.get_tools()}
        return await found[tool_name].run_async(args=args, tool_context=context("ada"))

    return asyncio.run(main())


def test_it_offers_its_tools_and_asks_first_only_for_a_test_run(
    signing: Settings,
) -> None:
    toolset, _ = tools(signing, Workflows())

    async def main() -> dict[str, Any]:
        return {tool.name: tool for tool in await toolset.get_tools()}

    found = asyncio.run(main())
    assert set(found) == TOOL_NAMES
    asking = {
        name
        for name, tool in found.items()
        if getattr(tool, "_require_confirmation", False)
    }
    assert asking == CONFIRMED == {"test_workflow"}


def test_a_workflow_reads_as_its_nodes_ways_connections_and_problems(
    signing: Settings,
) -> None:
    toolset, api = tools(signing, Workflows())

    found = call(toolset, "get_workflow", organization_id=ORG, workflow_id=WORKFLOW)

    assert found["status"] == "success"
    view = found["payload"]
    assert [call.url.path for call in api.calls] == [BASE, f"{BASE}/check"]
    assert view["revision"] == 4
    assert view["editing"] == "its draft (version 2)"
    assert [(node["id"], node["ways_out"]) for node in view["nodes"]] == [
        ("start", ["next"]),
        ("done", []),
    ]
    # Empty settings say nothing.
    assert view["nodes"][0]["settings"] == {
        "allow_files": False,
        "allow_file_system": False,
        "start_over": False,
    }
    assert view["connections"] == [
        {"from": "start (Start)", "way": "next", "to": "done (Done)"}
    ]
    assert view["problems"] == []


def test_an_edit_saves_the_draft_from_the_revision_read_and_says_what_it_did(
    signing: Settings,
) -> None:
    routes = Workflows()
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "edit_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        revision=4,
        changes=[
            {"action": "disconnect", "source": "start", "target": "done"},
            {
                "action": "add_node",
                "kind": "transform",
                "id": "shape",
                "name": "Shape it",
                "settings": {"expression": '{"total": input.amount}'},
                "after": {"node": "start"},
            },
            {"action": "connect", "source": "shape", "target": "done"},
        ],
    )

    assert found["status"] == "success", found
    answer = found["payload"]
    # The builder reloads the draft by these.
    assert (answer["workflow_id"], answer["organization_id"], answer["revision"]) == (
        WORKFLOW,
        ORG,
        5,
    )
    assert answer["added"] == ["shape"]
    assert answer["problems"] == []
    put = next(call for call in api.calls if call.method == "PUT")
    assert put.url.path == f"{BASE}/draft"
    assert api.body(api.calls.index(put))["revision"] == 4
    saved = routes.current["document"]
    assert [node["id"] for node in saved["nodes"]] == ["start", "done", "shape"]


def test_an_edit_made_from_an_old_revision_is_refused(signing: Settings) -> None:
    toolset, api = tools(signing, Workflows())

    found = call(
        toolset,
        "edit_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        revision=2,
        changes=[{"action": "update_workflow", "name": "X"}],
    )

    assert found["status"] == "failed"
    assert "is at revision 4 now, not 2" in found["reason"]
    assert all(call.method == "GET" for call in api.calls)


def test_an_edit_that_cant_be_made_saves_nothing_and_says_how_to_go_on(
    signing: Settings,
) -> None:
    toolset, api = tools(signing, Workflows())

    found = call(
        toolset,
        "edit_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        changes=[{"action": "remove_node", "id": "start"}],
    )

    assert found["status"] == "failed"
    assert (
        found["reason"]
        == "Change 1 (remove_node): the start can't be removed: every workflow has one."
    )
    assert found["suggested_fixes"][0].startswith("Fix that change and call again")
    assert all(call.method == "GET" for call in api.calls)


def test_a_published_workflow_without_a_draft_gets_one_first(signing: Settings) -> None:
    routes = Workflows(has_draft=False)
    toolset, api = tools(signing, routes)

    found = call(
        toolset,
        "edit_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        changes=[{"action": "update_workflow", "description": "Handles refunds."}],
    )

    assert found["status"] == "success", found
    assert [(call.method, call.url.path) for call in api.calls][:3] == [
        ("GET", BASE),
        ("POST", f"{BASE}/versions"),
        ("PUT", f"{BASE}/draft"),
    ]
    assert found["payload"]["started_draft"].startswith(
        "It was published with no draft"
    )
    assert routes.current["document"]["description"] == "Handles refunds."


def test_a_test_run_runs_the_draft_and_reports_each_step_it_reached(
    signing: Settings,
) -> None:
    run_id = "0" * 31 + "1"
    steps = {
        "steps": [
            {
                "id": "start",
                "name": "Start",
                "kind": "start",
                "status": "done",
                "output": {"order": "A1"},
            },
            {
                "id": "fetch",
                "name": "Fetch",
                "kind": "http",
                "status": "failed",
                "called_with": {
                    "method": "GET",
                    "url": "https://api.example.com/orders/A1",
                },
                "error": {
                    "message": "Fetch (fetch) failed: GET … answered 404 Not Found",
                    "code": "RunFailed",
                },
                "calls": [],
            },
            {"id": "done", "name": "Done", "kind": "end", "status": "not_reached"},
        ]
    }

    def routes(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            return httpx.Response(201, json={"id": run_id, "status": "queued"})
        if path.endswith("/steps"):
            return httpx.Response(200, json=steps)
        return httpx.Response(
            200,
            json={
                "id": run_id,
                "status": "failed",
                "error": {"message": "Fetch failed"},
                "input": {},
            },
        )

    toolset, api = tools(signing, routes)
    toolset_without_asking = WorkflowToolset(api.person_api, confirm_changes=False)

    found = call(
        toolset_without_asking,
        "test_workflow",
        organization_id=ORG,
        workflow_id=WORKFLOW,
        input={"order": "A1"},
        wait_seconds=5,
    )

    assert found["status"] == "success", found
    assert api.body(0) == {"input": {"order": "A1"}, "version": "draft"}
    view = found["payload"]
    assert view["status"] == "failed"
    assert [step["id"] for step in view["steps"]] == ["start", "fetch"]
    assert view["steps"][1]["called_with"]["url"] == "https://api.example.com/orders/A1"
    assert "answered 404" in view["steps"][1]["error"]["message"]


def test_node_kinds_are_listed_then_described_in_full() -> None:
    listed = describe_kinds(None, None)
    kinds = {kind["kind"]: kind for kind in listed["node_kinds"]}
    assert {"start", "llm", "http", "saved", "if", "switch", "end"} <= set(kinds)
    assert "Hands on" in kinds["http"]["does"]
    assert kinds["switch"]["ways_out"][-1] == "one per cases row, before these"
    assert {kind["kind"] for kind in listed["llm_tool_kinds"]} >= {
        "mcp",
        "http_tool",
        "knowledge_base",
    }

    full = describe_kinds(["llm", "nope"], ["http_tool"])
    llm = full["llm"]
    assert "instruction" in llm["settings"]["properties"]
    # What it refers to comes along.
    assert {"sub_agent", "tool"} <= set(llm["settings"]["$defs"])
    assert llm["new_node_settings"]["mode"]
    assert "unknown" in full["nope"]
    assert "url" in full["tool:http_tool"]["settings"]["properties"]


def test_building_blocks_say_what_the_person_may_not_read(signing: Settings) -> None:
    def routes(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/credentials"):
            return httpx.Response(403, json={"detail": "You need credentials:read."})
        if path.endswith("/chat-agents"):
            document = copy.deepcopy(workflow())
            document["nodes"][0]["config"]["state_schema"] = {
                "type": "object",
                "properties": {"tier": {"type": "string"}},
            }
            return httpx.Response(
                200,
                json=[
                    {"id": "ca_support01", "document": document, "status": "published"}
                ],
            )
        if path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "defaultModel": "openai/gpt-5",
                    "models": [
                        {"provider": "openai", "model": "gpt-5", "name": "GPT-5"}
                    ],
                },
            )
        return httpx.Response(200, json=[])

    toolset, _ = tools(signing, routes)

    found = call(toolset, "list_building_blocks", organization_id=ORG)["payload"]

    assert found["credentials"] == {"unavailable": "You need credentials:read."}
    assert found["agents"][0]["inputs"]["properties"] == {"tier": {"type": "string"}}
    assert found["models"]["choices"] == [
        {"provider": "openai", "name": "gpt-5", "label": "GPT-5"}
    ]
    assert found["workflows"] == [] and found["skills"] == []
