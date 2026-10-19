import copy
import json
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from jsonschema import Draft202012Validator

from forge_agent_runtime.build import BuildError, build_app, build_tools
from forge_agent_runtime.document import DocumentError, parse_document
from forge_agent_runtime.present import (
    ACTION_INSTRUCTION,
    CHOICES_INSTRUCTION,
    INSTRUCTION,
    PRESENT,
    REFUSED_NOTE,
    SHOWN_NOTE,
    PresentTool,
    components_of,
    default_components,
    instruction_for,
    known_components,
    present_catalog,
    present_schema,
    problems_of,
)
from forge_agent_runtime.services import RuntimeServices
from tests.conftest import EXAMPLE_HOSTS, ScriptedLlm, call, text

DEFAULTS = ("Card", "Row", "Col", "Fact", "Table", "Chart", "Steps", "Choices")

#: Revenue for a quarter, as the chat draws it: key figures over a chart, in a card.
SUMMARY: dict[str, Any] = {
    "_type": "Card",
    "title": "Q1 revenue",
    "children": [
        "Revenue grew every month.",
        {
            "_type": "Row",
            "children": [
                {"_type": "Fact", "label": "Total", "value": "$405k"},
                {"_type": "Fact", "label": "Growth", "value": "25%", "delta": "+$30k", "trend": "up"},
            ],
        },
        {
            "_type": "Chart",
            "variant": "bar",
            "data": [
                {"label": "Jan", "value": 120000},
                {"label": "Feb", "value": 135000},
                {"label": "Mar", "value": 150000},
            ],
        },
        {
            "_type": "Table",
            "columns": [{"label": "Month"}, {"label": "Revenue", "align": "end"}],
            "rows": [["Jan", 120000]],
        },
        {"_type": "Steps", "steps": [{"title": "Check March", "detail": "It closed late."}]},
        {
            "_type": "Choices",
            "options": [
                {"label": "By region", "reply": "Break Q1 down by region."},
                {"label": "Next quarter"},
            ],
        },
    ],
}


def with_rich_answers(example: dict[str, Any], *picks: list[str] | None) -> dict[str, Any]:
    """The example, its agent with a Rich answers node for each pick (None: as an older document has it)."""
    doc = copy.deepcopy(example)
    for n, picked in enumerate(picks or (None,)):
        config = {} if picked is None else {"components": picked}
        doc["nodes"].append(
            {"id": f"rich{n}", "kind": "rich_answers", "name": f"Rich {n}", "config": config, "outputs": []}
        )
        doc["edges"].append(
            {"id": f"agent:tools->rich{n}", "source": "agent", "source_output": "tools", "target": f"rich{n}"}
        )
    return doc


def presents(agent: Any) -> list[PresentTool]:
    return [tool for tool in agent.tools if isinstance(tool, PresentTool)]


def _refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value["$ref"]] if "$ref" in value else []
        return own + [ref for item in value.values() for ref in _refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _refs(item)]
    return []


def test_the_catalog_is_the_packages_vocabulary_and_choices() -> None:
    catalog = present_catalog()
    assert len(known_components()) == 36 == len(catalog["components"])
    assert {"Card", "Form", "Select", "Map", "Diagram", "Markdown", "Choices"} <= set(known_components())
    assert default_components() == DEFAULTS
    # Each component's own props, without the node's frame.
    assert set(catalog["components"]["Fact"]["properties"]) == {
        "label",
        "value",
        "delta",
        "trend",
        "upIsGood",
    }
    assert not {"_type", "_key", "children"} & set(catalog["components"]["Card"]["properties"])


def test_the_schema_holds_only_the_picked_components() -> None:
    schema = present_schema(DEFAULTS)
    node = schema["$defs"]["node"]
    assert node["properties"]["_type"]["enum"] == list(DEFAULTS)
    assert '"Choices": Buttons the person clicks' in node["properties"]["_type"]["description"]
    assert '"Map"' not in node["properties"]["_type"]["description"]
    # Their props and no others; nothing acts, so no _action.
    assert "places" not in node["properties"] and "_action" not in node["properties"]
    assert {"title", "label", "value", "columns", "rows", "variant", "data", "steps", "options"} <= set(
        node["properties"]
    )
    # The root is a node (function calling wants an object there), with no
    # _key, and Gemini refuses a loop through the root: every reference is into $defs.
    assert {key: value for key, value in schema.items() if key != "$defs"} == node
    assert "_key" not in node["properties"] and node["required"] == ["_type"]
    assert set(_refs(schema)) == {"#/$defs/node", "#/$defs/children"}
    assert schema["$defs"]["children"]["type"] == "array"


