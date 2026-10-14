"""The changes the workflow assistant makes to a workflow's document
(``workflow_edits.apply_changes``): each made as asked, the document kept in
Forge's format, and a change that can't be made refusing the whole batch,
saying why."""

from typing import Any

import pytest

from forge_admin.adk_workflows.documents import schema_problems
from forge_admin.assistant.workflow_edits import EditRefused, apply_changes

ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"


def workflow() -> dict[str, Any]:
    """A workflow with a start that leads to an End."""
    return {
        "format": "forge.agent/v1",
        "id": "ag_test000001",
        "name": "Refunds",
        "description": "",
        "organization_id": ORG,
        "nodes": [
            {
                "id": "start",
                "kind": "start",
                "name": "Start",
                "config": {
                    "input_schema": {},
                    "allow_files": False,
                    "file_types": [],
                    "allow_file_system": False,
                    "start_over": False,
                },
                "outputs": ["next"],
            },
            {
                "id": "done",
                "kind": "end",
                "name": "Done",
                "config": {"outcome": "succeeded", "result": "previous"},
                "outputs": [],
            },
        ],
        "edges": [
            {
                "id": "start:next->done",
                "source": "start",
                "source_output": "next",
                "target": "done",
            }
        ],
        "layout": {"start": {"x": 0, "y": 0}, "done": {"x": 320, "y": 0}},
    }


def test_nodes_are_added_with_their_kinds_defaults_and_connected() -> None:
    edited = apply_changes(
        workflow(),
        [
            {"action": "disconnect", "source": "start", "target": "done"},
            {
                "action": "add_node",
                "kind": "http",
                "id": "fetch_order",
                "name": "Look up the order",
                "settings": {"url": "https://api.example.com/orders/{{ input.order }}"},
                "after": {"node": "start", "way": "next"},
            },
            {
                "action": "connect",
                "source": "fetch_order",
                "way": "success",
                "target": "done",
            },
        ],
    )
    doc = edited.document
    added = next(node for node in doc["nodes"] if node["id"] == "fetch_order")
    # Its kind's defaults, under what was given.
    assert added["config"]["method"] == "GET"
    assert added["config"]["url"] == "https://api.example.com/orders/{{ input.order }}"
    assert added["outputs"] == ["success", "error"]
    assert {(e["source"], e["source_output"], e["target"]) for e in doc["edges"]} == {
        ("start", "next", "fetch_order"),
        ("fetch_order", "success", "done"),
    }
    assert edited.added == ["fetch_order"]
    assert edited.done[0] == "Disconnected Start (start) by next to Done (done)"
    assert edited.done[1].startswith(
        "Added Look up the order (fetch_order); Connected Start"
    )
    # Placed right of what leads to it, clear of Done.
    assert doc["layout"]["fetch_order"] == {"x": 320, "y": 160}
    assert schema_problems(doc) == []


def test_a_node_left_without_an_id_gets_one_from_its_kind() -> None:
    edited = apply_changes(
        workflow(),
        [{"action": "add_node", "kind": "transform", "name": "Shape it"}],
    )
    assert edited.added == ["transform_1"]


def test_settings_replace_whole_and_ways_out_follow_them() -> None:
    doc = apply_changes(
        workflow(),
        [
            {
                "action": "add_node",
                "kind": "switch",
                "id": "route",
                "name": "Route",
                "settings": {
                    "value": "input.kind",
                    "cases": [{"id": "bug", "value": "bug"}],
                },
            },
            {"action": "connect", "source": "route", "way": "bug", "target": "done"},
        ],
    ).document
    # Its cases replaced whole: the bug case's way is gone, and its line too.
    edited = apply_changes(
        doc,
        [
            {
                "action": "update_node",
                "id": "route",
                "settings": {"cases": [{"id": "billing", "value": "billing"}]},
            }
        ],
    )
    route = next(node for node in edited.document["nodes"] if node["id"] == "route")
    assert route["outputs"] == ["billing", "default"]
    assert edited.dropped == ["Route (route) by bug to Done (done)"]
    assert route["config"]["value"] == "input.kind"


