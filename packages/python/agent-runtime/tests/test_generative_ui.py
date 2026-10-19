import copy
import json
from typing import Any, cast

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events import Event
from google.adk.flows.llm_flows.context._fencing import _present_other_agent_message
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import InMemoryRunner
from google.genai import types
from jsonschema import Draft202012Validator

import forge_agent_runtime.generative_ui.compiler as compiler
from forge_agent_runtime.build import build_app
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.generative_ui import (
    ComponentError,
    Limits,
    UiComponentDefinition,
    check_definition,
    compile_component,
    input_schema_problems,
)
from forge_agent_runtime.present import (
    CHOICES_INSTRUCTION,
    PRESENT,
    SHOWN_NOTE,
    TEAM_ACTION_INSTRUCTION,
    TEAM_INSTRUCTION,
    UI_PROTOCOL,
    PresentHistoryPlugin,
    PresentTool,
    instruction_for,
    present_catalog,
    strip_drawn_trees,
)
from forge_agent_runtime.services import RuntimeServices
from tests.conftest import EXAMPLE_HOSTS, ScriptedLlm, call, text

#: The brief's example: an order's shipment, its line items, a delay when there is one.
SHIPMENT: dict[str, Any] = {
    "format": "forge.ui_component/v1",
    "key": "org.shipment-summary",
    "display_name": "Shipment summary",
    "description": "Show an order's shipping status, its line items and the expected delivery date.",
    "input_schema": {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "maxLength": 80},
            "status": {"type": "string", "enum": ["Packed", "Shipped", "Delivered"]},
            "delivery_date": {"type": "string", "maxLength": 80},
            "delay_reason": {"type": "string", "maxLength": 200},
            "line_items": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 120},
                        "quantity": {"type": "integer", "minimum": 1, "maximum": 9999},
                    },
                    "required": ["name", "quantity"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["order_number", "status", "delivery_date", "line_items"],
        "additionalProperties": False,
    },
    "template": {
        "_type": "Card",
        "title": {"$format": "Order {/order_number}"},
        "children": [
            {"_type": "Badge", "value": {"$bind": "/status"}},
            {"_type": "Fact", "label": "Expected delivery", "value": {"$bind": "/delivery_date"}},
            {
                "_type": "Alert",
                "$when": "/delay_reason",
                "title": "Delayed",
                "description": {"$bind": "/delay_reason"},
            },
            {
                "_type": "ListView",
                "children": {
                    "$each": "/line_items",
                    "max": 20,
                    "template": {"_type": "ListViewItem", "children": [{"$format": "{quantity} × {name}"}]},
                },
            },
            {
                "_type": "Button",
                "label": "Track delivery",
                "_action": {"type": "reply", "text": {"$format": "Track order {/order_number}"}},
            },
        ],
    },
    "examples": [
        {
            "order_number": "A-1042",
            "status": "Shipped",
            "delivery_date": "October 22",
            "line_items": [{"name": "Desk lamp", "quantity": 2}],
        }
    ],
    "version": 3,
}

INPUTS = SHIPMENT["examples"][0]

DRAWN = {
    "_type": "Card",
    "title": "Order A-1042",
    "children": [
        {"_type": "Badge", "value": "Shipped"},
        {"_type": "Fact", "label": "Expected delivery", "value": "October 22"},
        {"_type": "ListView", "children": [{"_type": "ListViewItem", "children": ["2 × Desk lamp"]}]},
        {
            "_type": "Button",
            "label": "Track delivery",
            "_action": {"type": "reply", "text": "Track order A-1042"},
        },
    ],
}

#: Every operator, on inputs that may be missing.
NOTE: dict[str, Any] = {
    "key": "org.note",
    "display_name": "Note",
    "description": "A titled note, maybe urgent, with a count and tags.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 40},
            "note": {"type": "string", "maxLength": 200},
            "urgent": {"type": "boolean"},
            "count": {"type": "integer"},
            "tags": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 20}},
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    "template": {
        "_type": "Card",
        "title": {"$bind": "/title"},
        "children": [
            {"_type": "Alert", "$when": "/note", "title": "Note", "description": {"$bind": "/note"}},
            {"_type": "Badge", "$when": "/urgent", "value": "Urgent"},
            {"_type": "Fact", "$when": "/count", "label": "Count", "value": {"$bind": "/count"}},
            {"_type": "Text", "value": {"$format": "{/note?}|{/count?}|{{x}}"}},
            {
                "_type": "Row",
                "children": {"$each": "/tags", "template": {"_type": "Badge", "value": {"$bind": ""}}},
            },
            {"_type": "Text", "value": {"$bind": "/note", "else": "—"}},
        ],
    },
    "examples": [{"title": "Plain"}],
}


