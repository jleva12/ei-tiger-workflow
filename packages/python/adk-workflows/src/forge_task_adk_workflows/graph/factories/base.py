"""What every factory is given, and what they share."""

import socket
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from google.adk import Context, Event, Workflow
from google.adk.workflow import BaseNode
from pydantic import BaseModel, ValidationError

from forge_task_adk_workflows.graph.data import CALLED_WITH_KEY, ERROR_KEY, STARTED_KEY, Scope, as_data, data_of
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
from forge_task_adk_workflows.graph.schemas import problems
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.errors import StepFailed

#: This worker's machine, as a step's start records it: where what the step
#: left running can be stopped from.
HOST = socket.gethostname()
#: What the error of a step whose attempt was cut off while it acted carries
#: (``steps.<id>.error.may_have_run``): its effect may have happened.
MAY_HAVE_RUN = "may_have_run"

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


def called_with(value: Any) -> Event:
    """
    :param value: What a step works out and sends: a saved workflow's
        inputs, an agent's message, an HTTP request.
    :return: The event a step records it on (``CALLED_WITH_KEY``), before
        it sends it: it hands on nothing, so the step goes on.
    """
    return Event(custom_metadata={CALLED_WITH_KEY: as_data(value)})


def error_way(node: dict[str, Any], error: StepFailed, *, has_error_way: bool) -> Event:
    """
    :return: The event of a step's Error way, its error on it (``steps.<id>.error``).
    :raises RunFailed: When it has no Error way connected: the run fails.
    """
    if not has_error_way:
        raise failed(node, error) from None
    return Event(route="error", custom_metadata={ERROR_KEY: error.as_error()})  # type: ignore[call-arg]  # ADK's shorthand for actions.route


@dataclass(frozen=True)
class Attempt:
    """
    A step's attempt, as the run's earlier attempts left it: this run of its
    node (its node path, the run IDs of it and the graphs it's in).

    :ivar key: The step's key: the same for every attempt of this one
        execution of it, and sent with what it does (``Idempotency-Key``,
        ``FORGE_IDEMPOTENCY_KEY``); another once an attempt of it ended
        failed and the run was tried again, so the step runs afresh.
    :ivar cut_off: What the last attempt recorded as it began to act
        (``{key, host}``), when nothing after it says how the step ended: it
        was cut off (its worker stopped, or another way stopped the run), and
        its effect may have happened. None when no attempt was.
    :ivar called: What that attempt recorded it sent (``CALLED_WITH_KEY``).
    """

    key: str
    cut_off: dict[str, Any] | None = None
    called: Any = None

    def elsewhere(self) -> str | None:
        """:return: The machine the cut-off attempt ran on, when it isn't
        this one (what it left running can't be stopped from here)."""
        host = (self.cut_off or {}).get("host")
        return host if isinstance(host, str) and host and host != HOST else None


def attempt(ctx: Context) -> Attempt:
    """
    :return: The step's attempt (:class:`Attempt`), from its node's events in
        this invocation: each start it recorded (:func:`started`), and whether
        anything after it says how it ended (an output, a way, an error).
    """
    ended = 0
    last: dict[str, Any] | None = None
    called: Any = None
    for event in ctx.session.events:
        if event.invocation_id != ctx.invocation_id or event.node_info.path != ctx.node_path:
            continue
        metadata = event.custom_metadata or {}
        record = metadata.get(STARTED_KEY)
        if isinstance(record, dict):
            last, called = record, metadata.get(CALLED_WITH_KEY)
        elif last is not None and _ends(event):
            # It ended (failed: an ending with an output or a way isn't run again).
            ended += 1
            last = called = None
    seed = f"forge:step:{ctx.invocation_id}:{ctx.node_path}" + (f"#{ended}" if ended else "")
    return Attempt(key=str(uuid.uuid5(uuid.NAMESPACE_URL, seed)), cut_off=last, called=called)


def _ends(event: Event) -> bool:
    # Whether a node's event says how it ended: its output, its way, its Error way, an error.
    return (
        event.output is not None
        or (event.actions is not None and event.actions.route is not None)
        or ERROR_KEY in (event.custom_metadata or {})
        or event.error_code is not None
    )


def started(step: Attempt, called: Any = None) -> Event:
    """
    :param called: What it sends, when the run page shows it (``called_with``).
    :return: The event a step whose effect may not be safe to repeat records
        before it acts (``STARTED_KEY``): its key, and this machine. Its node
        goes on once it's in the session, so an attempt that finds it there
        with nothing after it knows the step was cut off (:func:`attempt`).
    """
    metadata: dict[str, Any] = {STARTED_KEY: {"key": step.key, "host": HOST}}
    if called is not None:
        metadata[CALLED_WITH_KEY] = as_data(called)
    return Event(custom_metadata=metadata)


def may_have_run(message: str, step: Attempt) -> StepFailed:
    """:return: A cut-off step's failure: ``steps.<id>.error`` with
    ``may_have_run`` and the key it acted under (to look it up by, or to try
    again under, where what it called keeps one)."""
    return StepFailed(message, details={MAY_HAVE_RUN: True, "idempotency_key": step.key})


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
