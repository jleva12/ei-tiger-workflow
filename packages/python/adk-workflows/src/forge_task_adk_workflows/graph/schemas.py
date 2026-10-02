"""
JSON Schemas as Pydantic models, so ADK holds data to them: ADK validates and
coerces a node's data with Pydantic types only, and lets a JSON Schema dict
through unchecked.

:func:`to_model` converts what the web's schema builder (``features/json/lib/json-schema.ts``
``buildJsonSchema``) writes, and what stored documents may hold besides:

- an object: a model made with ``pydantic.create_model``, its required
  properties required and the others optional (``None`` when left out);
  ``additionalProperties: false`` forbids other keys, else they're kept.
  An object with no properties that takes any keys is a ``dict``.
- ``type`` string, integer, number, boolean and null, several of them, and the
  nullable form ``[t, "null"]`` (``Optional``); ``enum`` and ``const``, with
  or without a type (``Literal``).
- strings' ``minLength``, ``maxLength`` and ``pattern`` (a JSON Schema pattern
  matches anywhere in the text, as it does here); ``format`` (date, email,
  uri…) leaves a string a string, so data stays JSON.
- numbers' ``minimum``, ``maximum``, the exclusive forms (draft 2020-12's
  numbers and draft 4's flags) and ``multipleOf``. A number that's whole is
  handed on as an integer, as JSON has it.
- arrays' ``items``, ``minItems``, ``maxItems`` and ``uniqueItems``.
- nested objects (nested models); ``description`` (the field's).
- ``anyOf`` / ``oneOf``: any of them (a ``Union``), best effort.

Keywords that don't change what's accepted (``$schema``, ``$id``, ``title``,
``examples``, ``$comment``, ``default``…) are ignored; a schema that says
nothing this understands (``$ref``, ``not``, ``true``) takes anything.

Models are cached per schema, so the same schema is always the same model:
ADK refuses an edge whose ends' schemas differ, and two equal schemas are
one model.
"""

import json
import keyword
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    create_model,
)
from pydantic_core import PydanticCustomError

# What a model is called when its schema has no title.
DEFAULT_NAME = "Data"
# The most problems a refusal names.
MAX_PROBLEMS = 5
_NAME = re.compile(r"[^A-Za-z0-9_]+")


def to_model(json_schema: Any, name: str | None = None) -> Any:
    """
    The Pydantic type that holds data to a JSON Schema.

    :param json_schema: A JSON Schema.
    :param name: What its model is called; its ``title``, else ``Data``.
    :return: A ``BaseModel`` subclass for an object schema, else the type
        (``list[...]``, ``str``, a ``Union``…) ``TypeAdapter`` validates with;
        ``Any`` for a schema that takes anything.
    """
    return _cached(json.dumps(json_schema, ensure_ascii=False), name)


def is_model(value: Any) -> bool:
    """:return: Whether ``value`` is a Pydantic model class."""
    return isinstance(value, type) and issubclass(value, BaseModel)


def held_to(schema_type: Any, value: Any) -> Any:
    """
    ``value`` validated and coerced by a type :func:`to_model` made, as JSON.

    :param schema_type: The type.
    :param value: What to hold to it.
    :return: The value as the type has it: coerced (``"3"``: 3), keys it left
        out still out, keys it keeps kept.
    :raises ValidationError: When it doesn't fit.
    """
    adapter = _adapter(schema_type)
    return adapter.dump_python(adapter.validate_python(value), mode="json", exclude_unset=True)


def problems(error: ValidationError, what: str) -> str:
    """
    :param error: Why data didn't fit.
    :param what: What the data is, for a problem at its top.
    :return: Its first few problems, each with where (``items/0/qty: ...``).
    """
    found = [
        f"{'/'.join(str(part) for part in problem['loc']) or what}: {problem['msg']}"
        for problem in error.errors()[:MAX_PROBLEMS]
    ]
    more = error.error_count() - len(found)
    return "; ".join(found) + (f"; and {more} more" if more > 0 else "")


def _adapter(schema_type: Any) -> TypeAdapter[Any]:
    try:
        return _cached_adapter(schema_type)
    except TypeError:  # a type that can't be a cache key
        return TypeAdapter(schema_type)


@lru_cache(maxsize=512)
def _cached_adapter(schema_type: Any) -> TypeAdapter[Any]:
    return TypeAdapter(schema_type)


@lru_cache(maxsize=512)
def _cached(text: str, name: str | None) -> Any:
    schema = json.loads(text)
    title = schema.get("title") if isinstance(schema, dict) else None
    return _Converter().type_of(schema, _class_name(name or title or DEFAULT_NAME))


def _class_name(text: str) -> str:
    name = _NAME.sub("_", text).strip("_") or DEFAULT_NAME
    return name if name[0].isalpha() else f"{DEFAULT_NAME}_{name}"


def _whole(value: float) -> int | float:
    # A whole number as JSON writes it: 3, not 3.0.
    return int(value) if value.is_integer() else value


