import copy
import json
from typing import Any

import pytest

from forge_agent_runtime.document import DocumentError, load_document, parse_document
from forge_agent_runtime.templating import TemplateError, render, template_data
from tests.conftest import EXAMPLE


def problems(doc: dict[str, Any]) -> list[str]:
    with pytest.raises(DocumentError) as raised:
        parse_document(doc)
    return raised.value.problems


def test_the_builders_example_loads_from_a_file_its_text_or_its_value(example):
    for source in (EXAMPLE, str(EXAMPLE), EXAMPLE.read_text(), example):
        doc = load_document(source)
        assert doc.entry.name == "Support assistant"
        assert [n.id for n in doc.targets("agent", "tools")] == ["memory", "orders", "help"]
        assert [n.id for n in doc.targets("agent", "agents")] == ["billing"]


def test_settings_an_older_agent_lacks_get_their_defaults(example):
    for node in example["nodes"]:
        node["config"].pop("state_schema", None)
        node["config"].pop("max_output_tokens", None)
    doc = parse_document(example)
    assert doc.state_schema == {}
    assert doc.entry.config["max_output_tokens"] is None


def test_a_document_that_breaks_the_format_says_where(example):
    assert "format" in problems({**example, "format": "forge.agent/v1"})[0]
    bad = copy.deepcopy(example)
    bad["edges"][0]["source_output"] = "elsewhere"
    assert any("edges/0/source_output" in p for p in problems(bad))


def test_what_adk_cant_build_is_refused(example):
    two = copy.deepcopy(example)
    two["nodes"].append({**copy.deepcopy(two["nodes"][0]), "id": "agent_two", "name": "Two"})
    assert any("2 chat agents" in p for p in problems(two))

    twice = copy.deepcopy(example)
    twice["nodes"].append(
        {
            **copy.deepcopy(next(n for n in example["nodes"] if n["id"] == "billing")),
            "id": "other",
            "name": "Other",
        }
    )
    twice["edges"].append({"id": "e9", "source": "other", "source_output": "agents", "target": "billing"})
    twice["edges"].append({"id": "e10", "source": "agent", "source_output": "agents", "target": "other"})
    assert any("handed off to by 2 agents" in p for p in problems(twice))

    tool = copy.deepcopy(example)
    tool["edges"].append({"id": "e9", "source": "agent", "source_output": "agents", "target": "memory"})
    assert any("isn't an agent" in p for p in problems(tool))

    user = copy.deepcopy(example)
    user["nodes"][0]["name"] = "User"
    assert any("keeps that name" in p for p in problems(user))

    task = copy.deepcopy(example)
    billing = next(n for n in task["nodes"] if n["id"] == "billing")
    billing["config"]["mode"] = "task"
    task["edges"].append({"id": "e9", "source": "billing", "source_output": "agents", "target": "billing"})
    found = problems(task)
    assert any("can't hand off to anyone" in p for p in found)
    assert any("in a circle" in p for p in found)


def test_templates_fill_in_the_request_and_the_state():
    data = template_data(
        {
            "temp:request": {"userId": "ada", "newMessage": {"parts": [{"text": "hi"}]}},
            "customer_tier": "pro",
            "flags": {"beta": True},
            "count": 3,
            "nothing": None,
            "user:secret": "hidden",
            "temp:other": "hidden",
        }
    )
    assert set(data["state"]) == {"customer_tier", "flags", "count", "nothing"}
    text = render(
        "Hi {{ request.userId }} ({{ state.customer_tier }}), beta={{ state.flags.beta }} "
        "{{ state.flags }} n={{ state.count + 1 }} [{{ state.nothing }}][{{ state.missing }}] "
        "said {{ request.newMessage.parts[0].text }}",
        data,
    )
    assert text == 'Hi ada (pro), beta=true {"beta": true} n=4 [][] said hi'
    assert render("Braces {stay} as they are", data) == "Braces {stay} as they are"
    with pytest.raises(TemplateError, match="couldn't fill in"):
        render("{{ $undefinedFunction() }}", data, what="Its instruction")
    with pytest.raises(TemplateError, match="empty"):
        render("{{ }}", data)


def test_the_json_schema_ships_with_the_package():
    from importlib import resources

    schema = json.loads(resources.files("forge_agent_runtime").joinpath("chat_agent.schema.json").read_text())
    assert schema["properties"]["format"] == {"const": "forge.chat_agent/v1"}
    assert "dependencies" in schema["properties"]