def test_components_that_act_bring_action_and_shared_props_take_any_of_their_schemas() -> None:
    schema = present_schema(components_of(["Button", "Fact"]))
    node = schema["$defs"]["node"]
    # Offered in the catalog's order, not the pick's.
    assert node["properties"]["_type"]["enum"] == ["Fact", "Button"]
    assert node["properties"]["_action"]["properties"]["type"]["enum"] == ["reply"]
    # Fact's label and Button's are described apart: either fits.
    assert len(node["properties"]["label"]["anyOf"]) == 2


def test_a_pick_brings_what_it_needs_and_a_wrong_one_is_refused() -> None:
    assert components_of(["Carousel"]) == ("Card", "Carousel")
    assert components_of(["ListView"]) == ("ListView", "ListViewItem")
    assert components_of(None) == DEFAULTS
    with pytest.raises(ValueError, match="Gauge isn't a component it knows"):
        components_of(["Card", "Gauge"])
    with pytest.raises(ValueError, match="it picks no components"):
        components_of([])


def test_every_component_together_is_a_schema() -> None:
    schema = present_schema(known_components())
    Draft202012Validator.check_schema(schema)
    assert len(schema["properties"]["_type"]["enum"]) == 36
    assert "_action" in schema["properties"]


def test_a_composition_fits_and_one_that_doesnt_is_told_where() -> None:
    assert problems_of(SUMMARY, DEFAULTS) == []
    wrong = copy.deepcopy(SUMMARY)
    wrong["children"][1]["children"][0] = {"_type": "Gauge", "value": 3}
    wrong["children"][2]["variant"] = "pie"
    wrong["children"][5]["options"][1] = {"reply": "No label"}
    problems = problems_of(wrong, DEFAULTS)
    assert len(problems) == 3
    assert problems[0].startswith("children[1].children[0]._type: 'Gauge' is not one of")
    assert problems[1].startswith("children[2].variant: 'pie' is not one of")
    assert problems[2] == "children[5].options[1]: 'label' is a required property"
    assert problems_of({"title": "No type"}, DEFAULTS) == ["The root: '_type' is a required property"]
    # A component it didn't pick doesn't fit, though the chat could draw it.
    assert problems_of({"_type": "Map", "places": []}, DEFAULTS)[0].startswith("_type: 'Map' is not one of")


def test_a_button_that_would_do_nothing_is_refused() -> None:
    picked = ("Card", "Text", "Form", "Input")
    reply = {"type": "reply", "text": "Contact details:"}
    # The button on the card, the action on the form inside: the chat makes the card that form.
    on_the_card = {
        "_type": "Card",
        "title": "Start a new claim",
        "confirm": {"label": "Continue"},
        "children": [
            {"_type": "Text", "value": "Your details, so we can reach you."},
            {
                "_type": "Form",
                "_action": reply,
                "children": [{"_type": "Input", "label": "Email", "name": "email"}],
            },
        ],
    }
    assert problems_of(on_the_card, picked) == []
    as_form = {
        **on_the_card,
        "asForm": True,
        "_action": reply,
        "children": [{"_type": "Input", "name": "email"}],
    }
    assert problems_of(as_form, picked) == []

    nothing = {
        "_type": "Card",
        "title": "Start",
        "confirm": {"label": "Continue"},
        "cancel": {"label": "Not now"},
        "children": [{"_type": "Text", "value": "Ready?"}],
    }
    assert problems_of({"_type": "Card", "children": [nothing]}, picked) == [
        'children[0]: The Card "Start"\'s confirm button does nothing when clicked: give the confirm an '
        "_action, or make the Card asForm with an _action so it sends the fields in it.",
        'children[0]: The Card "Start"\'s cancel button does nothing when clicked: give the cancel an '
        "_action, or leave it out.",
    ]
    acting = {
        **nothing,
        "confirm": {"label": "Continue", "_action": reply},
        "cancel": {"label": "No", "_action": reply},
    }
    assert problems_of(acting, picked) == []


