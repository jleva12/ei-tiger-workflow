"""
Start: where every run starts. ADK's ``START`` hands the run's message to a
hidden input node, which keeps it as the run's ``input``: the message's text
(parsed when it's JSON), held to the start's input schema as a Pydantic model
(coerced; a run whose input doesn't fit fails). A run's input is kept in the
session's state as ``input`` too, for LLM agents' ``{input}``; a saved
agent's is its own, read from its graph's events, and leaves the run's alone.
"""

from typing import Any

from google.adk import Context
from google.adk.workflow import FunctionNode
from pydantic import ValidationError

from forge_task_adk_workflows.graph.data import INPUT_NODE, as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.base import BuildContext, settings_of
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_workflows.document import EntryConfig


def input_model(node: dict[str, Any]) -> Any:
    """:return: The Pydantic type a start holds the run's input to; None when
    it takes anything."""
    schema = settings_of(node, EntryConfig).input_schema
    return to_model(schema) if schema else None


def start(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    """The hidden input node a start stands for."""
    model = input_model(node)
    top = ctx.top
    misfit = ctx.input_misfit

    def run(adk: Context, node_input: Any) -> Any:
        value = as_data(node_input)
        if model is not None:
            try:
                value = held_to(model, value)
            except ValidationError as error:
                raise RunFailed(f"{misfit}: {problems(error, 'input')}", step=node["id"]) from None
        if top:
            adk.state["input"] = value
        return value

    return FunctionNode(name=INPUT_NODE, func=run)
