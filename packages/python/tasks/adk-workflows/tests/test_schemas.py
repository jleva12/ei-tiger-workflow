"""JSON Schemas as Pydantic models: what the web's schema builder writes, and
what stored documents may hold, held to and coerced as ADK would."""

from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from forge_task_adk_workflows.graph.schemas import held_to, is_model, problems, to_model


def holds(schema: dict[str, Any], value: Any) -> Any:
    return held_to(to_model(schema), value)


def refuses(schema: dict[str, Any], value: Any) -> str:
    with pytest.raises(ValidationError) as raised:
        holds(schema, value)
    return problems(raised.value, "value")


def obj(properties: dict[str, Any], *required: str, **more: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, **more}
    if required:
        schema["required"] = list(required)
    return schema


# The Support desk's customer, as the web's builder writes it.
CUSTOMER = obj(
    {
        "name": {"type": "string"},
        "plan": {"type": "string", "enum": ["free", "pro", "enterprise"]},
        "contacts": {
            "type": "array",
            "description": "Where replies go.",
            "items": {"type": "string", "format": "email"},
        },
    },
    "name",
    "plan",
    "contacts",
)


def test_an_object_is_a_model_with_its_required_and_optional_fields() -> None:
    model = to_model(CUSTOMER)
    assert is_model(model)
    assert list(model.model_fields) == ["name", "plan", "contacts"]
    assert all(field.is_required() for field in model.model_fields.values())
    assert model.model_fields["contacts"].description == "Where replies go."
    optional = to_model(obj({"n": {"type": "integer"}}))
    assert not optional.model_fields["n"].is_required()
    # Left out, it stays out; given, it's kept.
    assert held_to(optional, {}) == {}
    assert held_to(optional, {"n": 2}) == {"n": 2}


def test_it_coerces_as_pydantic_does() -> None:
    schema = obj(
        {
            "n": {"type": "integer"},
            "x": {"type": "number"},
            "ok": {"type": "boolean"},
            "when": {"type": "string", "format": "date"},
        }
    )
    assert holds(schema, {"n": "3", "x": "2.5", "ok": "true", "when": "2026-09-28"}) == {
        "n": 3,
        "x": 2.5,
        "ok": True,
        "when": "2026-09-28",
    }
    # A whole number stays one, as JSON has it.
    assert holds(schema, {"x": 4}) == {"x": 4}
    assert "n: Input should be a valid integer" in refuses(schema, {"n": "three"})
    assert "when: Input should be a valid string" in refuses(schema, {"when": 20260928})


def test_enums_and_consts_are_literals() -> None:
    assert holds(CUSTOMER, {"name": "Ada", "plan": "pro", "contacts": []})["plan"] == "pro"
    assert "plan: Input should be 'free', 'pro' or 'enterprise'" in refuses(
        CUSTOMER, {"name": "Ada", "plan": "gold", "contacts": []}
    )
    # Without a type too, and of objects.
    assert holds({"enum": [1, "two"]}, "two") == "two"
    assert holds({"const": 7}, 7) == 7
    refuses({"const": 7}, 8)
    assert holds({"enum": [{"a": 1}, [2]]}, [2]) == [2]
    assert "Input should be {'a': 1}" in refuses({"enum": [{"a": 1}]}, {"a": 2})


def test_a_closed_object_refuses_other_keys_and_an_open_one_keeps_them() -> None:
    closed = obj({"a": {"type": "string"}}, additionalProperties=False)
    assert "b: Extra inputs are not permitted" in refuses(closed, {"a": "x", "b": 1})
    assert holds(obj({"a": {"type": "string"}}), {"a": "x", "b": 1}) == {
        "a": "x",
        "b": 1,
    }
    # An object with no properties that takes any keys is a dict.
    assert holds({"type": "object"}, {"any": ["thing"]}) == {"any": ["thing"]}
    refuses({"type": "object"}, [1])


def test_nullable_types_and_enums_take_null() -> None:
    schema = obj(
        {
            "note": {"type": ["string", "null"]},
            "level": {"type": ["string", "null"], "enum": ["low", "high", None]},
        },
        "note",
        "level",
    )
    assert holds(schema, {"note": None, "level": None}) == {"note": None, "level": None}
    assert holds(schema, {"note": "x", "level": "low"}) == {"note": "x", "level": "low"}
    assert "note: Input should be a valid string" in refuses(schema, {"note": 1, "level": None})
    # Not nullable: a null isn't a string.
    refuses(obj({"note": {"type": "string"}}), {"note": None})


