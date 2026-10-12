"""Asking the person something in the middle of a turn: ADK's
``adk_request_input`` call, with a description that teaches the model the
answer schemas a chat renders as a form. Needs ``forge-common[adk]``.

:data:`ask_person` is the tool. Calling it pauses the turn: the chat shows its
``message`` and a form built from its ``response_schema`` (one question per
field), and the person's answer comes back as the call's function response
(``{"result": answer}``), which carries the turn on::

    agent = LlmAgent(..., tools=[ask_person], after_model_callback=repair_input_schemas)

It's ADK's own call (``google.adk.tools.request_input``), so everything that
answers ADK's input requests answers it: the Forge chat's form, A2A's
input-required tasks, ADK's web UI. Only its description differs.

:func:`repair_input_schemas` goes on the agent's ``after_model_callback``: a
model sometimes puts ``required`` (or ``additionalProperties``) among the
``properties`` of the schema it asks with, and ADK can't check the answer
against a property that isn't a schema, so the turn would fail when it comes
back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.tools.long_running_tool import LongRunningFunctionTool

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_response import LlmResponse

__all__ = ["ASK_PERSON", "ASK_PERSON_INSTRUCTION", "ask_person", "repair_input_schemas"]

#: The call's name: ADK's own for an input request (``request_input``'s).
ASK_PERSON = "adk_request_input"

#: What an agent that may ask is told about it, for an instruction of its own.
ASK_PERSON_INSTRUCTION = (
    "When you need something only the person can tell you (a choice, a "
    "preference, a missing detail), ask with adk_request_input instead of in "
    "your reply, and wait for the answer. Ask once, for everything you need, "
    "with a response_schema that fits the answer."
)


def _adk_request_input(message: str, response_schema: dict[str, Any] | None = None) -> None:
    """Asks the person a question and waits for their answer: the chat shows
    `message` and a form for the answer, one question per field, and you get
    their answer back as this call's result. Use it when you need something
    only they can tell you (a choice, a preference, a missing detail) before
    you go on; ask once, for everything you need.

    Args:
      message: The question, in a sentence or two: what you need and why.
      response_schema: A JSON Schema for the answer, which the form is built
        from. {"type": "string"} for text; {"type": "string", "enum": [...]}
        to pick one; {"type": "array", "items": {"type": "string", "enum":
        [...]}} to pick several; {"type": "boolean"} for yes or no;
        {"type": "number"} or {"type": "integer"}; "format": "email", "uri",
        "date", "date-time" or "time" on a string for those. For several
        answers at once, an object: {"type": "object", "properties": {"field":
        {"type": ..., "title": "Shown as the question", "description": "Help
        under it"}, ...}, "required": ["field", ...]} with each field one of
        the above; "required" goes beside "properties", not inside it.

    Returns:
      Nothing now: the turn pauses until the person answers.
    """
    return None


_adk_request_input.__name__ = ASK_PERSON

#: The tool: ADK's ``adk_request_input``, with the description above.
ask_person = LongRunningFunctionTool(_adk_request_input)


def _repair_schema(schema: object) -> object:
    """
    A JSON Schema ADK can check an answer against: ``required`` moved out of
    ``properties`` to beside them, whatever else isn't a schema dropped from
    them, ``required`` kept to real fields, and the same for array items.
    """
    if not isinstance(schema, dict):
        return schema
    repaired = dict(schema)
    properties = repaired.get("properties")
    if isinstance(properties, dict):
        fields = {}
        for name, field in properties.items():
            if isinstance(field, dict):
                fields[name] = _repair_schema(field)
            elif name == "required" and isinstance(field, list):
                repaired.setdefault("required", field)
        repaired["properties"] = fields
        if isinstance(repaired.get("required"), list):
            repaired["required"] = [name for name in repaired["required"] if name in fields]
    if "items" in repaired:
        repaired["items"] = _repair_schema(repaired["items"])
    return repaired


def repair_input_schemas(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """
    An ``after_model_callback``: repairs the ``response_schema`` of the
    model's ``adk_request_input`` calls before they're kept, so the person's
    answer can carry the turn on.
    """
    content = llm_response.content
    for part in (content.parts if content else None) or ():
        call = part.function_call
        if call is not None and call.name == ASK_PERSON and call.args:
            schema = call.args.get("response_schema")
            if schema is not None:
                call.args["response_schema"] = _repair_schema(schema)
    return None