def with_template(definition: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The definition with some of its template's root changed."""
    changed = copy.deepcopy(definition)
    changed["template"].update(changes)
    return changed


def with_child(definition: dict[str, Any], child: Any) -> dict[str, Any]:
    """The definition with one more child at the end of its template's root."""
    changed = copy.deepcopy(definition)
    changed["template"]["children"].append(child)
    return changed


def codes(definition: dict[str, Any], limits: Limits | None = None) -> list[tuple[str, str]]:
    try:
        compile_component(definition, *(limits,) if limits else ())
    except ComponentError as error:
        return [(each.code, each.path) for each in error.diagnostics]
    return []


# -- The compiler --


def test_a_definition_draws_its_inputs_as_base_components() -> None:
    compiled = compile_component(SHIPMENT)
    assert compiled.key == "org.shipment-summary" and compiled.version == 3
    assert compiled.base_components == {
        "Card",
        "Badge",
        "Fact",
        "Alert",
        "ListView",
        "ListViewItem",
        "Button",
    }
    assert compiled.capabilities.has_actions and not compiled.capabilities.has_choices
    assert not compiled.capabilities.has_forms
    assert compiled.expand(INPUTS) == DRAWN

    delayed = compiled.expand({**INPUTS, "delay_reason": "Held at customs."})
    assert delayed["children"][2] == {"_type": "Alert", "title": "Delayed", "description": "Held at customs."}
    # Each line item, by its own fields.
    many = compiled.expand(
        {**INPUTS, "line_items": [{"name": "Lamp", "quantity": 1}, {"name": "Desk", "quantity": 3}]}
    )
    assert [item["children"] for item in many["children"][2]["children"]] == [["1 × Lamp"], ["3 × Desk"]]


def test_when_format_and_else_draw_what_is_there() -> None:
    compiled = compile_component(NOTE)
    bare = compiled.expand({"title": "Plain"})
    assert bare == {
        "_type": "Card",
        "title": "Plain",
        "children": [
            {"_type": "Text", "value": "||{x}"},
            {"_type": "Row", "children": []},
            {"_type": "Text", "value": "—"},
        ],
    }
    # "" and false aren't drawn; 0 is.
    full = compiled.expand({"title": "T", "note": "", "urgent": False, "count": 0, "tags": ["a", "b"]})
    assert full["children"] == [
        {"_type": "Fact", "label": "Count", "value": 0},
        {"_type": "Text", "value": "|0|{x}"},
        {"_type": "Row", "children": [{"_type": "Badge", "value": "a"}, {"_type": "Badge", "value": "b"}]},
        {"_type": "Text", "value": ""},
    ]
    urgent = compiled.expand({"title": "T", "urgent": True, "note": "Call back"})
    assert [child["_type"] for child in urgent["children"][:2]] == ["Alert", "Badge"]


def test_inputs_stay_data_whatever_they_look_like() -> None:
    compiled = compile_component(NOTE)
    looks = '{/title} {"$bind": "/title"}'
    drawn = compiled.expand({"title": looks, "note": "{{x}} {/title}", "tags": ["$each", "{/note}"]})
    assert drawn["title"] == looks
    assert drawn["children"][1] == {"_type": "Text", "value": "{{x}} {/title}||{x}"}
    assert drawn["children"][2]["children"] == [
        {"_type": "Badge", "value": "$each"},
        {"_type": "Badge", "value": "{/note}"},
    ]
    # Values are copied, never shared with the call's arguments.
    props = {"title": "T", "tags": ["a"]}
    assert compiled.expand(props)["children"][1]["children"][0] is not props["tags"]


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (with_template(NOTE, **{"$type": "Card"}), ("operator_unsupported", "template.$type")),
        (with_template(NOTE, _type="Gauge"), ("base_component_unsupported", "template._type")),
        (with_template(NOTE, _type="org.other"), ("base_component_unsupported", "template._type")),
        (with_template(NOTE, colour="red"), ("prop_unsupported", "template.colour")),
        (with_template(NOTE, **{"$when": "/note"}), ("operator_unsupported", "template.$when")),
        (with_child(NOTE, {"_type": "Row", "gap": 20}), ("prop_invalid", "template.children[6].gap")),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$bind": "/title", "$format": "x"}}),
            ("operator_unsupported", "template.children[6].value"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"label": {"$foo": 1}}}),
            ("operator_unsupported", "template.children[6].value.label.$foo"),
        ),
        (
            with_child(NOTE, {"_type": "Button", "label": "Go", "_action": {"type": {"$bind": "/title"}}}),
            ("operator_unsupported", "template.children[6]._action.type"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$bind": "/note"}}),
            ("binding_missing", "template.children[6].value.$bind"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$format": "{/note}"}}),
            ("binding_missing", "template.children[6].value.$format"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$format": "{/tags?}"}}),
            ("format_value_not_scalar", "template.children[6].value.$format"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$format": "{ /title"}}),
            ("definition_invalid", "template.children[6].value.$format"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$bind": "title"}}),
            ("pointer_invalid", "template.children[6].value.$bind"),
        ),
        (
            with_child(NOTE, {"_type": "Text", "value": {"$bind": "/nope"}}),
            ("pointer_invalid", "template.children[6].value.$bind"),
        ),
        (
            with_child(
                NOTE, {"_type": "Row", "children": {"$each": "/title", "template": {"_type": "Text"}}}
            ),
            ("pointer_invalid", "template.children[6].children.$each"),
        ),
        (
            with_child(
                NOTE,
                {"_type": "Row", "children": {"$each": "/tags", "max": 2, "template": {"_type": "Text"}}},
            ),
            ("each_over_limit", "template.children[6].children.$each"),
        ),
        (
            with_child(
                NOTE,
                {"_type": "Row", "children": {"$each": "/tags", "max": 500, "template": {"_type": "Text"}}},
            ),
            ("each_over_limit", "template.children[6].children.max"),
        ),
        (with_child(NOTE, {"$bind": "/title"}), ("definition_invalid", "template.children[6]")),
    ],
)
def test_a_template_is_checked_before_any_inputs(
    definition: dict[str, Any], expected: tuple[str, str]
) -> None:
    assert expected in codes(definition)


def test_a_list_input_without_its_most_items_is_refused() -> None:
    unbounded = copy.deepcopy(NOTE)
    del unbounded["input_schema"]["properties"]["tags"]["maxItems"]
    assert codes(unbounded) == [("each_over_limit", "template.children[4].children.$each")]
    # Under a $when on it, an optional input is always there.
    guarded = with_child(NOTE, {"_type": "Text", "$when": "/note", "value": {"$format": "{/note}"}})
    assert codes(guarded) == []


@pytest.mark.parametrize(
    ("change", "where"),
    [
        ({"pattern": "^A"}, "input_schema.properties.title"),
        ({"format": "date"}, "input_schema.properties.title"),
        ({"default": "x"}, "input_schema.properties.title"),
        ({"maxLength": None}, "input_schema.properties.title.maxLength"),
    ],
)
def test_input_schemas_take_a_small_part_of_json_schema(change: dict[str, Any], where: str) -> None:
    schema = copy.deepcopy(NOTE["input_schema"])
    schema["properties"]["title"].update(change)
    assert [(each.code, each.path) for each in input_schema_problems(schema)] == [
        ("input_schema_unsupported", where)
    ]


def test_input_schemas_have_no_references_combinators_or_open_objects() -> None:
    def problems(schema: Any) -> list[str]:
        return [str(each) for each in input_schema_problems(schema)]

    base = NOTE["input_schema"]
    assert problems(base) == []
    assert problems({"type": "string", "maxLength": 3}) == [
        "input_schema: the inputs are an object (type: object) of named inputs"
    ]
    assert problems({**base, "additionalProperties": True}) == [
        'input_schema: an object input needs "additionalProperties": false'
    ]
    referred = copy.deepcopy(base)
    referred["properties"]["title"] = {"$ref": "#/$defs/x"}
    assert problems(referred)[0].startswith('input_schema.properties.title: give it a "type"')
    either = copy.deepcopy(base)
    either["properties"]["title"] = {"type": "string", "maxLength": 3, "anyOf": [{"type": "string"}]}
    assert problems(either) == ["input_schema.properties.title: a string input can't say anyOf here"]
    nullable = copy.deepcopy(base)
    nullable["properties"]["title"] = {"type": ["string", "null"], "maxLength": 3}
    assert problems(nullable)[0].startswith("input_schema.properties.title: give it a")
    named = copy.deepcopy(base)
    named["properties"]["a b"] = {"type": "boolean"}
    assert "isn't an input name" in problems(named)[0]
    deep: dict[str, Any] = {"type": "string", "maxLength": 1}
    for _ in range(9):
        deep = {"type": "object", "properties": {"x": deep}, "additionalProperties": False}
    assert problems(deep) == ["input_schema" + ".properties.x" * 9 + ": it nests deeper than 8 levels"]
    # A team component compiles only with a schema that fits.
    assert codes({**NOTE, "input_schema": {**base, "additionalProperties": True}}) == [
        ("input_schema_unsupported", "input_schema")
    ]


def test_inputs_are_checked_with_the_bounded_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled = compile_component(SHIPMENT)
    with pytest.raises(ComponentError) as raised:
        compiled.expand({**INPUTS, "status": "Lost", "extra": 1}, where="children[0].props")
    assert [(each.code, each.path) for each in raised.value.diagnostics] == [
        ("component_input_invalid", "children[0].props"),
        ("component_input_invalid", "children[0].props.status"),
    ]
    checked: list[Any] = []
    real = compiler.schema_errors

    def spy(schema: Any, instance: Any) -> Any:
        checked.append(schema)
        return real(schema, instance)

    monkeypatch.setattr(compiler, "schema_errors", spy)
    compiled.expand(INPUTS)
    assert checked == [SHIPMENT["input_schema"]]


def test_limits_hold_what_a_component_writes_and_draws() -> None:
    assert ("template_too_large", "template.children[2]") in codes(NOTE, Limits(template_nodes=3))
    assert ("template_too_large", "template.children[4].children.template") in codes(
        NOTE, Limits(template_depth=2)
    )
    assert codes(NOTE, Limits(definition_bytes=100)) == [("definition_too_large", "")]
    small = compile_component(NOTE, Limits(tree_nodes=3))
    with pytest.raises(ComponentError, match="it draws more than 3 nodes"):
        small.expand({"title": "T", "tags": ["a", "b", "c"]})
    tight = compile_component(NOTE, Limits(tree_bytes=200))
    with pytest.raises(ComponentError, match="bytes: 200 at most"):
        tight.expand({"title": "T", "note": "x" * 190})
    with pytest.raises(ComponentError, match="isn't one of|is not one of|definition_invalid|key"):
        compile_component({**NOTE, "key": "Note"})


def test_publishing_draws_every_example_against_every_base_component() -> None:
    assert check_definition(SHIPMENT) == []
    assert check_definition({**SHIPMENT, "examples": []})[0].code == "definition_invalid"
    wrong = check_definition({**SHIPMENT, "examples": [{**INPUTS, "status": "Lost"}]})
    assert [(each.code, each.path) for each in wrong] == [("component_input_invalid", "examples[0].status")]

    charted = copy.deepcopy(NOTE)
    charted["input_schema"]["properties"]["variant"] = {"type": "string", "enum": ["bar", "pie"]}
    charted["template"]["children"].append(
        {"_type": "Chart", "variant": {"$bind": "/variant", "else": "bar"}}
    )
    assert check_definition(charted) == []
    charted["examples"].append({"title": "Pie", "variant": "pie"})
    # Held to Chart's own variants, not any component's variant (the call's schema merges them).
    [problem] = check_definition(charted)
    assert (problem.code, problem.path) == ("prop_invalid", "examples[1]")
    assert problem.message.startswith("drawn, its Chart's variant: 'pie' is not one of")

    # A button that would do nothing is refused as it is in a call.
    dead = with_child(NOTE, {"_type": "Card", "title": "Go?", "confirm": {"label": "Go"}})
    [problem] = check_definition(dead)
    assert problem.code == "tree_invalid" and "confirm button does nothing" in problem.message


def test_no_base_prop_is_a_name_team_components_keep() -> None:
    for name, component in present_catalog()["components"].items():
        assert not [prop for prop in component["properties"] if prop == "props" or prop.startswith("$")], name


# -- The present tool --


def team(*definitions: dict[str, Any]) -> list[Any]:
    return [compile_component(each) for each in definitions]


def declared(tool: PresentTool) -> dict[str, Any]:
    declaration = tool._get_declaration()
    assert declaration is not None and isinstance(declaration.parameters_json_schema, dict)
    return declaration.parameters_json_schema


def test_present_offers_team_components_by_key_with_their_inputs() -> None:
    schema = declared(PresentTool(["Card"], team(SHIPMENT)))
    Draft202012Validator.check_schema(schema)
    node = schema["$defs"]["node"]
    assert node["properties"]["_type"]["enum"] == ["Card", "org.shipment-summary"]
    assert '"org.shipment-summary" (takes props): Show an order' in node["properties"]["_type"]["description"]
    assert node["properties"]["props"]["properties"] == SHIPMENT["input_schema"]["properties"]
    assert {key: value for key, value in schema.items() if key != "$defs"} == node
    # Never the template: the model gives inputs, and the layout's the component's.
    assert "$format" not in json.dumps(schema)

    both = declared(PresentTool(["Card"], team(SHIPMENT, NOTE)))
    assert len(both["properties"]["props"]["anyOf"]) == 2

    # Team components alone are enough; None is still the defaults.
    assert PresentTool([], team(NOTE)).components == ()
    assert PresentTool(None, team(NOTE)).components[:2] == ("Card", "Row")
    with pytest.raises(ValueError, match="it picks no components"):
        PresentTool([])
    with pytest.raises(ValueError, match="org.note twice"):
        PresentTool(["Card"], team(NOTE, NOTE))


def test_a_call_is_checked_by_each_nodes_type() -> None:
    tool = PresentTool(["Card", "Text"], team(SHIPMENT))
    shipment = {"_type": "org.shipment-summary", "props": INPUTS}
    assert tool.drawn({"_type": "Card", "children": [shipment]})[0] == []

    problems, _, _ = tool.drawn({"_type": "Card", "props": {}, "children": [{**shipment, "title": "x"}]})
    assert problems == [
        "props: only a team component (org.…) takes props",
        "children[0]: org.shipment-summary takes only _type and props, its layout is set: leave out title",
    ]
    problems, _, _ = tool.drawn({**shipment, "children": ["more"]})
    assert problems == [
        "The root: org.shipment-summary takes only _type and props, its layout is set: leave out children"
    ]
    problems, _, _ = tool.drawn(
        {"_type": "Card", "children": [{**shipment, "props": {**INPUTS, "status": "Lost"}}]}
    )
    assert problems[0].startswith("children[0].props.status: 'Lost' is not one of")
    # Neither a team component it doesn't offer, nor a base one it doesn't pick (though its own template has it).
    assert tool.drawn({"_type": "org.note", "props": {}})[0][0].startswith("_type: 'org.note' is not one of")
    assert tool.drawn({"_type": "ListView"})[0][0].startswith("_type: 'ListView' is not one of")


async def test_a_team_call_answers_with_the_tree_it_drew(tool_context: Any = None) -> None:
    tool = PresentTool(["Card", "Text"], team(SHIPMENT))
    answer = await tool.run_async(
        args={
            "_type": "Card",
            "children": ["Your order:", {"_type": "org.shipment-summary", "props": INPUTS}],
        },
        tool_context=cast(Any, tool_context),
    )
    assert answer == {
        "shown": True,
        "note": SHOWN_NOTE,
        "ui": {
            "protocol": UI_PROTOCOL,
            "components": [{"key": "org.shipment-summary", "version": 3}],
            "tree": {"_type": "Card", "children": ["Your order:", DRAWN]},
        },
    }
    # Base components alone answer as they always have.
    plain = await tool.run_async(args={"_type": "Text", "value": "Hi"}, tool_context=cast(Any, tool_context))
    assert plain == {"shown": True, "note": SHOWN_NOTE}

    # A tree its inputs draw against a base component's rules isn't shown.
    charted = copy.deepcopy(NOTE)
    charted["input_schema"]["properties"]["variant"] = {"type": "string", "enum": ["bar", "pie"]}
    charted["template"]["children"].append(
        {"_type": "Chart", "variant": {"$bind": "/variant", "else": "bar"}}
    )
    refused = await PresentTool([], team(charted)).run_async(
        args={"_type": "org.note", "props": {"title": "T", "variant": "pie"}},
        tool_context=cast(Any, tool_context),
    )
    assert refused["shown"] is False
    assert refused["problems"] == [
        "props: drawn, its Chart's variant: 'pie' is not one of ['bar', 'line', 'sparkline', 'area']"
    ]


def test_the_agent_is_told_how_to_call_its_team_components() -> None:
    told = instruction_for(("Card",), team(SHIPMENT))
    assert TEAM_INSTRUCTION in told and TEAM_ACTION_INSTRUCTION in told
    display = copy.deepcopy(NOTE)
    assert TEAM_ACTION_INSTRUCTION not in instruction_for((), team(display))
    # Choices inside a team component aren't the model's to write.
    picked = with_child(NOTE, {"_type": "Choices", "options": [{"label": "Yes"}, {"label": "No"}]})
    [compiled] = team(picked)
    assert compiled.capabilities.has_choices
    told = instruction_for((), [compiled])
    assert CHOICES_INSTRUCTION not in told and TEAM_ACTION_INSTRUCTION in told


# -- History --


def drawn_answer() -> dict[str, Any]:
    return {
        "shown": True,
        "note": SHOWN_NOTE,
        "ui": {"protocol": UI_PROTOCOL, "components": [{"key": "org.note", "version": 1}], "tree": DRAWN},
    }


def response_part(answer: dict[str, Any]) -> types.Part:
    return types.Part(function_response=types.FunctionResponse(id="c1", name=PRESENT, response=answer))


async def test_the_model_never_sees_a_tree_it_drew(llm: ScriptedLlm) -> None:
    agent = LlmAgent(
        name="shipping", model=llm, instruction="Help with orders.", tools=[PresentTool([], team(SHIPMENT))]
    )
    runner = InMemoryRunner(agent=agent, app_name="shipping")
    session = await runner.session_service.create_session(app_name="shipping", user_id="ada")
    llm.turns = [
        [call(PRESENT, _type="org.shipment-summary", props=INPUTS)],
        [text("It shipped and arrives October 22.")],
    ]
    async for _ in runner.run_async(
        user_id="ada",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="Where's A-1042?")]),
    ):
        pass

    def answers(contents: list[types.Content]) -> list[dict[str, Any]]:
        return [
            dict(part.function_response.response or {})
            for content in contents
            for part in content.parts or ()
            if part.function_response is not None and part.function_response.name == PRESENT
        ]

    assert answers(llm.requests[1].contents) == [{"shown": True, "note": SHOWN_NOTE}]
    # The conversation keeps it, for chats to draw.
    kept = await runner.session_service.get_session(app_name="shipping", user_id="ada", session_id=session.id)
    assert kept is not None
    [answer] = answers([event.content for event in kept.events if event.content])
    assert answer["ui"]["tree"] == DRAWN


async def test_other_agents_see_a_drawn_answer_without_its_tree() -> None:
    event = Event(
        author="shipping", content=types.Content(role="user", parts=[response_part(drawn_answer())])
    )
    relayed = _present_other_agent_message(event)
    assert relayed is not None and relayed.content is not None
    request = LlmRequest(contents=[relayed.content])
    before = "".join(part.text or "" for part in request.contents[0].parts or ())
    assert "'ui': {'protocol': " in before

    await PresentHistoryPlugin().before_model_callback(
        callback_context=cast(CallbackContext, None), llm_request=request
    )
    after = "".join(part.text or "" for part in request.contents[0].parts or ())
    assert "'ui'" not in after and "Desk lamp" not in after
    assert str({"shown": True, "note": SHOWN_NOTE}) in after
    # Quoted as ADK quotes it: only the answer changed.
    assert after.replace(str({"shown": True, "note": SHOWN_NOTE}), "") == before.replace(
        str(drawn_answer()), ""
    )


def test_stripping_replaces_parts_and_leaves_the_conversations_own() -> None:
    part = response_part(drawn_answer())
    content = types.Content(role="user", parts=[part, types.Part(text="[x] said: hi")])
    request = LlmRequest(contents=[content])
    strip_drawn_trees(request)
    assert request.contents[0] is not content
    assert request.contents[0].parts[0].function_response.response == {"shown": True, "note": SHOWN_NOTE}  # type: ignore[index,union-attr]
    assert request.contents[0].parts[1] is content.parts[1]  # type: ignore[index]
    assert "ui" in (part.function_response.response or {})  # type: ignore[union-attr]
    # Nothing to leave out: nothing replaced.
    unchanged = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hello")])])
    first = unchanged.contents[0]
    strip_drawn_trees(unchanged)
    assert unchanged.contents[0] is first


async def test_apps_leave_drawn_trees_out_of_every_agents_requests(example, models, web) -> None:
    app = await build_app(
        parse_document(example),
        models=models,
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
        http=web(),
    )
    assert sum(isinstance(plugin, PresentHistoryPlugin) for plugin in app.plugins) == 1
    assert UiComponentDefinition.model_validate(SHIPMENT).version == 3