def test_string_and_number_limits() -> None:
    schema = obj(
        {
            "code": {
                "type": "string",
                "minLength": 2,
                "maxLength": 4,
                "pattern": "^[A-Z]+",
            },
            "word": {"type": "string", "pattern": "b"},
            "look": {"type": "string", "pattern": "^(?=.*x)"},
            "age": {"type": "integer", "minimum": 0, "maximum": 130},
            "share": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
            "old": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
            "step": {"type": "integer", "multipleOf": 5},
        }
    )
    assert holds(schema, {"code": "AB1", "word": "abc", "look": "axe", "age": 0}) == {
        "code": "AB1",
        "word": "abc",
        "look": "axe",
        "age": 0,
    }
    for value, where in [
        ({"code": "A"}, "code: String should have at least 2 characters"),
        ({"code": "ABCDE"}, "code: String should have at most 4 characters"),
        ({"code": "ab"}, "code: String should match pattern"),
        # A pattern Rust's regex can't read is Python's.
        ({"look": "abc"}, "look: String should match pattern"),
        ({"age": -1}, "age: Input should be greater than or equal to 0"),
        ({"age": 131}, "age: Input should be less than or equal to 130"),
        ({"share": 0}, "share: Input should be greater than 0"),
        ({"share": 1}, "share: Input should be less than 1"),
        ({"old": 0}, "old: Input should be greater than 0"),
        ({"step": 7}, "step: Input should be a multiple of 5"),
    ]:
        assert where in refuses(schema, value)


def test_list_limits_items_and_unique_items() -> None:
    schema = obj(
        {
            "tags": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
            }
        }
    )
    assert holds(schema, {"tags": ["1", 2]}) == {"tags": [1, 2]}
    assert "tags: List should have at least 1 item" in refuses(schema, {"tags": []})
    assert "tags: List should have at most 3 items" in refuses(schema, {"tags": [1, 2, 3, 4]})
    assert "tags: Items should be unique" in refuses(schema, {"tags": [1, 1]})
    assert "tags/1: Input should be a valid integer" in refuses(schema, {"tags": [1, "x"]})
    # A list at the top.
    assert holds({"type": "array", "items": {"type": "string"}}, ["a"]) == ["a"]


def test_nested_objects_are_nested_models() -> None:
    schema = obj(
        {
            "order": obj(
                {
                    "items": {
                        "type": "array",
                        "items": obj(
                            {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                            "sku",
                            "qty",
                        ),
                    }
                },
                "items",
            )
        },
        "order",
        title="Purchase",
    )
    model = to_model(schema)
    assert model.__name__ == "Purchase"
    assert holds(schema, {"order": {"items": [{"sku": "A1", "qty": "3"}]}}) == {
        "order": {"items": [{"sku": "A1", "qty": 3}]}
    }
    assert "order/items/0/qty: Field required" in refuses(schema, {"order": {"items": [{"sku": "A1"}]}})


def test_any_of_takes_any_of_them() -> None:
    schema = {"anyOf": [{"type": "integer"}, obj({"id": {"type": "integer"}}, "id")]}
    assert holds(schema, 3) == 3
    assert holds(schema, {"id": "4"}) == {"id": 4}
    refuses(schema, "x")
    assert holds({"oneOf": [{"type": "string"}]}, "one") == "one"


def test_properties_that_arent_python_names_keep_their_names() -> None:
    schema = obj(
        {
            "customer-id": {"type": "string"},
            "class": {"type": "integer"},
            "model_name": {"type": "string"},
            "json": {"type": "boolean"},
            "_private": {"type": "string"},
        },
        "customer-id",
    )
    value = {
        "customer-id": "c1",
        "class": 2,
        "model_name": "m",
        "json": True,
        "_private": "p",
    }
    assert holds(schema, value) == value
    assert "customer-id: Field required" in refuses(schema, {})
    # The schema ADK sends a model says so too.
    assert set(to_model(schema).model_json_schema()["properties"]) == set(value)


def test_what_changes_nothing_is_ignored_and_what_says_nothing_takes_anything() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:x",
        "title": "Thing",
        "examples": [{"a": "x"}],
        "$comment": "why",
        "type": "object",
        "properties": {"a": {"type": "string", "default": "x"}},
    }
    assert holds(schema, {"a": "y"}) == {"a": "y"}
    assert to_model({"$ref": "#/$defs/x"}) is Any
    assert to_model({}) is Any
    assert holds(obj({"free": {"not": {"type": "string"}}}), {"free": [1]}) == {"free": [1]}


def test_models_are_cached_per_schema() -> None:
    first = to_model(CUSTOMER)
    assert to_model(dict(CUSTOMER)) is first
    assert to_model(obj({"other": {"type": "string"}})) is not first
    # ADK validates a node's data with the model itself.
    assert TypeAdapter(first).validate_python({"name": "Ada", "plan": "pro", "contacts": ["a@x.com"]}).model_dump() == {
        "name": "Ada",
        "plan": "pro",
        "contacts": ["a@x.com"],
    }
    assert issubclass(first, BaseModel)