def test_what_the_agent_is_told_follows_its_components() -> None:
    told = instruction_for(DEFAULTS)
    assert told.startswith(INSTRUCTION) and CHOICES_INSTRUCTION in told and ACTION_INSTRUCTION not in told
    told = instruction_for(components_of(["Form", "Input"]))
    assert ACTION_INSTRUCTION in told and CHOICES_INSTRUCTION not in told


async def test_an_agent_offers_what_its_rich_answers_nodes_pick(example, models, web) -> None:
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS)

    async def root_of(doc: dict[str, Any]) -> LlmAgent:
        app = await build_app(parse_document(doc), models=models, services=services, http=web())
        assert isinstance(app.root_agent, LlmAgent)
        return app.root_agent

    [tool] = presents(await root_of(with_rich_answers(example, ["Card", "Chart"])))
    assert tool.name == PRESENT and tool.components == ("Card", "Chart")
    declaration = tool._get_declaration()
    assert declaration is not None
    assert declaration.parameters_json_schema == present_schema(("Card", "Chart"))

    # An older node, without picks, offers the defaults.
    [tool] = presents(await root_of(with_rich_answers(example, None)))
    assert tool.components == DEFAULTS

    # Two nodes are still one tool (a model can't be offered a name twice), with both's picks.
    [tool] = presents(await root_of(with_rich_answers(example, ["Fact"], ["Map", "Fact"])))
    assert tool.components == ("Fact", "Map")

    # The document's schema knows the components: an unknown one, neither a
    # base name nor a team key (org.…), is refused as it's read.
    with pytest.raises(DocumentError, match="'Gauge' is not valid under any of the given schemas"):
        await root_of(with_rich_answers(example, ["Gauge"]))
    with pytest.raises(BuildError, match="'Rich 0': it picks no components"):
        await root_of(with_rich_answers(example, []))

    # Without it, no present.
    assert presents(await root_of(example)) == []


async def test_present_answers_at_once_and_the_agent_is_told_to_write_too(
    example, models, web, llm: ScriptedLlm
) -> None:
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS)
    app = await build_app(
        parse_document(with_rich_answers(example, list(DEFAULTS))),
        models=models,
        services=services,
        http=web(),
    )
    llm.turns = [
        [call(PRESENT, **SUMMARY)],
        [call(PRESENT, _type="Gauge")],
        [text("Revenue grew 25% over the quarter, to $405k.")],
    ]
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name=app.name, user_id="ada")
    responses: list[dict[str, Any]] = []
    async for event in runner.run_async(
        user_id="ada",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="Sum up Q1 with a chart.")]),
    ):
        for part in (event.content.parts if event.content else None) or ():
            if part.function_response is not None:
                responses.append(dict(part.function_response.response or {}))

    # Each answer tells the model it's its own, not to quote.
    assert responses[0] == {"shown": True, "note": SHOWN_NOTE}
    # One that doesn't fit isn't shown, and the model is told why, to send it again.
    assert responses[1]["shown"] is False
    assert responses[1]["problems"][0].startswith("_type: 'Gauge' is not one of")
    assert responses[1]["note"] == REFUSED_NOTE

    first = llm.requests[0]
    declared = [
        d.name
        for tool in first.config.tools or ()
        if isinstance(tool, types.Tool)
        for d in tool.function_declarations or ()
    ]
    assert PRESENT in declared
    assert instruction_for(DEFAULTS) in str(first.config.system_instruction)


async def test_it_builds_on_its_own_for_an_agent_built_elsewhere(models, web) -> None:
    [tool] = await build_tools("Rich answers", "rich_answers", {}, models=models, http=web())
    assert isinstance(tool, PresentTool) and tool.components == DEFAULTS
    assert json.dumps(tool._get_declaration().parameters_json_schema)  # type: ignore[union-attr]
    # Without a document's schema, building it is what refuses a wrong pick.
    with pytest.raises(BuildError, match="'Rich answers': Gauge isn't a component it knows"):
        await build_tools(
            "Rich answers", "rich_answers", {"components": ["Gauge"]}, models=models, http=web()
        )
