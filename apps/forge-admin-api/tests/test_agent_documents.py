"""Agent documents: what the API stores of the one the builder sends."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forge_admin.agent_documents import (
    ID_PATTERN,
    AgentError,
    checked_document,
    new_agent_id,
)

# The web console's example agent, as its builder exports it.
# The example ADK workflow, kept with the package that runs it.
EXAMPLE = (
    Path(__file__).parents[3]
    / "packages/python/tasks/adk-workflows/tests/fixtures/example.agent.json"
)
ORGANIZATION = "3f6c0000-0000-4000-8000-000000000001"
MADE = datetime(2026, 9, 26, 9, 30, tzinfo=UTC)
SAVED = datetime(2026, 9, 26, 10, 45, 12, 345000, tzinfo=UTC)


def example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def check(document: dict[str, Any], max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    return checked_document(
        document,
        agent_id="ag_abc123def4",
        organization_id=ORGANIZATION,
        created_at=MADE,
        updated_at=SAVED,
        max_bytes=max_bytes,
    )


def test_the_builders_example_is_an_agent() -> None:
    stored = check(example())
    assert stored["format"] == "forge.agent/v1"
    assert len(stored["nodes"]) == 23


def test_the_api_sets_the_id_organization_and_times_in_place() -> None:
    sent = example()
    stored = check(sent)
    assert stored["id"] == "ag_abc123def4"
    assert stored["organization_id"] == ORGANIZATION
    assert stored["created_at"] == "2026-09-26T09:30:00.000Z"
    assert stored["updated_at"] == "2026-09-26T10:45:12.345Z"
    # Keys stay where the builder put them, and nothing else changes.
    assert list(stored) == list(sent)
    assert stored["nodes"] == sent["nodes"]


def test_a_draft_that_cant_run_yet_is_saved() -> None:
    # Only the format is checked: the start leads nowhere, and nothing leads to
    # anything else.
    draft = example()
    draft["edges"] = []
    assert check(draft)["edges"] == []


def test_a_document_that_isnt_an_agent_is_refused_with_where() -> None:
    document = example()
    document["nodes"][1]["kind"] = "teleport"
    del document["edges"]
    with pytest.raises(AgentError) as refused:
        check(document)
    message = str(refused.value)
    assert "isn't a forge.agent/v1 document" in message
    assert "'edges' is a required property" in message
    assert "nodes/1/kind" in message


def test_a_node_missing_a_setting_is_refused() -> None:
    document = example()
    triage = next(node for node in document["nodes"] if node["id"] == "triage")
    del triage["config"]["instruction"]
    with pytest.raises(AgentError, match="'instruction' is a required property"):
        check(document)


def test_a_sub_agents_settings_are_checked_too() -> None:
    document = example()
    bug_team = document["nodes"][6]
    assert bug_team["id"] == "bug_team"
    reproducer = bug_team["config"]["sub_agents"][0]["config"]
    del reproducer["instruction"]
    reproducer["mode"] = "task"
    with pytest.raises(AgentError) as refused:
        check(document)
    message = str(refused.value)
    assert (
        "nodes/6/config/sub_agents/0/config: 'instruction' is a required property"
        in message
    )
    # Only a node has a mode; a sub-agent runs as its parent has it.
    assert "nodes/6/config/sub_agents/0/config: Additional properties" in message


def test_too_large_an_agent_is_refused() -> None:
    with pytest.raises(AgentError, match="the most an agent can be is 1,024"):
        check(example(), max_bytes=1024)


def test_new_ids_are_unique_and_the_builders_shape() -> None:
    made = {new_agent_id() for _ in range(500)}
    assert len(made) == 500
    assert all(re.fullmatch(ID_PATTERN, agent_id) for agent_id in made)
