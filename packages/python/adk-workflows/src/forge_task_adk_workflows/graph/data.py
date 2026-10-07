"""
Forge's data convention, for every expression a node evaluates: what it reads
is

    {"input": ..., "previous": ..., "steps": {"<id>": {"output": ..., "error": ...}},
     "state": {...}, "<a loop's item name>": ..., "index": ...}

- ``input``: the run's input (its agent's, in a saved agent), as the start
  kept it: the hidden input node's output. ``steps.<start's ID>.output`` is it
  too.
- ``previous``: what the node before handed on (the node's input).
- ``steps.<id>.output`` / ``.error``: what each step the node can see handed
  on, read from the run's own events. That's the latest output of each node
  in this invocation, its ADK name mapped back to its step ID, in the node's
  graph and in the graphs of the loops it's in (the innermost item's first),
  never those of a saved agent it runs or runs in. It's every kind's: LLM and
  team agents' too, not only Forge's steps. If, Switch and Match record the
  way they took (``{"branch": ...}``), a Match taking every rule that holds
  the ways (``{"branches": [...]}``); a step that took its Error way records
  ``{"output": None, "error": {...}}``.
- ``state``: the session's state, but for Forge's own keys (``forge:``).
- a loop's item and index: the bindings its body was built with.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from google.adk import Context, Event
from google.genai import types
from pydantic import BaseModel

from forge_task_adk_workflows.support.expressions import plain

#: The hidden node after ADK's START that keeps the run's input.
INPUT_NODE = "__input__"
#: Where a step's error rides on the event of its Error way.
ERROR_KEY = "forge_error"
#: Session state keys Forge keeps for itself, out of ``state``.
PRIVATE_STATE = "forge:"
#: Logic steps: they hand on what came in, and record the way they took.
LOGIC = frozenset({"if", "switch", "match"})


@dataclass(frozen=True)
class StepInfo:
    """A step as its events are read: its ID and kind, and how an LLM agent's
    answer (the event's message) becomes data."""

    id: str
    kind: str
    parse: Callable[[str], Any] | None = None


@dataclass(frozen=True)
class Scope:
    """
    What a node's graph lets it read: its agent's steps by ADK name, the
    start's ID, and how many loop bodies deep the node's graph is in its
    agent's.
    """

    steps: Mapping[str, StepInfo] = field(default_factory=dict)
    start: str = "start"
    depth: int = 0

    def inside(self) -> "Scope":
        """:return: The scope of a loop's body in this one's graph."""
        return Scope(steps=self.steps, start=self.start, depth=self.depth + 1)


def as_data(value: Any) -> Any:
    """
    What a node was handed, as JSON its expressions read: a message as its text,
    parsed when it's JSON (a run's first node is handed the person's message).

    :param value: What the node was handed.
    :return: It as JSON values.
    """
    if isinstance(value, types.Content):
        text = "".join(part.text for part in value.parts or [] if part.text and not part.thought)
        try:
            return json.loads(text)
        except ValueError:
            return text
    if isinstance(value, BaseModel):
        return as_data(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): as_data(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [as_data(item) for item in value]
    return value


def data_of(
    ctx: Context,
    node_input: Any,
    scope: Scope,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    What a node's expressions read.

    :param ctx: The node's ADK context.
    :param node_input: What it was handed.
    :param scope: Its graph's scope.
    :param bindings: The items (and index) of the loops it's in.
    :return: ``input``, ``previous``, ``steps``, ``state`` and the bindings,
        as JSON.
    """
    steps = steps_of(ctx, scope)
    data: dict[str, Any] = {
        "input": steps.get(scope.start, {}).get("output"),
        "steps": steps,
        "previous": as_data(node_input),
        "state": {key: value for key, value in ctx.state.to_dict().items() if not key.startswith(PRIVATE_STATE)},
    }
    data.update(bindings or {})
    return plain(data)


def steps_of(ctx: Context, scope: Scope) -> dict[str, dict[str, Any]]:
    """
    :param ctx: A node's ADK context.
    :param scope: Its graph's scope.
    :return: What each step it can see handed on (``{"output": ..., "error":
        ...}``), by step ID.
    """
    frames = _frames(ctx.node_path, scope.depth)
    found: dict[str, dict[str, dict[str, Any]]] = {frame: {} for frame in frames}
    for event in ctx.session.events:
        if event.invocation_id != ctx.invocation_id:
            continue
        for path in event.node_info.output_for or [event.node_info.path]:
            frame, _, segment = path.rpartition("/")
            steps = found.get(frame)
            info = scope.steps.get(segment.partition("@")[0])
            if steps is None or info is None:
                continue
            record = _record(event, info)
            if record is not None:
                steps[info.id] = record
    merged: dict[str, dict[str, Any]] = {}
    for frame in frames:  # outermost first: an inner item's wins
        merged.update(found[frame])
    return merged


def _frames(node_path: str, depth: int) -> list[str]:
    # The paths of the graphs a node reads: its own, and those of the loops
    # it's in (a body's graph runs as <loop>@<run>/<body>@item_<i>).
    frame = node_path.rpartition("/")[0]
    frames = [frame]
    for _ in range(depth):
        frame = frame.rpartition("/")[0].rpartition("/")[0]
        frames.insert(0, frame)
    return frames


def _record(event: Event, info: StepInfo) -> dict[str, Any] | None:
    # What an event of a step says it handed on, if anything.
    if info.kind in LOGIC:
        route = event.actions.route if event.actions else None
        if isinstance(route, list):  # a match taking every rule that holds
            return {"output": {"branches": route}}
        return {"output": {"branch": route}} if isinstance(route, str) else None
    error = (event.custom_metadata or {}).get(ERROR_KEY)
    if error is not None:
        return {"output": None, "error": error}
    if event.output is not None:
        return {"output": as_data(event.output)}
    if event.node_info.message_as_output and event.content and info.parse:
        text = "".join(part.text for part in event.content.parts or [] if part.text and not part.thought)
        if text.strip():
            return {"output": info.parse(text)}
    return None