def test_removing_a_node_takes_its_connections_and_place() -> None:
    doc = apply_changes(
        workflow(),
        [
            {
                "action": "add_node",
                "kind": "transform",
                "id": "shape",
                "name": "Shape",
                "after": {"node": "start"},
            }
        ],
    ).document
    edited = apply_changes(doc, [{"action": "remove_node", "id": "shape"}])
    assert [node["id"] for node in edited.document["nodes"]] == ["start", "done"]
    assert all(
        "shape" not in (e["source"], e["target"]) for e in edited.document["edges"]
    )
    assert "shape" not in edited.document["layout"]
    assert edited.done == ["Removed Shape (shape) and its 1 connection(s)"]


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"action": "teleport"}, "its action must be one of"),
        (
            {"action": "add_node", "kind": "slack", "name": "Ping"},
            "there's no node kind 'slack'",
        ),
        (
            {"action": "add_node", "kind": "start", "name": "Again"},
            "has a start already",
        ),
        (
            {"action": "add_node", "kind": "transform", "name": "Done"},
            "is called that already",
        ),
        (
            {"action": "add_node", "kind": "transform", "name": "X", "id": "Bad-Id"},
            "isn't one",
        ),
        (
            {"action": "add_node", "kind": "transform", "name": "X", "id": "done"},
            "has a node 'done'",
        ),
        (
            {
                "action": "add_node",
                "kind": "http",
                "name": "X",
                "settings": {"uri": "x"},
            },
            "a http node has no setting 'uri'; its settings are body, credential",
        ),
        (
            {"action": "update_node", "id": "nope", "name": "X"},
            "has no node 'nope'; its nodes are start (Start), done (Done)",
        ),
        ({"action": "remove_node", "id": "start"}, "the start can't be removed"),
        (
            {"action": "connect", "source": "done", "target": "start"},
            "nothing leads into the start",
        ),
        (
            {"action": "connect", "source": "done", "target": "done"},
            "can't lead to itself",
        ),
        (
            {"action": "connect", "source": "start", "way": "yes", "target": "done"},
            "has no way out 'yes': its ways out are next",
        ),
        (
            {"action": "disconnect", "source": "done"},
            "no connection of Done (done) matches it",
        ),
    ],
)
def test_a_change_that_cant_be_made_refuses_the_batch_saying_why(
    change: dict[str, Any], said: str
) -> None:
    before = workflow()
    with pytest.raises(EditRefused) as refused:
        apply_changes(
            before, [{"action": "update_workflow", "name": "Renamed"}, change]
        )
    assert str(refused.value).startswith("Change 2")
    assert said in str(refused.value)
    assert before == workflow()  # never touched


def test_settings_that_dont_fit_the_format_are_named_by_node() -> None:
    with pytest.raises(EditRefused) as refused:
        apply_changes(
            workflow(),
            [
                {
                    "action": "add_node",
                    "kind": "http",
                    "id": "fetch",
                    "name": "Fetch",
                    "settings": {"method": "FETCH", "retries": "two"},
                }
            ],
        )
    said = str(refused.value)
    assert said.startswith(
        "The workflow these changes make doesn't fit Forge's format: "
    )
    assert "node fetch settings/method" in said
    assert "node fetch settings/retries" in said


def test_a_way_out_must_be_named_when_there_are_several() -> None:
    with pytest.raises(EditRefused) as refused:
        apply_changes(
            workflow(),
            [
                {
                    "action": "add_node",
                    "kind": "if",
                    "id": "check",
                    "name": "Check",
                    "settings": {"condition": "true"},
                },
                {"action": "connect", "source": "check", "target": "done"},
            ],
        )
    assert "say which way out of Check (check) leads there: one of true, false" in str(
        refused.value
    )


def test_the_workflow_is_renamed_and_described() -> None:
    edited = apply_changes(
        workflow(),
        [
            {
                "action": "update_workflow",
                "name": "Refund requests",
                "description": "Handles refunds.",
            }
        ],
    )
    assert (edited.document["name"], edited.document["description"]) == (
        "Refund requests",
        "Handles refunds.",
    )
    with pytest.raises(EditRefused, match="There are no changes"):
        apply_changes(workflow(), [])
