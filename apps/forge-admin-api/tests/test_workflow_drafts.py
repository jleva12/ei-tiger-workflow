"""Workflow drafts: made whole from the step catalog, then checked as the
workflows routes, the runner and the builder check a workflow."""

import json
from typing import Any

from forge_admin.workflow_drafts import CATALOG_PATH, catalog, complete, problems
from forge_admin.workflows import SCHEMA_PATH

ORGANIZATION = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e03"


def draft(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **over: Any
) -> dict:
    return {"name": "Triage", "nodes": nodes, "edges": edges, **over}


START = {
    "id": "start",
    "kind": "entry",
    "config": {
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "required": ["kind"],
        }
    },
}


def checked(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict:
    return problems(complete(draft(nodes, edges), organization_id=ORGANIZATION))


def test_the_catalog_has_every_kind_the_format_has() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    kinds = {
        name.removeprefix("config_")
        for name in schema["$defs"]
        if name.startswith("config_")
    }
    assert set(catalog()["kinds"]) == kinds
    for kind, spec in catalog()["kinds"].items():
        # Its defaults are the settings the format requires, no more.
        assert set(spec["defaults"]) == set(
            schema["$defs"][f"config_{kind}"]["required"]
        )
    assert CATALOG_PATH.name == "workflow.catalog.json"


def test_a_draft_says_only_what_differs_and_is_made_whole() -> None:
    made = complete(
        draft(
            [
                START,
                {
                    "id": "route",
                    "kind": "switch",
                    "config": {
                        "value": "input.kind",
                        "cases": [{"value": "bug"}, {"id": "q", "value": "question"}],
                    },
                },
                {
                    "id": "review",
                    "kind": "approval",
                    "config": {"message": "Approve {{ input.kind }}?"},
                },
            ],
            [
                {"source": "start", "target": "route"},
                {"source": "route", "output": "case_1", "target": "review"},
            ],
        ),
        organization_id=ORGANIZATION,
    )
    nodes = {node["id"]: node for node in made["nodes"]}
    assert made["format"] == "forge.workflow/v1" and made["entry"] == "start"
    assert made["organization_id"] == ORGANIZATION and made["layout"] == {}
    # Its kind's defaults under what it set, and its ways out.
    assert nodes["review"]["config"] == {
        "message": "Approve {{ input.kind }}?",
        "approvers": "org:admin",
        "timeout_hours": 0,
    }
    assert nodes["review"]["name"] == "Approval"
    assert nodes["review"]["outputs"] == ["approved", "rejected"]
    # A case without an ID gets one; each case is a way out, then the default.
    assert [c["id"] for c in nodes["route"]["config"]["cases"]] == ["case_1", "q"]
    assert nodes["route"]["outputs"] == ["case_1", "q", "default"]
    assert made["edges"] == [
        {
            "id": "start:next->route",
            "source": "start",
            "source_output": "next",
            "target": "route",
        },
        {
            "id": "route:case_1->review",
            "source": "route",
            "source_output": "case_1",
            "target": "review",
        },
    ]


def test_a_sound_draft_has_no_problems() -> None:
    found = checked(
        [
            START,
            {
                "id": "shout",
                "kind": "transform",
                "config": {"expression": "$uppercase(input.kind)"},
            },
            {"id": "done", "kind": "end", "config": {"result": "steps.shout.output"}},
        ],
        [{"source": "start", "target": "shout"}, {"source": "shout", "target": "done"}],
    )
    assert found == {"errors": [], "warnings": []}


def test_a_draft_is_held_to_the_format_the_graph_and_each_steps_settings() -> None:
    found = checked(
        [
            START,
            {"id": "Bad-ID", "kind": "delay"},
            {
                "id": "ask",
                "kind": "http",
                "config": {"url": "not a url", "retries": "two"},
            },
            {"id": "review", "kind": "approval"},
        ],
        [
            {"source": "start", "target": "ask"},
            {"source": "ask", "output": "next", "target": "review"},
            {"source": "review", "output": "approved", "target": "ghost"},
        ],
    )
    errors = found["errors"]
    assert any(e.startswith("nodes/1/id:") for e in errors)
    assert any("config/retries" in e for e in errors)  # the schema: an integer
    assert (
        "HTTP request (ask) has no way out 'next'; its ways out are success, error."
        in errors
    )
    assert (
        "A connection from Approval (review) goes to 'ghost', which isn't a step."
        in errors
    )
    assert any("its URL starts with https://" in e for e in errors)


def test_expressions_are_compiled_as_the_runner_compiles_them() -> None:
    found = checked(
        [
            START,
            {
                "id": "check",
                "kind": "if",
                "config": {"condition": "input.kind === 'bug'"},
            },
            {"id": "yes", "kind": "end"},
        ],
        [
            {"source": "start", "target": "check"},
            {"source": "check", "output": "true", "target": "yes"},
        ],
    )
    assert [e for e in found["errors"] if "(check)" in e], found


def test_only_a_loop_repeats_steps_and_there_is_one_start() -> None:
    found = checked(
        [
            {"id": "a", "kind": "transform", "config": {"expression": "1"}},
            {"id": "b", "kind": "transform", "config": {"expression": "2"}},
        ],
        [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    )
    assert any("Add a start step" in e for e in found["errors"])
    assert any("go round in a circle" in e for e in found["errors"])


def test_warnings_say_what_probably_isnt_meant() -> None:
    found = checked(
        [
            START,
            {
                "id": "check",
                "kind": "if",
                "config": {"condition": "input.kind = 'bug'"},
            },
            {"id": "join", "kind": "merge"},
            {"id": "lost", "kind": "transform", "config": {"expression": "1"}},
        ],
        [
            {"source": "start", "target": "check"},
            {"source": "check", "output": "false", "target": "join"},
        ],
    )
    assert found["errors"] == []
    assert (
        "If / else (check)'s true way goes nowhere: a run that takes it ends there."
        in found["warnings"]
    )
    assert any(
        w.startswith("Merge (join) is a merge, but 1 way(s)") for w in found["warnings"]
    )
    assert "Transform (lost): no way from the start leads here." in found["warnings"]


def test_only_the_formats_kinds_and_settings_are_accepted() -> None:
    found = checked(
        [
            START,
            {"id": "open", "kind": "integration", "config": {}},
            {
                "id": "helper",
                "kind": "agent",
                "config": {"instructions": "Help.", "tools": ["send_email"]},
            },
        ],
        [
            {"source": "start", "target": "helper"},
            {"source": "helper", "output": "success", "target": "open"},
        ],
    )
    assert any(
        e.startswith("nodes/1/kind: 'integration' is not one of")
        for e in found["errors"]
    )
    assert any("'tools' was unexpected" in e for e in found["errors"])


def test_a_url_may_put_spaces_only_inside_its_expressions() -> None:
    def url_errors(url: str) -> list[str]:
        found = checked(
            [
                START,
                {"id": "ask", "kind": "http", "config": {"url": url}},
            ],
            [{"source": "start", "target": "ask"}],
        )
        return [e for e in found["errors"] if "its URL" in e]

    assert url_errors("https://api.example.com/vendors/{{ input.kind }}") == []
    assert url_errors("{{ input.kind }}") == []
    assert url_errors("https://api.example.com/a b") != []
