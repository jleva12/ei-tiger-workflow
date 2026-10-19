"""An HTTP tool's URL and body as templates: what one call sends, from the
model's arguments and the run's values.

They hold three kinds of placeholder:

- ``{name}``: the model's argument of that name. The tool's parameters are
  the ones its URL and body name (:func:`parameters_of`): one named nowhere
  isn't offered to the model, and isn't sent.
- ``{{ expression }}``: one of the run's values, in JSONata, filled in as the
  tool's called: a workflow's data where its agent runs in one
  (``RuntimeServices.tool_values``), else the chat's ``request`` and
  ``state``, as instructions read them.
- ``${NAME}``: a secret or setting, filled in from the environment when the
  tool's built.

In the URL each is written in as text. A model's argument is
percent-encoded, so it can't add a path or a query of its own; a run's value
goes in as it is (it may be a base URL: ``$encodeUrlComponent`` encodes
one). A query parameter that is one argument, ``?status={status}``, is left
out when the model doesn't send it; any other argument the URL names must
be sent. Where a URL calls is the tool's author's to say, not the model's:
an argument can't be in its scheme or host, and a run's value there is only
for a tool sending no headers of its own (:meth:`UrlTemplate.check_origin`).

Query parameters may also be listed by name and value (:func:`parse_query`),
each a template: they're filled in, then each name and value is
percent-encoded whole and added to the URL's query
(``forge_common.urls.with_query``), so a value arrives as it was filled in,
``&``, ``=`` and spaces included. A value that is one placeholder and comes to
nothing (an argument the model didn't send, a run's value that's null) leaves
its parameter out; a list sends it once per item.

The body (POST, PUT, PATCH) is JSON. A placeholder that is a whole value,
``{order_id}`` or ``"{order_id}"``, sends what it gives as it is (a number
stays a number, an object an object); one inside a string is written into
it. A whole-value argument the model doesn't send leaves its key (or its
list item) out.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from forge_agent_runtime.templating import TemplateError, as_text, evaluate_async, template_data
from forge_common.adk import ToolFailure

if TYPE_CHECKING:
    from google.adk.tools.tool_context import ToolContext

    from forge_agent_runtime.services import ToolValues

#: A placeholder: ``${NAME}`` (the environment), ``{{ expression }}`` (a
#: run's value) or ``{name}`` (an argument), in that order at one place.
PLACEHOLDER = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{(.*?)\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}",
    re.DOTALL,
)
#: An argument's name, as ``{name}`` holds it.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The methods that send a body.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class TemplateProblem(ValueError):
    """A URL or body that can't be a template as it's written, and why."""


@dataclass(frozen=True)
class Argument:
    """``{name}``: the model's argument."""

    name: str


@dataclass(frozen=True)
class Value:
    """``{{ expression }}``: one of the run's values."""

    expression: str


Part = str | Argument | Value


@dataclass(frozen=True)
class Text:
    """A string with placeholders in it: its literal text and placeholders, in order."""

    parts: tuple[Part, ...]


@dataclass(frozen=True)
class Whole:
    """A body value that's one placeholder: what it gives goes in as it is."""

    part: Argument | Value


class _Omitted:
    """A whole-value argument the model didn't send: its key or item is left out."""


OMITTED = _Omitted()


def parse_text(text: str, fill: Callable[[str], str], *, what: str) -> tuple[Part, ...]:
    """
    :param fill: Gives ``${NAME}``'s value.
    :param what: What's being read, for errors.
    :return: The text as literal strings and placeholders, adjacent strings joined.
    :raises TemplateProblem: It has an empty ``{{ }}``.
    """
    parts: list[Part] = []

    def literal(piece: str) -> None:
        if not piece:
            return
        if parts and isinstance(parts[-1], str):
            parts[-1] += piece
        else:
            parts.append(piece)

    at = 0
    for match in PLACEHOLDER.finditer(text):
        literal(text[at : match.start()])
        setting, expression, name = match.groups()
        if setting is not None:
            literal(fill(setting))
        elif expression is not None:
            if not expression.strip():
                raise TemplateProblem(f"{what} has an empty {{{{ }}}}.")
            parts.append(Value(expression.strip()))
        else:
            parts.append(Argument(name))
        at = match.end()
    literal(text[at:])
    return tuple(parts)


