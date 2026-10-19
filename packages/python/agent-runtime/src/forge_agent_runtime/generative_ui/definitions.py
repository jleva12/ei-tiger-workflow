"""Team UI components' definitions (``forge.ui_component/v1``).

An organization writes a component as data: a composition of the base
components the chat draws (``present.catalog.json``), offered to its agents
beside them. A definition names the component (``key``: ``org.`` and
lowercase words joined by hyphens, never changed once published), says when
to use it (``description``: what the model is told), the inputs the model
gives it (``input_schema``: a small part of JSON Schema,
:func:`input_schema_problems`), the base components it's drawn as
(``template``, with the operators :mod:`.compiler` reads), and inputs to try
it with (``examples``).

Nothing in a definition runs: no code, styles or requests. Its inputs are
data, checked against its schema with ``forge_common.patterns``' bounded
checker, never the stock engine: the schema is the organization's, not the
package's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORMAT: Final = "forge.ui_component/v1"
#: What every team component's key starts with: base names never do.
PREFIX = "org."
KEY_PATTERN = re.compile(r"^org\.[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_KEY = 64
MAX_DISPLAY_NAME = 80
#: What the model is told of it, in every request of the agents offering it.
MAX_DESCRIPTION = 300
MAX_EXAMPLES = 20
#: How deep an input schema may nest.
MAX_SCHEMA_DEPTH = 8
#: The most values an enum may list.
MAX_ENUM = 100
#: The longest string an input may say it takes.
MAX_STRING = 10_000
#: An input's name: what pointers and placeholders spell, so nothing to escape.
INPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Diagnostic:
    """
    What's wrong with a definition, or a call of one.

    :ivar code: Stable, for clients: ``input_schema_unsupported``,
        ``component_input_invalid``, ``binding_missing``, …
    :ivar path: Where: ``template.children[2].value``, ``props.status``; empty for the whole.
    :ivar message: What's wrong, in words.
    """

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or 'The component'}: {self.message}"


class UiComponentDefinition(BaseModel):
    """
    A team component, as its organization wrote it.

    :ivar version: Which published version it is, set by the store that
        keeps it (and carried with exports); None for one not published.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["forge.ui_component/v1"] = FORMAT
    key: str = Field(max_length=MAX_KEY)
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION)
    input_schema: dict[str, Any]
    template: dict[str, Any]
    examples: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_EXAMPLES)
    version: int | None = Field(default=None, ge=1)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not KEY_PATTERN.match(value):
            raise ValueError(
                f'a key is "{PREFIX}" and lowercase words joined by hyphens, like "org.shipment-summary"'
            )
        return value

    @field_validator("display_name", "description")
    @classmethod
    def _words(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("it can't be blank")
        return value.strip()


#: What each type of input may say about itself, beside ``type``, ``description`` and ``title``.
_ALLOWED: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "string": frozenset({"enum", "maxLength", "minLength"}),
    "integer": frozenset({"enum", "minimum", "maximum"}),
    "number": frozenset({"enum", "minimum", "maximum"}),
    "boolean": frozenset(),
}
_COMMON = frozenset({"type", "description", "title"})


def fits_type(value: Any, kind: str) -> bool:
    """:return: Whether a value is of a scalar input type."""
    match kind:
        case "string":
            return isinstance(value, str)
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def input_schema_problems(schema: Any) -> list[Diagnostic]:
    """
    :return: Where an input schema leaves the part of JSON Schema team
        components take; none when it doesn't. It's an object of named
        inputs (``additionalProperties: false``), each an object, array,
        string (with ``maxLength``, or an ``enum``), integer, number or
        boolean, with their bounds and enums: no references, patterns,
        formats, combinators, defaults or recursion.
    """
    found: list[Diagnostic] = []

    def problem(where: str, message: str) -> None:
        found.append(Diagnostic("input_schema_unsupported", where, message))

    def visit(node: Any, where: str, depth: int) -> None:
        if depth > MAX_SCHEMA_DEPTH:
            problem(where, f"it nests deeper than {MAX_SCHEMA_DEPTH} levels")
            return
        if not isinstance(node, dict):
            problem(where, "it isn't a schema (an object)")
            return
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _ALLOWED:
            problem(where, 'give it a "type": object, array, string, integer, number or boolean')
            return
        extra = sorted(set(node) - _COMMON - _ALLOWED[kind])
        if extra:
            problem(where, f"a {kind} input can't say {', '.join(extra)} here")
        for key in ("description", "title"):
            if key in node and not isinstance(node[key], str):
                problem(f"{where}.{key}", "it's text")
        if "enum" in node:
            enum = node["enum"]
            if (
                not isinstance(enum, list)
                or not 0 < len(enum) <= MAX_ENUM
                or not all(fits_type(each, kind) for each in enum)
                or len({repr(each) for each in enum}) != len(enum)
            ):
                problem(f"{where}.enum", f"it lists 1 to {MAX_ENUM} different {kind} values")
        match kind:
            case "object":
                properties = node.get("properties")
                if not isinstance(properties, dict) or not properties:
                    problem(where, "an object input names its inputs in properties")
                    properties = {}
                if node.get("additionalProperties") is not False:
                    problem(where, 'an object input needs "additionalProperties": false')
                required = node.get("required", [])
                if not isinstance(required, list) or not all(
                    isinstance(name, str) and name in properties for name in required
                ):
                    problem(f"{where}.required", "it lists some of the object's own inputs")
                for name, sub in properties.items():
                    if not isinstance(name, str) or not INPUT_NAME.match(name):
                        problem(
                            f"{where}.properties",
                            f"{name!r} isn't an input name: letters, digits, _ and -, starting with a letter or _",
                        )
                        continue
                    visit(sub, f"{where}.properties.{name}", depth + 1)
            case "array":
                if "items" not in node:
                    problem(where, "a list input says what its items are (items)")
                else:
                    visit(node["items"], f"{where}.items", depth + 1)
                for key in ("minItems", "maxItems"):
                    if key in node and not _count(node[key]):
                        problem(f"{where}.{key}", "it's a whole number, 0 or more")
                if (
                    _count(node.get("minItems"))
                    and _count(node.get("maxItems"))
                    and node["minItems"] > node["maxItems"]
                ):
                    problem(where, "its minItems is more than its maxItems")
            case "string":
                if "maxLength" not in node and "enum" not in node:
                    problem(
                        where, "a text input says how long it may be (maxLength), or lists its values (enum)"
                    )
                for key in ("minLength", "maxLength"):
                    if key in node and (not _count(node[key]) or node[key] > MAX_STRING):
                        problem(f"{where}.{key}", f"it's a whole number from 0 to {MAX_STRING}")
            case "integer" | "number":
                for key in ("minimum", "maximum"):
                    if key in node and not fits_type(node[key], "number"):
                        problem(f"{where}.{key}", "it's a number")

    if not isinstance(schema, dict) or schema.get("type") != "object":
        problem("input_schema", "the inputs are an object (type: object) of named inputs")
        return found
    visit(schema, "input_schema", 0)
    return found
