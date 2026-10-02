"""
The executor pattern: one factory per kind of node, a higher-order function
``(node, ctx: BuildContext) -> BaseNode`` that reads the node's settings once
and returns the ADK node whose function has what it needs closed over (its
templates, its schemas' Pydantic models, the run's services).
"""

from forge_task_adk_workflows.graph.factories.actions import delay, http, transform
from forge_task_adk_workflows.graph.factories.agents import (
    llm,
    loop_agent,
    parallel,
    saved,
    sequential,
)
from forge_task_adk_workflows.graph.factories.base import Body, BuildContext, Factory
from forge_task_adk_workflows.graph.factories.logic import end, if_, loop, match, merge, switch
from forge_task_adk_workflows.graph.factories.people import approval, human_input
from forge_task_adk_workflows.graph.factories.start import start

#: Each kind of node's factory. A start's is its hidden input node.
FACTORIES: dict[str, Factory] = {
    "start": start,
    "llm": llm,
    "sequential": sequential,
    "parallel": parallel,
    "loop_agent": loop_agent,
    "saved": saved,
    "approval": approval,
    "human_input": human_input,
    "http": http,
    "transform": transform,
    "delay": delay,
    "if": if_,
    "switch": switch,
    "match": match,
    "loop": loop,
    "merge": merge,
    "end": end,
}

__all__ = ["FACTORIES", "Body", "BuildContext", "Factory"]
