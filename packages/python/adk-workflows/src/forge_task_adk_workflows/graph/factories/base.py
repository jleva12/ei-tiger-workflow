"""What every factory is given, and what they share."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from google.adk import Context, Workflow
from google.adk.workflow import BaseNode
from pydantic import BaseModel, ValidationError

from forge_task_adk_workflows.graph.data import Scope, data_of
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
from forge_task_adk_workflows.graph.schemas import problems
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.errors import StepFailed

#: Makes a loop's body graph for an item: its bindings (the item under the
#: loop's item name, and ``index``) in the data its nodes read.
BuildBody = Callable[[Mapping[str, Any]], Workflow]


@dataclass(frozen=True)
class Body:
    """A loop's body, cut out of its graph: made per item, and whether it can
    pause (then its items run one at a time)."""

    build: BuildBody
    pauses: bool


@dataclass(frozen=True)
class BuildContext:
    """
    What a factory is given besides its node: the run's services, what its
    node reads at run time, the graph's shape, and how nested graphs are
    built.

    :ivar services: The run's services.
    :ivar scope: What the node's graph lets it read (``data_of``).
    :ivar names: Every step's ADK name, by step ID.
    :ivar connected: Each step's ways out that lead somewhere, by step ID.
    :ivar bindings: The items (and index) of the loops the graph is in.
    :ivar top: Whether the graph is the run's own, not a saved agent's.
    :ivar input_misfit: How a refusal of the graph's input starts.
    :ivar saved: Builds another of the organization's agents as a node:
        ``(agent ID, ADK name, where) -> graph``.
    :ivar bodies: The loops' bodies, by loop step ID.
    """

    services: RunServices
    scope: Scope = field(default_factory=Scope)
    names: Mapping[str, str] = field(default_factory=dict)
    connected: Mapping[str, frozenset[str]] = field(default_factory=dict)
    bindings: Mapping[str, Any] = field(default_factory=dict)
    top: bool = True
    input_misfit: str = "The run's input doesn't fit its start"
    saved: Callable[[str, str, str], Workflow] | None = None
    bodies: Mapping[str, Body] = field(default_factory=dict)

    def data(self, ctx: Context, node_input: Any) -> dict[str, Any]:
        """:return: What a node's expressions read (``data_of``)."""
        return data_of(ctx, node_input, self.scope, self.bindings)


#: Turns a node of a document into its ADK node.
Factory = Callable[[dict[str, Any], BuildContext], BaseNode]


def text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def config_of(item: dict[str, Any]) -> dict[str, Any]:
    config = item.get("config")
    return config if isinstance(config, dict) else {}


def schema_of(value: Any) -> dict[str, Any] | None:
    """A JSON Schema setting: ``{}`` means none."""
    return value if isinstance(value, dict) and value else None


def where_of(item: dict[str, Any]) -> str:
    """How a build error names a node: ``Node 'Triage'``."""
    return f"Node '{text(item.get('name')) or text(item.get('id'))}'"


def label_of(node: dict[str, Any]) -> str:
    """How a run's failure names a step: ``Triage (triage)``."""
    return f"{text(node.get('name')) or text(node.get('id'))} ({text(node.get('id'))})"


def settings_of[T: BaseModel](node: dict[str, Any], model: type[T]) -> T:
    """
    A Forge step's settings, typed by its kind (``support.step_settings``).

    :raises AgentBuildError: When they aren't its kind's.
    """
    try:
        return model.model_validate(config_of(node))
    except ValidationError as error:
        raise AgentBuildError(
            f"{where_of(node)}: its settings aren't a {node.get('kind')}'s: {problems(error, 'settings')}."
        ) from None


def failed(node: dict[str, Any], error: StepFailed) -> RunFailed:
    """:return: The run's failure at a step that failed with nowhere to go."""
    return RunFailed(f"{label_of(node)} failed: {error.message}", step=node.get("id"))


def interrupt_id(node: dict[str, Any], ctx: Context) -> str:
    """
    The ID a step asks for input under: its ID and where in the run it is
    (its node path, with the run IDs of it and the graphs it's in), so a step
    reached twice, or once per loop item, never asks under another's.
    """
    return f"{node['id']}@{ctx.node_path}"


def asked(ctx: Context, interrupt: str) -> dict[str, Any]:
    """:return: The payload a step asked for input with, in this run; empty
    when it didn't."""
    for event in ctx.session.events:
        if event.invocation_id != ctx.invocation_id:
            continue
        for call in event.get_function_calls():
            if call.id == interrupt:
                payload = (call.args or {}).get("payload")
                return payload if isinstance(payload, dict) else {}
    return {}
