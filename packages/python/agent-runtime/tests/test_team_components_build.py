"""Agents whose Rich answers pick team UI components by key: built with those
their document carries (an export, a workflow run), else the organization's
(``RuntimeServices.ui_components``), never quietly without them."""

import copy
from collections.abc import Sequence
from typing import Any

import pytest

from forge_agent_runtime.build import BuildError, build_app, build_tools
from forge_agent_runtime.document import parse_document, schema_problems, with_defaults
from forge_agent_runtime.generative_ui import UiComponentDefinition, team_keys
from forge_agent_runtime.present import PresentTool
from forge_agent_runtime.services import RuntimeServices
from tests.conftest import EXAMPLE_HOSTS, without
from tests.test_generative_ui import NOTE, SHIPMENT

KEY = SHIPMENT["key"]


def picking(example: dict[str, Any], *components: str) -> dict[str, Any]:
    """The example, its agent with a Rich answers node picking these."""
    doc = copy.deepcopy(example)
    doc["nodes"].append(
        {
            "id": "rich",
            "kind": "rich_answers",
            "name": "Rich",
            "config": {"components": list(components)},
            "outputs": [],
        }
    )
    doc["edges"].append({"id": "r", "source": "agent", "source_output": "tools", "target": "rich"})
    return doc


class Library:
    """The organization's published team components, as the hosted runtime finds them."""

    def __init__(self, *definitions: dict[str, Any]) -> None:
        self.definitions = {each["key"]: each for each in definitions}
        self.asked: list[tuple[list[str], str | None]] = []

    async def __call__(
        self, keys: Sequence[str], *, organization_id: str | None
    ) -> list[UiComponentDefinition]:
        self.asked.append((list(keys), organization_id))
        missing = [key for key in keys if key not in self.definitions]
        if missing:
            raise LookupError(f"the organization has no team component {missing[0]} to use")
        return [UiComponentDefinition.model_validate(self.definitions[key]) for key in keys]


async def build(doc: dict[str, Any], models: Any, web: Any, **services: Any) -> Any:
    services.setdefault("allowed_hosts", EXAMPLE_HOSTS)
    return await build_app(
        parse_document(doc), models=models, services=RuntimeServices(**services), http=web()
    )


def present_of(app: Any) -> PresentTool:
    [tool] = [tool for tool in app.root_agent.tools if isinstance(tool, PresentTool)]
    return tool


def test_the_format_takes_team_keys_and_the_components_a_document_carries(example: dict[str, Any]) -> None:
    doc = picking(example, "Card", KEY)
    assert schema_problems(with_defaults(doc)) == []
    assert team_keys(doc) == [KEY]
    carried = {**doc, "ui_components": {KEY: {**SHIPMENT, "version": 3}}}
    assert schema_problems(with_defaults(carried)) == []
    assert parse_document(carried).ui_components[KEY]["version"] == 3
    # Neither a base name nor a key.
    assert schema_problems(with_defaults(picking(example, "org.Shipment")))
    assert schema_problems(with_defaults({**doc, "ui_components": {"Shipment": {}}}))


async def test_a_document_carrying_its_components_builds_without_the_organization(
    example: dict[str, Any], models: Any, web: Any
) -> None:
    doc = picking(example, "Card", KEY)
    doc["ui_components"] = {KEY: {**SHIPMENT, "version": 3}}
    tool = present_of(await build(doc, models, web))
    assert tool.components == ("Card",) and list(tool.team) == [KEY]
    assert tool.team[KEY].version == 3
    declared = tool._get_declaration()
    assert declared is not None and KEY in str(declared.parameters_json_schema)

    # Team components alone are a Rich answers node's picks too.
    only = picking(example, KEY)
    only["ui_components"] = doc["ui_components"]
    assert present_of(await build(only, models, web)).components == ()


async def test_the_organizations_are_found_where_it_runs_and_never_left_out(
    example: dict[str, Any], models: Any, web: Any
) -> None:
    doc = picking(example, "Card", KEY, NOTE["key"])
    with pytest.raises(BuildError, match="which only the hosted runtime has"):
        await build(doc, models, web)

    library = Library(SHIPMENT, NOTE)
    tool = present_of(await build(doc, models, web, ui_components=library))
    assert list(tool.team) == [KEY, NOTE["key"]]
    # In the agent's organization; base components alone ask nothing.
    assert library.asked == [([KEY, NOTE["key"]], "org-1")]
    await build(picking(example, "Card"), models, web, ui_components=library)
    assert len(library.asked) == 1

    # Carried first, then the organization's for the rest.
    carried = {**doc, "ui_components": {KEY: SHIPMENT}}
    await build(carried, models, web, ui_components=library)
    assert library.asked[-1] == ([NOTE["key"]], "org-1")

    with pytest.raises(BuildError, match="'Rich': the organization has no team component org.note to use"):
        await build(doc, models, web, ui_components=Library(SHIPMENT))


async def test_a_carried_component_that_cant_be_drawn_doesnt_build(
    example: dict[str, Any], models: Any, web: Any
) -> None:
    broken = copy.deepcopy(SHIPMENT)
    broken["template"]["_type"] = "Gauge"
    doc = picking(example, KEY)
    doc["ui_components"] = {KEY: broken}
    with pytest.raises(BuildError, match="the team component org.shipment-summary can't be drawn"):
        await build(doc, models, web)
    doc["ui_components"] = {KEY: NOTE}
    with pytest.raises(BuildError, match="what it carries as org.shipment-summary is org.note"):
        await build(doc, models, web)


async def test_a_saved_agent_uses_the_components_the_agent_using_it_carries(
    example: dict[str, Any], models: Any, web: Any
) -> None:
    refunds = picking(
        without(copy.deepcopy(example), "memory", "orders", "help", "billing", "billing_api"), KEY
    )
    refunds.update(id="ca_refunds", name="Refunds")
    refunds["nodes"][0]["name"] = "Refunds desk"
    doc = without(example, "help")
    doc["nodes"].append(
        {
            "id": "saved",
            "kind": "saved_agent",
            "name": "Refunds",
            "config": {"agent": "ca_refunds"},
            "outputs": [],
        }
    )
    doc["edges"].append({"id": "s", "source": "agent", "source_output": "agents", "target": "saved"})
    bundled = {**doc, "dependencies": {"ca_refunds@2": refunds}, "ui_components": {KEY: SHIPMENT}}
    root = (await build(bundled, models, web)).root_agent
    desk = root.sub_agents[-1]
    [tool] = [tool for tool in desk.tools if isinstance(tool, PresentTool)]
    assert list(tool.team) == [KEY]


async def test_a_tool_built_elsewhere_finds_the_organizations_too(models: Any, web: Any) -> None:
    library = Library(SHIPMENT)
    [tool] = await build_tools(
        "Rich answers",
        "rich_answers",
        {"components": ["Card", KEY]},
        models=models,
        http=web(),
        services=RuntimeServices(ui_components=library),
        organization_id="org-2",
    )
    assert isinstance(tool, PresentTool) and list(tool.team) == [KEY]
    assert library.asked == [([KEY], "org-2")]
    with pytest.raises(BuildError, match="only the hosted runtime has"):
        await build_tools("Rich answers", "rich_answers", {"components": [KEY]}, models=models, http=web())