class _Converter:
    def type_of(self, schema: Any, name: str) -> Any:
        """The type for a schema; ``name`` names the models it makes."""
        if not isinstance(schema, dict):
            return Any
        for key in ("anyOf", "oneOf"):
            options = schema.get(key)
            if isinstance(options, list) and options:
                members = tuple(self.type_of(option, f"{name}_{index}") for index, option in enumerate(options))
                return members[0] if len(members) == 1 else Union[members]  # noqa: UP007
        declared = schema.get("type")
        types = (
            [declared]
            if isinstance(declared, str)
            else [t for t in declared if isinstance(t, str)]
            if isinstance(declared, list)
            else []
        )
        nullable = "null" in types
        types = [t for t in types if t != "null"]
        if "const" in schema:
            base = self.choice([schema["const"]])
        elif isinstance(schema.get("enum"), list) and schema["enum"]:
            values = [value for value in schema["enum"] if value is not None]
            nullable = nullable or len(values) < len(schema["enum"])
            base = self.choice(values) if values else type(None)
        elif not types:
            if isinstance(schema.get("properties"), dict):
                base = self.object(schema, name)
            elif "items" in schema:
                base = self.array(schema, name)
            else:
                return Any if not nullable else type(None)
        else:
            members = tuple(self.typed(kind, schema, name) for kind in types)
            base = members[0] if len(members) == 1 else Union[members]  # noqa: UP007
        return base | None if nullable and base is not Any else base

    def choice(self, values: list[Any]) -> Any:
        try:
            return Literal[tuple(values)]
        except TypeError:
            # An object or a list among them: not a Literal's.
            return Annotated[Any, AfterValidator(_one_of(values))]

    def typed(self, kind: str, schema: dict[str, Any], name: str) -> Any:
        match kind:
            case "string":
                return self.string(schema)
            case "integer":
                return Annotated[int, Field(**_bounds(schema))]
            case "number":
                return Annotated[
                    float,
                    Field(**_bounds(schema)),
                    PlainSerializer(_whole, return_type=int | float),
                ]
            case "boolean":
                return bool
            case "array":
                return self.array(schema, name)
            case "object":
                return self.object(schema, name)
        return Any

    def string(self, schema: dict[str, Any]) -> Any:
        limits = {
            key: schema[source]
            for key, source in (
                ("min_length", "minLength"),
                ("max_length", "maxLength"),
            )
            if isinstance(schema.get(source), int)
        }
        pattern = schema.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return Annotated[str, StringConstraints(**limits)]
        constrained = Annotated[str, StringConstraints(**limits, pattern=pattern)]
        try:
            TypeAdapter(constrained)
            return constrained
        except Exception:  # noqa: BLE001 - a pattern Rust's regex doesn't read
            return Annotated[str, StringConstraints(**limits), AfterValidator(_matching(pattern))]

    def array(self, schema: dict[str, Any], name: str) -> Any:
        items = schema.get("items")
        item = self.type_of(items, f"{name}_item") if isinstance(items, dict) else Any
        limits = {
            key: schema[source]
            for key, source in (("min_length", "minItems"), ("max_length", "maxItems"))
            if isinstance(schema.get(source), int)
        }
        extras: list[Any] = [Field(**limits)]
        if schema.get("uniqueItems") is True:
            extras.append(AfterValidator(_unique))
        return Annotated[list[item], *extras]  # type: ignore[valid-type]

    def object(self, schema: dict[str, Any], name: str) -> Any:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        closed = schema.get("additionalProperties") is False
        if not properties and not closed:
            return dict[str, Any]
        required = schema.get("required")
        required = set(required) if isinstance(required, list) else set()
        fields: dict[str, Any] = {}
        for index, (key, value) in enumerate(properties.items()):
            field_name = key if _usable(key) else f"field_{index}"
            settings: dict[str, Any] = {}
            if field_name != key:
                settings["alias"] = key
            if isinstance(value, dict) and isinstance(value.get("description"), str):
                settings["description"] = value["description"]
            kind = self.type_of(value, f"{name}_{_class_name(key)}")
            if key in required:
                fields[field_name] = (kind, Field(**settings))
            else:
                fields[field_name] = (kind, Field(None, **settings))
        description = schema.get("description")
        return create_model(
            name,
            __config__=ConfigDict(
                extra="forbid" if closed else "allow",
                serialize_by_alias=True,
                validate_by_alias=True,
                validate_by_name=False,
                protected_namespaces=(),
            ),
            __doc__=description if isinstance(description, str) else None,
            **fields,
        )


def _usable(key: str) -> bool:
    # A property whose name can be the model's field's; else it's an alias.
    return (
        key.isidentifier()
        and not keyword.iskeyword(key)
        and not key.startswith(("_", "model_"))
        and not hasattr(BaseModel, key)
    )


def _bounds(schema: dict[str, Any]) -> dict[str, Any]:
    def number(key: str) -> float | None:
        value = schema.get(key)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    bounds: dict[str, Any] = {}
    minimum, maximum = number("minimum"), number("maximum")
    # Draft 4 has the exclusive forms as flags on minimum and maximum.
    if schema.get("exclusiveMinimum") is True and minimum is not None:
        bounds["gt"] = minimum
    elif minimum is not None:
        bounds["ge"] = minimum
    if schema.get("exclusiveMaximum") is True and maximum is not None:
        bounds["lt"] = maximum
    elif maximum is not None:
        bounds["le"] = maximum
    if number("exclusiveMinimum") is not None:
        bounds["gt"] = number("exclusiveMinimum")
    if number("exclusiveMaximum") is not None:
        bounds["lt"] = number("exclusiveMaximum")
    if number("multipleOf"):
        bounds["multiple_of"] = number("multipleOf")
    return bounds


def _one_of(values: list[Any]) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if value not in values:
            raise PydanticCustomError("enum", "Input should be one of {expected}", {"expected": values})
        return value

    return check


def _matching(pattern: str) -> Callable[[str], str]:
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        if not compiled.search(value):
            raise PydanticCustomError(
                "string_pattern_mismatch",
                "String should match pattern '{pattern}'",
                {"pattern": pattern},
            )
        return value

    return check


def _unique(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            raise PydanticCustomError("unique_items", "Items should be unique")
        seen.add(key)
    return items
