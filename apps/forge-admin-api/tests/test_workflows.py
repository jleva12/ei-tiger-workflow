"""Workflow documents: what the API stores of the one the builder sends."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forge_admin.workflows import (
    ID_PATTERN,
    WorkflowError,
    checked_document,
    new_workflow_id,
)

# The web console's example workflow, as its builder exports it.
EXAMPLE = Path(__file__).with_name("fixtures") / "example.workflow.json"
ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"
MADE = datetime(2026, 9, 26, 9, 30, tzinfo=UTC)
SAVED = datetime(2026, 9, 26, 10, 45, 12, 345000, tzinfo=UTC)


def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def check(document: dict[str, Any], max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    return checked_document(
        document,
        workflow_id="wf_abc123def4",
        organization_id=ORGANIZATION,
        created_at=MADE,
        updated_at=SAVED,
        max_bytes=max_bytes,
    )


def test_the_builders_example_is_a_workflow() -> None:
    stored = check(example())
    assert stored["format"] == "forge.workflow/v1"
    assert len(stored["nodes"]) == 12


def test_the_api_sets_the_id_organization_and_times_in_place() -> None:
    sent = example()
    stored = check(sent)
    assert stored["id"] == "wf_abc123def4"
    assert stored["organization_id"] == ORGANIZATION
    assert stored["created_at"] == "2026-09-26T09:30:00.000Z"
    assert stored["updated_at"] == "2026-09-26T10:45:12.345Z"
    # Keys stay where the builder put them, and nothing else changes.
    assert list(stored) == list(sent)
    assert stored["nodes"] == sent["nodes"]


def test_a_document_that_isnt_a_workflow_is_refused_with_where() -> None:
    document = example()
    document["nodes"][1]["kind"] = "teleport"
    del document["edges"]
    with pytest.raises(WorkflowError) as refused:
        check(document)
    message = str(refused.value)
    assert "isn't a forge.workflow/v1 document" in message
    assert "'edges' is a required property" in message
    assert "nodes/1/kind" in message


def test_a_step_missing_a_setting_is_refused() -> None:
    document = example()
    agent = next(node for node in document["nodes"] if node["kind"] == "agent")
    del agent["config"]["instructions"]
    with pytest.raises(WorkflowError, match="'instructions' is a required property"):
        check(document)


def test_a_document_saved_before_a_setting_existed_is_saved_with_its_default() -> None:
    sent = example()
    agents = [node for node in sent["nodes"] if node["kind"] == "agent"]
    for agent in agents:
        del agent["config"]["thinking_level"]
    stored = check(sent)
    kept = [node for node in stored["nodes"] if node["kind"] == "agent"]
    assert [agent["config"]["thinking_level"] for agent in kept] == [""]
    # What it had is as it was, and what was sent isn't changed.
    for before, after in zip(agents, kept, strict=True):
        assert {k: v for k, v in after["config"].items() if k != "thinking_level"} == (
            before["config"]
        )
    assert all("thinking_level" not in agent["config"] for agent in agents)
    others = [node for node in stored["nodes"] if node["kind"] != "agent"]
    assert others == [node for node in sent["nodes"] if node["kind"] != "agent"]


def test_an_http_step_saved_before_it_declared_its_output_declares_none() -> None:
    sent = example()
    requests = [node for node in sent["nodes"] if node["kind"] == "http"]
    for request in requests:
        del request["config"]["output_schema"]
    stored = check(sent)
    kept = [node for node in stored["nodes"] if node["kind"] == "http"]
    assert [request["config"]["output_schema"] for request in kept] == [{}, {}, {}]
    assert all("output_schema" not in request["config"] for request in requests)


def test_too_large_a_workflow_is_refused() -> None:
    with pytest.raises(WorkflowError, match="the most a workflow can be is 1,024"):
        check(example(), max_bytes=1024)


def test_new_ids_are_unique_and_the_builders_shape() -> None:
    made = {new_workflow_id() for _ in range(500)}
    assert len(made) == 500
    assert all(re.fullmatch(ID_PATTERN, workflow_id) for workflow_id in made)
