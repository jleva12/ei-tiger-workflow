"""Team UI components drawn as base ones: the one compiler preview, publishing
and the ``present`` tool share.

A definition's ``template`` is a ``present`` tree of base components, where
these stand for what the call's ``props`` hold (A2UI's data binding, spelled
as operators inside Forge's nested tree):

- ``{"$bind": "/pointer"}``, a prop's value or an action's ``text``: the
  input there, as it is (arrays too, for a Table's rows or Choices' options);
  ``"else"`` beside it is what it is when that input is missing.
- ``{"$format": "Order {/order_number}"}``, a prop, an action's ``text`` or
  a child: text with inputs in it, each a string, number or boolean;
  ``{/pointer?}`` is empty when its input is missing; ``{{`` and ``}}`` are
  braces.
- ``"$when": "/pointer"`` on a node in a ``children`` list: the node is
  drawn only when that input is there, and isn't ``null``, ``false``, ``""``
  or ``[]``.
- ``"children": {"$each": "/pointer", "max": N, "template": {…}}``: the
  template drawn for each item of a list input (at most ``N``, 100 at most,
  and the input's ``maxItems`` no more than that). In it, a pointer without
  a leading ``/`` (``name``, or ``""`` for the item itself) is the item's.

Pointers are JSON Pointers into the inputs. What the inputs hold is data:
never read as operators or nodes, whatever it looks like. Types, actions'
types and the shape of ``children`` lists come only from the template, and
any other ``$`` key in it is refused (``$type`` too, though chats read it
as ``_type``).

:func:`compile_component` checks a definition as far as it can without
inputs; :func:`check_definition` (publishing, preview) also draws each of
its examples and holds the result to the base components' schema.
:meth:`CompiledComponent.expand` draws one call's inputs.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from forge_agent_runtime.generative_ui.definitions import (
    Diagnostic,
    UiComponentDefinition,
    input_schema_problems,
)
from forge_agent_runtime.present import known_components, present_catalog, problems_of
from forge_common.patterns import schema_errors

#: The most diagnostics one check reports.
MAX_DIAGNOSTICS = 20
#: The longest ``$format`` text.
MAX_FORMAT = 2_000
_SCALARS = frozenset({"string", "integer", "number", "boolean"})


@dataclass(frozen=True)
class Limits:
    """How big a team component may be, written and drawn; server settings."""

    definition_bytes: int = 64 * 1024
    template_nodes: int = 200
    template_depth: int = 16
    each_items: int = 100
    tree_bytes: int = 64 * 1024
    tree_nodes: int = 500


LIMITS = Limits()


@dataclass(frozen=True)
class Capabilities:
    """
    What a component's template has, which decides what its agents are told
    (from its node types, never the author's say).

    :ivar has_choices: Choices: a click comes back as the person's message.
    :ivar has_actions: A control or button that sends something back.
    :ivar has_forms: A Form, or a Card made one (``asForm``).
    """

    has_choices: bool
    has_actions: bool
    has_forms: bool


class ComponentError(ValueError):
    """A definition that can't be used, or a call of it that can't be drawn, and why."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics[:MAX_DIAGNOSTICS]
        super().__init__("; ".join(map(str, self.diagnostics)))

    @property
    def problems(self) -> list[str]:
        """:return: Its diagnostics, as the model is told them."""
        return [str(each) for each in self.diagnostics]


@dataclass(frozen=True)
class Pointer:
    """A JSON Pointer into the inputs, or (``absolute`` False) into the item an ``$each`` draws."""

    absolute: bool
    segments: tuple[str, ...]


def parse_pointer(text: Any) -> Pointer | None:
    """:return: The pointer, or None when it isn't one."""
    if not isinstance(text, str):
        return None
    absolute = text.startswith("/")
    body = text[1:] if absolute else text
    if not body:
        # "/" names an input called "" (there's none); "" is the item itself.
        return None if absolute else Pointer(False, ())
    segments: list[str] = []
    for raw in body.split("/"):
        if not raw or "~" in raw.replace("~0", "").replace("~1", ""):
            return None
        segments.append(raw.replace("~1", "/").replace("~0", "~"))
    return Pointer(absolute, tuple(segments))


@cache
def split_format(text: str) -> tuple[str | tuple[str, bool], ...]:
    """
    :return: A ``$format``'s text and placeholders, in order: text as it is,
        placeholders as (pointer, optional).
    :raises ValueError: A brace that's neither doubled nor a placeholder's.
    """
    pieces: list[str | tuple[str, bool]] = []
    literal: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char in "{}" and text[i + 1 : i + 2] == char:
            literal.append(char)
            i += 2
        elif char == "}":
            raise ValueError("a } that closes nothing: write }} for a brace")
        elif char == "{":
            end = text.find("}", i + 1)
            inner = text[i + 1 : end] if end >= 0 else ""
            if end < 0 or "{" in inner or not inner.rstrip("?"):
                raise ValueError("a { that doesn't hold a pointer: write {{ for a brace")
            if literal:
                pieces.append("".join(literal))
                literal = []
            pieces.append((inner.removesuffix("?"), inner.endswith("?")))
            i = end + 1
        else:
            literal.append(char)
            i += 1
    if literal:
        pieces.append("".join(literal))
    return tuple(pieces)


@dataclass(frozen=True)
class _Scope:
    """Where relative pointers start, as the checker sees it: an ``$each``'s item."""

    schema: Mapping[str, Any]
    #: The item's place in the inputs, ``*`` for any item of a list.
    path: tuple[str, ...]


class _Checker:
    """Reads a template once, without inputs: what can be known before drawing it."""

    def __init__(self, definition: UiComponentDefinition, limits: Limits) -> None:
        self.inputs = definition.input_schema
        self.limits = limits
        catalog = present_catalog()
        self.components: Mapping[str, Any] = catalog["components"]
        self.acting = frozenset(catalog["acting"])
        self.found: list[Diagnostic] = []
        self.nodes = 0
        self.types: set[str] = set()
        self.acts = False
        self.forms = False

    def problem(self, code: str, where: str, message: str) -> None:
        self.found.append(Diagnostic(code, where, message))

    # -- Pointers --

    def resolve(
        self, text: Any, where: str, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> tuple[Mapping[str, Any], tuple[str, ...], bool] | None:
        """
        :return: The schema of the input a pointer names, its place, and
            whether it's always there (required all the way down, or under a
            ``$when`` on it); None (told) when it names none.
        """
        pointer = parse_pointer(text)
        if pointer is None:
            self.problem("pointer_invalid", where, f"{text!r} isn't a pointer, like /order_number")
            return None
        schema: Mapping[str, Any]
        path: tuple[str, ...]
        if pointer.absolute:
            schema, path = self.inputs, ()
        elif scope is None:
            self.problem(
                "pointer_invalid", where, f"{text!r} starts without /: that's an item's, inside an $each only"
            )
            return None
        else:
            schema, path = scope.schema, scope.path
        always = True
        for segment in pointer.segments:
            kind = schema.get("type")
            if kind == "object" and segment in (schema.get("properties") or {}):
                if segment not in (schema.get("required") or []) and (*path, segment) not in present:
                    always = False
                schema = schema["properties"][segment]
            elif kind == "array" and segment.isdigit():
                if int(segment) >= int(schema.get("minItems") or 0) and (*path, segment) not in present:
                    always = False
                schema = schema["items"]
            else:
                self.problem("pointer_invalid", where, f"{text!r} names no input")
                return None
            path = (*path, segment)
        return schema, path, always

    # -- Nodes --

    def node(
        self,
        node: Any,
        where: str,
        depth: int,
        scope: _Scope | None,
        present: frozenset[tuple[str, ...]],
        *,
        root: bool = False,
    ) -> None:
        if depth > self.limits.template_depth:
            self.problem(
                "template_too_large", where, f"it nests deeper than {self.limits.template_depth} levels"
            )
            return
        self.nodes += 1
        if self.nodes == self.limits.template_nodes + 1:
            self.problem("template_too_large", where, f"it has more than {self.limits.template_nodes} nodes")
        if not isinstance(node, dict) or "_type" not in node:
            self.problem("definition_invalid", where, "a node is an object naming its component in _type")
            return
        kind = node["_type"]
        if not isinstance(kind, str) or kind not in self.components:
            team = isinstance(kind, str) and kind.startswith("org.")
            self.problem(
                "base_component_unsupported",
                f"{where}._type",
                "a team component can't hold another team component yet"
                if team
                else f"{kind!r} isn't a base component",
            )
            return
        self.types.add(kind)
        self.acts = self.acts or kind in self.acting
        self.forms = self.forms or kind == "Form" or (kind == "Card" and node.get("asForm") is True)
        if "$when" in node:
            if root:
                self.problem(
                    "operator_unsupported", f"{where}.$when", "the root is always drawn: $when is for a child"
                )
            else:
                found = self.resolve(node["$when"], f"{where}.$when", scope, present)
                if found is not None:
                    present = present | {found[1]}
        props = self.components[kind]["properties"]
        for key, value in node.items():
            at = f"{where}.{key}"
            if key in ("_type", "$when"):
                continue
            if key == "children":
                self.children(value, at, depth, scope, present)
            elif key == "_action":
                self.action(value, at, scope, present)
            elif key.startswith("$"):
                self.problem("operator_unsupported", at, f"{key} isn't an operator a node takes")
            elif key not in props:
                self.problem("prop_unsupported", at, f"{kind} has no prop {key!r}")
            else:
                self.value(value, at, scope, present)
                if _literal(value):
                    self.literal(value, at, props[key])

    def children(
        self, value: Any, where: str, depth: int, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> None:
        if isinstance(value, dict) and "$each" in value:
            self.each(value, where, depth, scope, present)
            return
        if not isinstance(value, list):
            self.problem("definition_invalid", where, "children are a list of nodes and text, or one $each")
            return
        for index, child in enumerate(value):
            at = f"{where}[{index}]"
            if isinstance(child, str):
                continue
            if isinstance(child, dict) and "$format" in child:
                self.format(child, at, scope, present)
            elif isinstance(child, dict) and "_type" in child:
                self.node(child, at, depth + 1, scope, present)
            else:
                self.problem("definition_invalid", at, "a child is text, a $format or a node")

    def each(
        self,
        value: dict[str, Any],
        where: str,
        depth: int,
        scope: _Scope | None,
        present: frozenset[tuple[str, ...]],
    ) -> None:
        extra = sorted(set(value) - {"$each", "max", "template"})
        if extra:
            self.problem(
                "operator_unsupported",
                where,
                f"an $each takes $each, max and template, not {', '.join(extra)}",
            )
        limit = value.get("max", self.limits.each_items)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self.limits.each_items:
            self.problem(
                "each_over_limit", f"{where}.max", f"it's a whole number from 1 to {self.limits.each_items}"
            )
            limit = self.limits.each_items
        found = self.resolve(value["$each"], f"{where}.$each", scope, present)
        if found is not None:
            schema, path, _ = found
            if schema.get("type") != "array":
                self.problem("pointer_invalid", f"{where}.$each", f"{value['$each']!r} isn't a list input")
                return
            most = schema.get("maxItems")
            if not isinstance(most, int) or most > limit:
                self.problem(
                    "each_over_limit",
                    f"{where}.$each",
                    f"its list input needs a maxItems of at most {limit}, the most it draws",
                )
            template = value.get("template")
            if not isinstance(template, dict):
                self.problem(
                    "definition_invalid", f"{where}.template", "an $each draws one node for each item"
                )
                return
            item = _Scope(schema["items"], (*path, "*"))
            self.node(template, f"{where}.template", depth + 1, item, present)

    def action(
        self, value: Any, where: str, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> None:
        self.acts = True
        if not isinstance(value, dict):
            self.problem("definition_invalid", where, 'an action is {"type": "reply", "text": "..."}')
            return
        extra = sorted(set(value) - {"type", "text"})
        if extra:
            self.problem("prop_unsupported", where, f"an action has type and text, not {', '.join(extra)}")
        if value.get("type") != "reply":
            code = "operator_unsupported" if isinstance(value.get("type"), dict) else "definition_invalid"
            self.problem(code, f"{where}.type", 'an action\'s type is "reply", as it is')
        if "text" in value and not isinstance(value["text"], str):
            if isinstance(value["text"], dict) and (
                {"$bind"} <= set(value["text"]) or "$format" in value["text"]
            ):
                self.value(value["text"], f"{where}.text", scope, present)
            else:
                self.problem(
                    "definition_invalid", f"{where}.text", "an action's text is text, a $bind or a $format"
                )

    # -- Values --

    def value(
        self, value: Any, where: str, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                self.value(item, f"{where}[{index}]", scope, present)
            return
        if not isinstance(value, dict):
            return
        if "$bind" in value:
            self.bind(value, where, scope, present)
        elif "$format" in value:
            self.format(value, where, scope, present)
        else:
            for key, item in value.items():
                if key.startswith("$"):
                    self.problem(
                        "operator_unsupported", f"{where}.{key}", f"{key} isn't an operator a value takes"
                    )
                elif key == "_action":
                    self.action(item, f"{where}._action", scope, present)
                else:
                    self.value(item, f"{where}.{key}", scope, present)

    def bind(
        self, value: dict[str, Any], where: str, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> None:
        extra = sorted(set(value) - {"$bind", "else"})
        if extra:
            self.problem(
                "operator_unsupported", where, f"a $bind takes $bind and else, not {', '.join(extra)}"
            )
        if "else" in value and not _literal(value["else"]):
            self.problem(
                "operator_unsupported", f"{where}.else", "an else is a value as it is, without operators"
            )
        found = self.resolve(value["$bind"], f"{where}.$bind", scope, present)
        if found is not None and not found[2] and "else" not in value:
            self.problem(
                "binding_missing",
                f"{where}.$bind",
                f"{value['$bind']!r} may be missing: put its node under a $when on it, or give the $bind an else",
            )

    def format(
        self, value: dict[str, Any], where: str, scope: _Scope | None, present: frozenset[tuple[str, ...]]
    ) -> None:
        if set(value) != {"$format"}:
            self.problem("operator_unsupported", where, "a $format takes nothing beside it")
        text = value["$format"]
        if not isinstance(text, str) or len(text) > MAX_FORMAT:
            self.problem(
                "definition_invalid", f"{where}.$format", f"it's text, at most {MAX_FORMAT} characters"
            )
            return
        try:
            pieces = split_format(text)
        except ValueError as error:
            self.problem("definition_invalid", f"{where}.$format", str(error))
            return
        for piece in pieces:
            if isinstance(piece, str):
                continue
            pointer, optional = piece
            found = self.resolve(pointer, f"{where}.$format", scope, present)
            if found is None:
                continue
            schema, _, always = found
            if schema.get("type") not in _SCALARS:
                self.problem(
                    "format_value_not_scalar",
                    f"{where}.$format",
                    f"{{{pointer}}} is a {schema.get('type')}: text takes a string, number or boolean",
                )
            elif not always and not optional:
                self.problem(
                    "binding_missing",
                    f"{where}.$format",
                    f"{{{pointer}}} may be missing: write {{{pointer}?}}, or put the node under a $when on it",
                )

    def literal(self, value: Any, where: str, schema: Mapping[str, Any]) -> None:
        """A prop written as it is: held to the base component's schema for it (the package's own)."""
        for error in _prop_validator(json.dumps(schema, sort_keys=True)).iter_errors(value):
            self.problem("prop_invalid", where, error.message)
            break


@cache
def _prop_validator(schema: str) -> Draft202012Validator:
    # A base component's prop, from the package's catalog: the stock engine is safe here.
    return Draft202012Validator(json.loads(schema))


def _literal(value: Any) -> bool:
    """:return: Whether a value holds no operator (no ``$`` key) anywhere."""
    if isinstance(value, dict):
        return not any(key.startswith("$") for key in value) and all(map(_literal, value.values()))
    if isinstance(value, list):
        return all(map(_literal, value))
    return True


#: What a pointer finds when there's nothing there.
_MISSING: Any = object()


def _drawn(found: Any) -> bool:
    """:return: Whether a ``$when``'s input lets its node be drawn (0 does: only these don't)."""
    return not (found is _MISSING or found is False or found == "" or found == [])


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _path(base: str, parts: Iterable[Any]) -> str:
    where = base + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)
    return where.lstrip(".")


class _Drawer:
    """Draws one call's inputs through a checked template."""

    def __init__(self, props: Any, where: str, limits: Limits) -> None:
        self.props = props
        self.where = where
        self.limits = limits
        self.nodes = 0

    def fail(self, code: str, message: str) -> ComponentError:
        return ComponentError([Diagnostic(code, self.where, message)])

    def lookup(self, text: str, item: Any) -> Any:
        pointer = parse_pointer(text)
        assert pointer is not None, "The template was checked."
        found = self.props if pointer.absolute else item
        for segment in pointer.segments:
            if isinstance(found, dict) and segment in found:
                found = found[segment]
            elif isinstance(found, list) and segment.isdigit() and int(segment) < len(found):
                found = found[int(segment)]
            else:
                return _MISSING
        return _MISSING if found is None else found

    def node(self, node: dict[str, Any], item: Any) -> dict[str, Any] | None:
        if "$when" in node and not _drawn(self.lookup(node["$when"], item)):
            return None
        self.nodes += 1
        if self.nodes > self.limits.tree_nodes:
            raise self.fail("tree_too_large", f"it draws more than {self.limits.tree_nodes} nodes")
        drawn: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$when":
                continue
            if key == "_type":
                drawn[key] = value
            elif key == "children":
                drawn[key] = self.children(value, item)
            else:
                drawn[key] = self.value(value, item)
        return drawn

    def children(self, value: Any, item: Any) -> list[Any]:
        drawn: list[Any] = []
        if isinstance(value, dict):
            items = self.lookup(value["$each"], item)
            items = [] if items is _MISSING else items
            limit = value.get("max", self.limits.each_items)
            if len(items) > limit:
                raise self.fail(
                    "each_over_limit", f"{value['$each']} has {len(items)} items: it draws {limit} at most"
                )
            for each in items:
                if (child := self.node(value["template"], each)) is not None:
                    drawn.append(child)
            return drawn
        for child in value:
            if isinstance(child, str):
                drawn.append(child)
            elif "$format" in child:
                drawn.append(self.format(child["$format"], item))
            elif (node := self.node(child, item)) is not None:
                drawn.append(node)
        return drawn

    def value(self, value: Any, item: Any) -> Any:
        if isinstance(value, list):
            return [self.value(each, item) for each in value]
        if not isinstance(value, dict):
            return value
        if "$bind" in value:
            found = self.lookup(value["$bind"], item)
            if found is _MISSING:
                if "else" in value:
                    return copy.deepcopy(value["else"])
                raise self.fail("binding_missing", f"{value['$bind']} is missing")
            return copy.deepcopy(found)
        if "$format" in value:
            return self.format(value["$format"], item)
        return {key: self.value(each, item) for key, each in value.items()}

    def format(self, text: str, item: Any) -> str:
        out: list[str] = []
        for piece in split_format(text):
            if isinstance(piece, str):
                out.append(piece)
                continue
            pointer, optional = piece
            found = self.lookup(pointer, item)
            if found is _MISSING:
                if not optional:
                    raise self.fail("binding_missing", f"{{{pointer}}} is missing")
                continue
            if isinstance(found, dict | list):
                raise self.fail("format_value_not_scalar", f"{{{pointer}}} isn't a string, number or boolean")
            out.append(_text(found))
        return "".join(out)


@dataclass(frozen=True)
class CompiledComponent:
    """
    A team component checked and ready to draw: what the ``present`` tool
    offers beside the base components.

    :ivar base_components: The base components its template draws.
    """

    definition: UiComponentDefinition
    capabilities: Capabilities
    base_components: frozenset[str]
    limits: Limits = LIMITS

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def version(self) -> int | None:
        return self.definition.version

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.definition.input_schema

    def expand(self, props: Any, *, where: str = "props") -> Any:
        """
        :param props: A call's inputs for it.
        :param where: Where they are in the call, for what's told.
        :return: The base tree it draws them as.
        :raises ComponentError: They don't fit its inputs, or draw too much.
        """
        errors = sorted(schema_errors(self.input_schema, props), key=lambda error: list(error.absolute_path))
        if errors:
            raise ComponentError(
                [
                    Diagnostic("component_input_invalid", _path(where, error.absolute_path), error.message)
                    for error in errors
                ]
            )
        drawer = _Drawer(props, where, self.limits)
        tree = drawer.node(self.definition.template, _MISSING)
        assert tree is not None, "The root has no $when."
        size = len(json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode())
        if size > self.limits.tree_bytes:
            raise drawer.fail("tree_too_large", f"it draws {size} bytes: {self.limits.tree_bytes} at most")
        found = _prop_misfits(tree, where)
        if found:
            raise ComponentError(found)
        return tree


def _prop_misfits(tree: Any, where: str) -> list[Diagnostic]:
    """
    :return: Where a drawn tree's nodes have props their own component
        doesn't take as they are (an input bound into one). The ``present``
        schema can't tell: it merges every component's props into one node,
        so another component's prop of the same name would let it through.
    """
    components: Mapping[str, Any] = present_catalog()["components"]
    found: list[Diagnostic] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 64 or len(found) >= MAX_DIAGNOSTICS:
            return
        if isinstance(node, list):
            for each in node:
                visit(each, depth + 1)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("_type")
        props = components[kind]["properties"] if isinstance(kind, str) and kind in components else {}
        for key, value in node.items():
            if key not in props:
                continue
            for error in _prop_validator(json.dumps(props[key], sort_keys=True)).iter_errors(value):
                inside = _path("", error.absolute_path)
                found.append(
                    Diagnostic(
                        "prop_invalid",
                        where,
                        f"drawn, its {kind}'s {key}{'.' + inside if inside else ''}: {error.message}",
                    )
                )
                break
        visit(node.get("children"), depth + 1)

    visit(tree, 0)
    return found


def _parsed(definition: UiComponentDefinition | Mapping[str, Any]) -> UiComponentDefinition:
    """:raises ComponentError: It isn't a definition."""
    if isinstance(definition, UiComponentDefinition):
        return definition
    try:
        return UiComponentDefinition.model_validate(definition)
    except ValidationError as error:
        raise ComponentError(
            [Diagnostic("definition_invalid", _path("", each["loc"]), each["msg"]) for each in error.errors()]
        ) from error


def compile_component(
    definition: UiComponentDefinition | Mapping[str, Any], limits: Limits = LIMITS
) -> CompiledComponent:
    """
    :return: The definition, checked as far as it can be without inputs.
    :raises ComponentError: It can't be used: what's wrong, and where.
    """
    parsed = _parsed(definition)
    size = len(parsed.model_dump_json().encode())
    if size > limits.definition_bytes:
        raise ComponentError(
            [Diagnostic("definition_too_large", "", f"it's {size} bytes: {limits.definition_bytes} at most")]
        )
    found = input_schema_problems(parsed.input_schema)
    if found:
        raise ComponentError(found)
    checker = _Checker(parsed, limits)
    checker.node(parsed.template, "template", 1, None, frozenset(), root=True)
    if checker.found:
        raise ComponentError(checker.found)
    return CompiledComponent(
        parsed,
        Capabilities(
            has_choices="Choices" in checker.types,
            has_actions=checker.acts,
            has_forms=checker.forms,
        ),
        frozenset(checker.types),
        limits,
    )


def check_definition(
    definition: UiComponentDefinition | Mapping[str, Any], limits: Limits = LIMITS
) -> list[Diagnostic]:
    """
    Everything publishing holds a definition to: :func:`compile_component`'s
    checks, then each example drawn and held to the base components' schema
    (every base component, as chats draw them: a composite may use some its
    agents don't pick) and to buttons that would do nothing.

    :return: What's wrong, and where; none when it can be published.
    """
    try:
        compiled = compile_component(definition, limits)
    except ComponentError as error:
        return error.diagnostics
    examples = compiled.definition.examples
    if not examples:
        return [
            Diagnostic("definition_invalid", "examples", "give it an example: preview and publishing draw it")
        ]
    found: list[Diagnostic] = []
    for index, example in enumerate(examples):
        where = f"examples[{index}]"
        try:
            tree = compiled.expand(example, where=where)
        except ComponentError as error:
            found.extend(error.diagnostics)
            continue
        found.extend(
            Diagnostic("tree_invalid", where, f"drawn, {problem}")
            for problem in problems_of(tree, known_components())
        )
    return found[:MAX_DIAGNOSTICS]