def _droppable(pair: Sequence[Part]) -> Argument | None:
    """:return: The argument a query pair is, ``name={name}``; None for any other pair."""
    if (
        len(pair) == 2
        and isinstance(pair[0], str)
        and pair[0].endswith("=")
        and isinstance(pair[1], Argument)
    ):
        return pair[1]
    return None


@dataclass(frozen=True)
class QueryRow:
    """A listed query parameter: its name and its value, as literal text and placeholders."""

    name: tuple[Part, ...]
    value: tuple[Part, ...]

    @property
    def whole(self) -> Argument | Value | None:
        """What its value is when it's one placeholder and nothing else (but spaces)."""
        placeholders = [part for part in self.value if not isinstance(part, str)]
        literal = "".join(part for part in self.value if isinstance(part, str))
        return placeholders[0] if len(placeholders) == 1 and not literal.strip() else None


def parse_query(
    rows: Any, fill: Callable[[str], str], *, what: str = "Its query parameter"
) -> tuple[QueryRow, ...]:
    """
    :param rows: The listed query parameters, ``[{name, value}]``; one without a name is left out.
    :return: Each as a :class:`QueryRow`.
    :raises TemplateProblem: One has an empty ``{{ }}``.
    """
    out: list[QueryRow] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        where = f"{what} {name!r}"
        out.append(
            QueryRow(
                parse_text(name, fill, what=where), parse_text(str(row.get("value") or ""), fill, what=where)
            )
        )
    return tuple(out)


def query_arguments(rows: Sequence[QueryRow]) -> tuple[list[str], set[str]]:
    """:return: The arguments listed query parameters name, in order, and those they must have: all but a value that's one argument."""
    names: list[str] = []
    required: set[str] = set()
    for row in rows:
        whole = row.whole
        for part in (*row.name, *row.value):
            if isinstance(part, Argument):
                if part.name not in names:
                    names.append(part.name)
                if part is not whole:
                    required.add(part.name)
    return names, required


@dataclass(frozen=True)
class UrlTemplate:
    """A URL: what comes before its query, and its query's ``name=value`` pairs."""

    path: tuple[Part, ...]
    query: tuple[tuple[Part, ...], ...]

    @classmethod
    def parse(cls, url: str, fill: Callable[[str], str], *, what: str = "Its URL") -> UrlTemplate:
        path: list[Part] = []
        query: list[list[Part]] | None = None
        for part in parse_text(url, fill, what=what):
            if query is None:
                if isinstance(part, str) and "?" in part:
                    before, after = part.split("?", 1)
                    if before:
                        path.append(before)
                    query = [[]]
                    part = after
                else:
                    path.append(part)
                    continue
            if isinstance(part, str):
                pieces = part.split("&")
                if pieces[0]:
                    query[-1].append(pieces[0])
                for piece in pieces[1:]:
                    query.append([piece] if piece else [])
            else:
                query[-1].append(part)
        return cls(tuple(path), tuple(tuple(pair) for pair in query or () if pair))

    def check_origin(self, *, headers: bool, what: str = "Its URL") -> None:
        """
        Check where it sends requests: its scheme and host are written out,
        or, for a tool sending no headers, one of the run's values gives
        them. The model's arguments never do: an argument there would send
        the request, and the secrets its headers carry, wherever the model
        was talked into.

        :param headers: The tool sends headers of its own, which go only to
            the host its URL names as it's written.
        :raises TemplateProblem: An argument in its scheme or host; a run's
            value there, with headers.
        """
        lead = self.path[0] if self.path and isinstance(self.path[0], str) else ""
        scheme, written, rest = lead.partition("://")
        if all(isinstance(part, str) for part in self.path) or (
            written and scheme and any(mark in rest for mark in "/?#\\")
        ):
            return
        first = next(part for part in self.path if not isinstance(part, str))
        if isinstance(first, Argument):
            raise TemplateProblem(
                f"{what} lets the model pick where it calls ({{{first.name}}} in its scheme or host): "
                "write its host out, and put arguments in its path or query."
            )
        if headers:
            raise TemplateProblem(
                f"{what} takes its host from a run's value and the tool sends headers, which go only "
                "to a host written in its URL: write the host out, or take the headers away."
            )

    def arguments(self) -> tuple[list[str], set[str]]:
        """:return: The arguments it names, in order, and those it must have: all but a query pair's own."""
        names: list[str] = []
        required: set[str] = set()
        for pair in (self.path, *self.query):
            optional = _droppable(pair)
            for part in pair:
                if isinstance(part, Argument):
                    if part.name not in names:
                        names.append(part.name)
                    if part is not optional:
                        required.add(part.name)
        return names, required


