"""Rich answers: the ``present`` tool (a ``rich_answers`` node).

The model calls ``present`` with a composition (cards, key figures, tables,
charts, forms…) as one tree of nodes, and Forge's chats draw the call's
arguments with assistant-ui's Generative UI (``rich-answers.tsx`` in the web
console). Each Rich answers node picks the components its agent may use
(``components``), and the tool's schema holds only those: every component
costs tokens in every model call.

The schema is built from the web console's catalog
(``present.catalog.json``, written by ``npm run generate:agent-schema``):
each component's own props, merged as the package merges them, into one
flat node whose ``_type`` names the component and whose ``children`` recurse
through ``$defs`` (Gemini refuses recursion through the root).

The browser can't add tools to ADK's run API, so it's a backend tool: it
answers ``{"shown": true}`` at once, and the turn goes on. A call that
doesn't fit the agent's components answers ``{"shown": false, "problems":
[...]}`` instead, and the chat leaves it undrawn, so the model can send it
again. Only chats draw it: API and A2A callers get the raw call, so the
agent is told to write a sentence about it too.

What the person does with it comes back as their next message: a Choices
option's reply, or a control's ``_action`` text with what they chose or
entered (a form's values by name). Nothing comes back through this tool.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from functools import cache
from importlib import resources
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

#: The tool's name, as the web console's toolkit renders it.
PRESENT = "present"

DESCRIPTION = (
    "Shows the person a composition in the chat, drawn from the tree you send. Every node, the root "
    "included, names its component with _type and has its props beside it; nest components in "
    "children, and wrap several in one layout component."
)

#: What every agent with the tool is told, beside its own instruction.
INSTRUCTION = (
    "When an answer is easier to take in drawn than written (key figures, a comparison, a trend, rows "
    "of data, steps to follow), show it with present, once per answer, then reply in text as well: a "
    "sentence or two on what it shows and what matters in it. Only the chat draws present; other "
    "readers get its raw arguments, so the text must stand on its own (ask in it what the composition "
    "asks), without repeating every figure."
)

#: Added when the agent has Choices.
CHOICES_INSTRUCTION = (
    "To offer next steps or a pick between a few options, add Choices: the person's click comes back "
    "as their next message."
)

#: Added when the agent has components that act (buttons, fields, forms).
ACTION_INSTRUCTION = (
    'A button, field or list item does something only with _action {"type": "reply", "text": "..."}: '
    "using it sends the text, then what the person chose or entered, as their next message. To ask "
    "for several values at once, give each field a name inside a Form (or a Card with asForm and a "
    "confirm) and put the _action there, not on the fields: its values come back together, by name "
    "(a field's own _action in a form is ignored)."
)

#: The most problems a call that doesn't fit is told about.
MAX_PROBLEMS = 5


@cache
def present_catalog() -> dict[str, Any]:
    """:return: The web console's catalog of components, each with its props."""
    text = resources.files("forge_agent_runtime").joinpath("present.catalog.json").read_text("utf-8")
    return json.loads(text)  # type: ignore[no-any-return]


def known_components() -> tuple[str, ...]:
    """:return: Every component, in the order the model is offered them."""
    return tuple(name for group in present_catalog()["groups"] for name in group["components"])


def default_components() -> tuple[str, ...]:
    """:return: What a node that picked nothing yet offers (an older document's)."""
    return tuple(present_catalog()["defaults"])


def components_of(picked: Iterable[Any] | None) -> tuple[str, ...]:
    """
    :param picked: Component names, as a node's ``components`` has them;
        None for the defaults.
    :return: Them made whole: with what each needs, in the offered order.
    :raises ValueError: One isn't a component, or there are none.
    """
    names = default_components() if picked is None else [str(name) for name in picked]
    known = known_components()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(
            f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'}n't a component it knows"
        )
    requires: dict[str, list[str]] = present_catalog()["requires"]
    chosen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in chosen:
            chosen.add(name)
            todo.extend(requires.get(name, []))
    if not chosen:
        raise ValueError("it picks no components")
    return tuple(name for name in known if name in chosen)


def _canonical(schema: Any) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


@cache
def present_schema(components: tuple[str, ...] | None = None) -> dict[str, Any]:
    """
    :param components: The components it offers (:func:`components_of`'s);
        the defaults for None.
    :return: The JSON Schema of the call's arguments: one node, ``_type``
        naming one of them and their props beside it (a prop several have
        with different schemas takes any of them), ``_action`` when one of
        them acts, and ``children`` recursing through ``$defs``.
    """
    catalog = present_catalog()
    names = components_of(components)
    merged: dict[str, list[Any]] = {}
    for name in names:
        for key, schema in catalog["components"][name]["properties"].items():
            seen = merged.setdefault(key, [])
            if _canonical(schema) not in {_canonical(each) for each in seen}:
                seen.append(copy.deepcopy(schema))
    described = "; ".join(f'"{name}": {catalog["components"][name]["description"]}' for name in names)
    properties: dict[str, Any] = {
        "_type": {
            "type": "string",
            "enum": list(names),
            "description": f"The component to render. {described}",
        }
    }
    properties.update({key: each[0] if len(each) == 1 else {"anyOf": each} for key, each in merged.items()})
    if any(name in catalog["acting"] for name in names):
        properties["_action"] = copy.deepcopy(catalog["action"])
    properties["children"] = {"$ref": "#/$defs/children"}
    node = {"type": "object", "properties": properties, "required": ["_type"]}
    return {**node, "$defs": {"node": copy.deepcopy(node), "children": copy.deepcopy(catalog["children"])}}


def instruction_for(components: tuple[str, ...]) -> str:
    """:return: What an agent offering these components is told."""
    parts = [INSTRUCTION]
    if "Choices" in components:
        parts.append(CHOICES_INSTRUCTION)
    if any(name in present_catalog()["acting"] for name in components):
        parts.append(ACTION_INSTRUCTION)
    return " ".join(parts)


@cache
def _validator(components: tuple[str, ...]) -> Draft202012Validator:
    # The schema is the package's own, not the model's: the stock engine is safe here.
    return Draft202012Validator(present_schema(components))


def problems_of(args: Any, components: tuple[str, ...] | None = None) -> list[str]:
    """:return: Where a call's arguments don't fit these components, the first few; none when they do."""
    found: list[str] = []
    validator = _validator(components_of(components))
    for each in sorted(validator.iter_errors(args), key=lambda error: list(error.absolute_path)):
        # Children are text or a node (anyOf): name what's wrong in the node.
        error = best_match([each]) or each
        where = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        found.append(f"{where.lstrip('.') or 'The root'}: {error.message}")
        if len(found) == MAX_PROBLEMS:
            break
    return found


class PresentTool(BaseTool):
    """Shows a composition in the chat, of the components it offers; it answers at once."""

    def __init__(self, components: Iterable[Any] | None = None) -> None:
        super().__init__(name=PRESENT, description=DESCRIPTION)
        #: What it offers, made whole.
        self.components = components_of(components)

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=present_schema(self.components),
        )

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        await super().process_llm_request(tool_context=tool_context, llm_request=llm_request)
        llm_request.append_instructions([instruction_for(self.components)])

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        problems = problems_of(args, self.components)
        if problems:
            return {"shown": False, "problems": problems}
        return {"shown": True}
