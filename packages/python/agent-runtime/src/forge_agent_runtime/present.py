"""Rich answers: the ``present`` tool (a ``rich_answers`` node).

The model calls ``present`` with a composition (cards, key figures, tables,
charts, forms…) as one tree of nodes, and Forge's chats draw the call's
arguments with assistant-ui's Generative UI (``components/forge/generative-ui/present-ui.tsx`` in the web
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
again. Either answer carries a ``note`` that it's the model's alone: models
sometimes quoted it, or the composition they sent, back as their reply
(chats leave such a quote out too: ``present-echo.ts``).
Only chats draw it: API and A2A callers get the raw call, so the agent is
told to write a sentence about it too.

What the person does with it comes back as their next message: a Choices
option's reply, or a control's ``_action`` text with what they chose or
entered (a form's values by name). Nothing comes back through this tool.

The tool may also offer its organization's team components
(``forge_agent_runtime.generative_ui``): compositions of base components,
which the model calls by key (``org.…``) with their inputs in ``props``.
The tool draws those as base components itself and answers with the tree
(``ui``, :data:`UI_PROTOCOL`), which chats draw in place of the call's
arguments, and which stays with the conversation, so old messages show what
was drawn then. A call of base components alone still answers ``shown``
only. The tree is the chat's, not the model's: :func:`strip_drawn_trees`
leaves it out of every later model request (the tool's own agent's, and
through :class:`PresentHistoryPlugin` every other agent's in the app).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

if TYPE_CHECKING:
    from forge_agent_runtime.generative_ui.compiler import CompiledComponent

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
    "of data, steps to follow), show it with present, once per answer, then reply in plain words as "
    "well: a sentence or two on what it shows and what matters in it. Only chats draw present, so the "
    "words must stand on their own (ask in them what the composition asks), without repeating every "
    "figure. Never write JSON in your reply: not the composition you sent, and not present's answer "
    '({"shown": ...}), which is for you alone.'
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

#: Added when the agent has team components.
TEAM_INSTRUCTION = (
    "Components named org.… are your team's own: give one its inputs in props, as its schema says, and "
    "nothing else beside _type (no children, no _action): its layout is set. Prefer one when it fits "
    "what you're showing."
)

#: Added when one of its team components has controls or Choices.
TEAM_ACTION_INSTRUCTION = (
    "What the person picks or enters in a team component comes back as their next message."
)

#: The version of the drawn tree's answer (``ui``) chats read.
UI_PROTOCOL = 1

#: The most problems a call that doesn't fit is told about.
MAX_PROBLEMS = 5

#: What a call that's drawn is told, beside ``shown``.
SHOWN_NOTE = "The person sees it now. This answer is for you alone: never quote it; reply in your own words."
#: What a call that doesn't fit is told, beside its problems.
REFUSED_NOTE = (
    "Nothing was shown. Fix the problems and call present again. This answer is for you alone: "
    "never quote it to the person."
)


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


#: The components that act: buttons, fields, forms (what the person uses comes back as their message).
ACTING = frozenset(present_catalog()["acting"])


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
    return _schema_of(components_of(components))


def _schema_of(
    names: tuple[str, ...], team: tuple[CompiledComponent, ...] = (), *, inputs: bool = True
) -> dict[str, Any]:
    """
    :param names: Base components, made whole.
    :param team: Team components, named in ``_type`` beside them, their
        inputs in ``props`` (any of their schemas).
    :param inputs: Describe ``props`` as the team components' inputs; with
        False, any object (to check a call's shape with the stock engine,
        before each team node's props are held to its own schema).
    """
    catalog = present_catalog()
    merged: dict[str, list[Any]] = {}
    for name in names:
        for key, schema in catalog["components"][name]["properties"].items():
            seen = merged.setdefault(key, [])
            if _canonical(schema) not in {_canonical(each) for each in seen}:
                seen.append(copy.deepcopy(schema))
    described = "; ".join(
        [f'"{name}": {catalog["components"][name]["description"]}' for name in names]
        + [f'"{each.key}" (takes props): {each.description}' for each in team]
    )
    properties: dict[str, Any] = {
        "_type": {
            "type": "string",
            "enum": [*names, *(each.key for each in team)],
            "description": f"The component to render. {described}",
        }
    }
    properties.update({key: each[0] if len(each) == 1 else {"anyOf": each} for key, each in merged.items()})
    if any(name in catalog["acting"] for name in names):
        properties["_action"] = copy.deepcopy(catalog["action"])
    if team:
        schemas = [copy.deepcopy(each.input_schema) for each in team] if inputs else [{"type": "object"}]
        properties["props"] = {
            **(schemas[0] if len(schemas) == 1 else {"anyOf": schemas}),
            "description": "A team component's inputs, as its own schema says; only team components take props.",
        }
    properties["children"] = {"$ref": "#/$defs/children"}
    node = {"type": "object", "properties": properties, "required": ["_type"]}
    return {**node, "$defs": {"node": copy.deepcopy(node), "children": copy.deepcopy(catalog["children"])}}


def instruction_for(components: tuple[str, ...], team: Iterable[CompiledComponent] = ()) -> str:
    """
    :return: What an agent offering these components is told: how to write
        the base ones it may (Choices, controls), and how to call its team's,
        whose controls and Choices it doesn't write but should know answer.
    """
    parts = [INSTRUCTION]
    if "Choices" in components:
        parts.append(CHOICES_INSTRUCTION)
    if any(name in present_catalog()["acting"] for name in components):
        parts.append(ACTION_INSTRUCTION)
    team = tuple(team)
    if team:
        parts.append(TEAM_INSTRUCTION)
        if any(each.capabilities.has_actions or each.capabilities.has_choices for each in team):
            parts.append(TEAM_ACTION_INSTRUCTION)
    return " ".join(parts)


@cache
def _validator(components: tuple[str, ...]) -> Draft202012Validator:
    # The schema is the package's own, not the model's: the stock engine is safe here.
    return Draft202012Validator(present_schema(components))


def problems_of(args: Any, components: tuple[str, ...] | None = None) -> list[str]:
    """
    :return: Where a call's arguments don't fit these components, the first
        few, or where a button in them would do nothing; none when neither.
    """
    return _misfits(_validator(components_of(components)), args) or _dead_buttons(args)[:MAX_PROBLEMS]


def _misfits(validator: Draft202012Validator, args: Any) -> list[str]:
    """:return: Where the arguments don't fit the validator's schema, the first few."""
    found: list[str] = []
    for each in sorted(validator.iter_errors(args), key=lambda error: list(error.absolute_path)):
        # Children are text or a node (anyOf): name what's wrong in the node.
        error = best_match([each]) or each
        where = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        found.append(f"{where.lstrip('.') or 'The root'}: {error.message}")
        if len(found) == MAX_PROBLEMS:
            break
    return found


def _acts(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("_action"), dict)


def _form_acts_in(node: Any, depth: int = 0) -> bool:
    """Whether there's a Form with an ``_action`` in a node's children: the chat makes it its Card's."""
    if depth > 32:
        return False
    if isinstance(node, list):
        return any(_form_acts_in(each, depth + 1) for each in node)
    if not isinstance(node, dict):
        return False
    if node.get("_type") == "Form" and _acts(node):
        return True
    return _form_acts_in(node.get("children"), depth + 1)


def _dead_buttons(node: Any, where: str = "", depth: int = 0) -> list[str]:
    """
    :return: The Cards' footer buttons that would do nothing when clicked: a
        confirm or cancel without an ``_action``, where nothing else takes the
        click (a confirm sends an asForm Card's fields with the Card's
        ``_action``, or a Form's inside it with the Form's).
    """
    if depth > 32:
        return []
    if isinstance(node, list):
        return [
            found
            for index, each in enumerate(node)
            for found in _dead_buttons(each, f"{where}[{index}]", depth + 1)
        ]
    if not isinstance(node, dict):
        return []
    found: list[str] = []
    if node.get("_type") == "Card":
        title = node.get("title")
        card = f'The Card "{title}"' if isinstance(title, str) and title.strip() else "A Card"
        for button in ("confirm", "cancel"):
            spec = node.get(button)
            if not isinstance(spec, dict) or _acts(spec):
                continue
            if button == "confirm" and (
                (node.get("asForm") is True and _acts(node)) or _form_acts_in(node.get("children"))
            ):
                continue
            fix = (
                "give the confirm an _action, or make the Card asForm with an _action so it sends the fields in it"
                if button == "confirm"
                else "give the cancel an _action, or leave it out"
            )
            found.append(
                f"{where.lstrip('.') or 'The root'}: {card}'s {button} button does nothing when clicked: {fix}."
            )
    if "children" in node:
        found.extend(_dead_buttons(node["children"], f"{where}.children", depth + 1))
    return found


#: How deep a call's team nodes are looked for.
_MAX_DEPTH = 32
#: Another agent's present answer as ADK hands it to an agent, in words
#: (``google.adk.flows.llm_flows.context._fencing``): who, then the answer quoted.
_RELAYED = re.compile(r"\A\[[^\n]*\] `present` tool returned result:\n", re.ASCII)
#: How a drawn tree's answer reads quoted there (a Python dict's repr).
_RELAYED_TREE = "'ui': {'protocol': "


def _without_tree(part: types.Part) -> types.Part:
    """:return: The part, or a copy of it without a present answer's drawn tree."""
    response = part.function_response
    if response is not None:
        answer = response.response
        if response.name == PRESENT and isinstance(answer, dict) and "ui" in answer:
            kept = {key: value for key, value in answer.items() if key != "ui"}
            return part.model_copy(
                update={"function_response": response.model_copy(update={"response": kept})}
            )
        return part
    text = part.text
    if text is not None and _RELAYED_TREE in text and (start := _RELAYED.match(text)) is not None:
        # Only a drawn answer has a tree, and every drawn answer says this: no need to read the repr back.
        quoted = text[start.end() :]
        opened, closed = quoted.find("{"), quoted.rfind("}")
        if 0 <= opened < closed:
            shown = str({"shown": True, "note": SHOWN_NOTE})
            return part.model_copy(
                update={"text": text[: start.end()] + quoted[:opened] + shown + quoted[closed + 1 :]}
            )
    return part


def strip_drawn_trees(llm_request: LlmRequest) -> None:
    """
    Leave the trees ``present`` drew out of a model request: they're the
    chat's, kept in the conversation for it, and would cost tokens in every
    later call (and show the model base trees to copy). The model keeps
    ``shown`` and the note. Parts are replaced, never changed: ADK shares
    their fields with the conversation's events.
    """
    for index, content in enumerate(llm_request.contents):
        parts = content.parts or []
        kept = [_without_tree(part) for part in parts]
        if any(new is not old for new, old in zip(kept, parts, strict=True)):
            llm_request.contents[index] = content.model_copy(update={"parts": kept})


class PresentHistoryPlugin(BasePlugin):
    """
    Leaves drawn trees out of every agent's model requests in an app
    (:func:`strip_drawn_trees`): agents without ``present`` see another's
    answers too, relayed in words.
    """

    def __init__(self) -> None:
        super().__init__(name="forge_present_history")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        strip_drawn_trees(llm_request)
        return None


class PresentTool(BaseTool):
    """Shows a composition in the chat, of the components it offers; it answers at once."""

    #: What it shows is for the person (its calls are shown in full: ``SHOWN_ATTR``).
    forge_shown: ClassVar[bool] = True

    def __init__(
        self, components: Iterable[Any] | None = None, team: Iterable[CompiledComponent] = ()
    ) -> None:
        """
        :param components: Base component names, as a node's ``components``
            has them; None for the defaults. None of them is fine beside
            team components.
        :param team: Its organization's team components the agent offers,
            compiled (``forge_agent_runtime.generative_ui``).
        :raises ValueError: A name isn't a component, there are none at all,
            or two team components have one key.
        """
        super().__init__(name=PRESENT, description=DESCRIPTION)
        #: Its team components, by key.
        self.team: dict[str, CompiledComponent] = {}
        for each in team:
            if each.key in self.team:
                raise ValueError(f"it has the team component {each.key} twice")
            self.team[each.key] = each
        picked = None if components is None else list(components)
        #: What it offers, made whole.
        self.components = () if self.team and picked == [] else components_of(picked)
        if self.team:
            # Its own: the same key is another component in another organization.
            offered = tuple(self.team.values())
            self._schema = _schema_of(self.components, offered)
            self._shape = Draft202012Validator(_schema_of(self.components, offered, inputs=False))

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self._schema if self.team else present_schema(self.components),
        )

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        await super().process_llm_request(tool_context=tool_context, llm_request=llm_request)
        llm_request.append_instructions([instruction_for(self.components, self.team.values())])
        # The request's contents are in by now (ADK runs tools' processors last).
        strip_drawn_trees(llm_request)

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        problems, tree, used = self.drawn(args)
        if problems:
            return {"shown": False, "problems": problems, "note": REFUSED_NOTE}
        if not used:
            return {"shown": True, "note": SHOWN_NOTE}
        return {
            "shown": True,
            "note": SHOWN_NOTE,
            "ui": {
                "protocol": UI_PROTOCOL,
                "components": [{"key": each.key, "version": each.version} for each in used],
                "tree": tree,
            },
        }

    def drawn(self, args: Any) -> tuple[list[str], Any, list[CompiledComponent]]:
        """
        :return: Where a call doesn't fit, the first few; else none, the
            base tree the chat draws, and the team components drawn in it.
        """
        if not self.team:
            return problems_of(args, self.components), args, []
        # Its shape, by the package's own schema: types it offers, base props, props anywhere.
        found = _misfits(self._shape, args) or self._misplaced(args)
        if found:
            return found, None, []
        used: dict[str, CompiledComponent] = {}
        try:
            tree = self._expand(args, "", used, 0)
        except ValueError as error:
            return list(getattr(error, "problems", None) or [str(error)])[:MAX_PROBLEMS], None, []
        # Every base component, as chats draw them: a team one may use some the agent doesn't pick.
        found = problems_of(tree, known_components())
        if found and used:
            found = [f"Drawn with its team components, {problem}" for problem in found]
        return found, None if found else tree, list(used.values())

    def _team_of(self, node: dict[str, Any]) -> CompiledComponent | None:
        kind = node.get("_type")
        return self.team.get(kind) if isinstance(kind, str) else None

    def _misplaced(self, node: Any, where: str = "", depth: int = 0) -> list[str]:
        """:return: Team nodes with more than their props, and base nodes with props."""
        if depth > _MAX_DEPTH:
            return []
        if isinstance(node, list):
            return [
                found
                for index, each in enumerate(node)
                for found in self._misplaced(each, f"{where}[{index}]", depth + 1)
            ][:MAX_PROBLEMS]
        if not isinstance(node, dict):
            return []
        if self._team_of(node) is not None:
            extra = sorted(set(node) - {"_type", "props"})
            if not extra:
                return []
            return [
                f"{where.lstrip('.') or 'The root'}: {node['_type']} takes only _type and props, its layout is "
                f"set: leave out {', '.join(extra)}"
            ]
        found = []
        if "props" in node:
            found.append(f"{(where + '.props').lstrip('.')}: only a team component (org.…) takes props")
        found.extend(self._misplaced(node.get("children"), f"{where}.children", depth + 1))
        return found[:MAX_PROBLEMS]

    def _expand(self, node: Any, where: str, used: dict[str, CompiledComponent], depth: int) -> Any:
        """:return: The call with its team nodes drawn as base ones (deeper than it looks, left: refused after)."""
        if depth > _MAX_DEPTH:
            return node
        if isinstance(node, list):
            return [
                self._expand(each, f"{where}[{index}]", used, depth + 1) for index, each in enumerate(node)
            ]
        if not isinstance(node, dict):
            return node
        team = self._team_of(node)
        if team is not None:
            used.setdefault(team.key, team)
            return team.expand(node.get("props", {}), where=f"{where}.props".lstrip("."))
        if "children" not in node:
            return node
        return {**node, "children": self._expand(node["children"], f"{where}.children", used, depth + 1)}