def parse_body(text: str, fill: Callable[[str], str], *, what: str = "Its body") -> Any:
    """
    :return: The body as JSON whose strings are :class:`Text` or :class:`Whole`
        where they hold placeholders; None for none.
    :raises TemplateProblem: It isn't JSON, a bare placeholder taken as a value.
    """
    if not text.strip():
        return None
    try:
        raw = json.loads(_quoted(text))
    except json.JSONDecodeError as exc:
        raise TemplateProblem(
            f"{what} isn't JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        ) from None

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            parts = parse_text(value, fill, what=what)
            if len(parts) == 1 and not isinstance(parts[0], str):
                return Whole(parts[0])
            if all(isinstance(part, str) for part in parts):
                return "".join(parts)  # type: ignore[arg-type]
            return Text(parts)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(raw)


def _quoted(text: str) -> str:
    """:return: The JSON text with each placeholder outside its strings quoted: a whole value."""
    out: list[str] = []
    at = 0
    in_string = False
    while at < len(text):
        char = text[at]
        if in_string:
            out.append(char)
            if char == "\\" and at + 1 < len(text):
                out.append(text[at + 1])
                at += 2
                continue
            if char == '"':
                in_string = False
            at += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            at += 1
            continue
        match = PLACEHOLDER.match(text, at) if char in "{$" else None
        if match:
            out.append(json.dumps(match.group(0)))
            at = match.end()
            continue
        out.append(char)
        at += 1
    return "".join(out)


def body_arguments(body: Any) -> list[str]:
    """:return: The arguments a parsed body names, in order."""
    names: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Whole):
            parts: Sequence[Part] = (value.part,)
        elif isinstance(value, Text):
            parts = value.parts
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        elif isinstance(value, list):
            for item in value:
                walk(item)
            return
        else:
            return
        for part in parts:
            if isinstance(part, Argument) and part.name not in names:
                names.append(part.name)

    walk(body)
    return names


def parameters_of(
    schema: Mapping[str, Any],
    names: Sequence[str],
    required: set[str],
    optional: Collection[str] = frozenset(),
) -> dict[str, Any]:
    """
    :param schema: The arguments' JSON Schema as the author wrote it: their
        types and descriptions, and which are required.
    :param names: The arguments the URL and body name.
    :param required: Those the URL can't do without.
    :param optional: Those only a query pair of the URL is: left out unsent.
    :return: The JSON Schema the model calls the tool with: the named
        arguments only, each as the author declared it (a string otherwise);
        required when the URL needs it, the author says so, or it isn't
        declared and isn't only a query pair.
    """
    declared = schema.get("properties")
    properties: Mapping[str, Any] = declared if isinstance(declared, dict) else {}
    listed = schema.get("required")
    said: list[Any] = listed if isinstance(listed, list) else []
    out: dict[str, Any] = {
        "type": "object",
        "properties": {
            name: properties[name] if isinstance(properties.get(name), dict) else {"type": "string"}
            for name in names
        },
    }
    needed = [
        name
        for name in names
        if name in required or name in said or (name not in properties and name not in optional)
    ]
    if needed:
        out["required"] = needed
    return out


async def _filled(
    parts: Sequence[Part],
    args: Mapping[str, Any],
    values: Callable[[str], Any],
    *,
    encode: bool,
    what: str,
    need: bool | None = None,
) -> str:
    """
    :param encode: Percent-encode the arguments.
    :param need: Fail when an argument wasn't sent; as ``encode`` by default
        (a URL's must be, a body's text leaves it out).
    """
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, Value):
            out.append(as_text(await values(part.expression)))
        elif args.get(part.name) is not None:
            text = as_text(args[part.name])
            out.append(quote(text, safe="") if encode else text)
        elif encode if need is None else need:
            raise ToolFailure(
                f"{what} needs the {part.name!r} argument.", [f"Call it again with {part.name}."]
            )
    return "".join(out)


