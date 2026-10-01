"""Step code: one module per kind of step."""

from forge_task_workflows.engine import Executor
from forge_task_workflows.nodes.agent import agent
from forge_task_workflows.nodes.approval import approval
from forge_task_workflows.nodes.basic import delay, entry, if_, match, switch, transform
from forge_task_workflows.nodes.http import http
from forge_task_workflows.nodes.subworkflow import subworkflow

# Each kind of step's code. Loops, merges and ends are the engine's own.
EXECUTORS: dict[str, Executor] = {
    "entry": entry,
    "agent": agent,
    "approval": approval,
    "http": http,
    "transform": transform,
    "delay": delay,
    "subworkflow": subworkflow,
    "if": if_,
    "switch": switch,
    "match": match,
}