async def render_url(url: UrlTemplate, args: Mapping[str, Any], values: Callable[[str], Any]) -> str:
    """:return: The URL a call goes to; a query pair whose argument wasn't sent left out."""
    path = await _filled(url.path, args, values, encode=True, what="Its URL")
    pairs: list[str] = []
    for pair in url.query:
        optional = _droppable(pair)
        if optional is not None and args.get(optional.name) is None:
            continue
        pairs.append(await _filled(pair, args, values, encode=True, what="Its URL"))
    return path + ("?" + "&".join(pairs) if pairs else "")


async def render_query(
    rows: Sequence[QueryRow], args: Mapping[str, Any], values: Callable[[str], Any]
) -> list[tuple[str, str]]:
    """
    :return: The listed query parameters a call sends, filled in and not yet
        encoded: one whose value is one placeholder that comes to nothing
        left out, a list sent once per item.
    """
    pairs: list[tuple[str, str]] = []
    for row in rows:
        what = "Its query parameters"
        name = (await _filled(row.name, args, values, encode=False, need=True, what=what)).strip()
        if not name:
            continue
        whole = row.whole
        if whole is None:
            pairs.append((name, await _filled(row.value, args, values, encode=False, need=True, what=what)))
            continue
        value = await values(whole.expression) if isinstance(whole, Value) else args.get(whole.name)
        pairs.extend(
            (name, as_text(item))
            for item in (value if isinstance(value, list) else [value])
            if item is not None
        )
    return pairs


async def render_body(body: Any, args: Mapping[str, Any], values: Callable[[str], Any]) -> Any:
    """:return: The JSON a call sends; :data:`OMITTED` for none."""
    if isinstance(body, Whole):
        part = body.part
        if isinstance(part, Value):
            return await values(part.expression)
        return args.get(part.name, OMITTED)
    if isinstance(body, Text):
        return await _filled(body.parts, args, values, encode=False, what="Its body")
    if isinstance(body, dict):
        out: dict[str, Any] = {}
        for key, item in body.items():
            value = await render_body(item, args, values)
            if value is not OMITTED:
                out[key] = value
        return out
    if isinstance(body, list):
        items = [await render_body(item, args, values) for item in body]
        return [item for item in items if item is not OMITTED]
    return body


def chat_values(context: ToolContext | None, *, what: str) -> Callable[[str], Any]:
    """:return: What fills in ``{{ }}`` from the chat's request and state, as an instruction is."""

    async def value(expression: str) -> Any:
        state = context.state.to_dict() if context is not None else {}
        try:
            return await evaluate_async(expression, template_data(state), what=what)
        except TemplateError as exc:
            raise ToolFailure(str(exc)) from None

    return value


def run_values(
    provider: ToolValues | None, context: ToolContext | None, *, what: str
) -> Callable[[str], Any]:
    """:return: What fills in a call's ``{{ }}``: the run's provider's, else the chat's."""
    if provider is None:
        return chat_values(context, what=what)

    async def value(expression: str) -> Any:
        return await provider(expression, context, what=what)

    return value


def upgraded_http_tool(config: Mapping[str, Any]) -> dict[str, Any]:
    """
    An HTTP tool's settings from before it had a body (2026-10-10), as they
    are now. Then the arguments its URL didn't name went in the query (GET,
    DELETE) or as the JSON body; now only what its URL and body name is
    sent, so they're written there: ``?expand={expand}``, ``{"note": {note}}``.

    :return: The settings, with a ``body``; as they are when they have one.
    """
    if "body" in config:
        return dict(config)
    out: dict[str, Any] = {**config, "body": ""}
    schema = config.get("parameters")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return out
    url = str(config.get("url") or "")
    named = {match.group(3) for match in PLACEHOLDER.finditer(url) if match.group(3)}
    rest = [name for name in properties if NAME.fullmatch(name) and name not in named]
    if not rest:
        return out
    if str(config.get("method") or "GET").upper() in BODY_METHODS:
        out["body"] = "{\n" + ",\n".join(f"  {json.dumps(name)}: {{{name}}}" for name in rest) + "\n}"
    else:
        query = "&".join(f"{name}={{{name}}}" for name in rest)
        out["url"] = url + ("" if url.endswith(("?", "&")) else "&" if "?" in url else "?") + query
    return out
