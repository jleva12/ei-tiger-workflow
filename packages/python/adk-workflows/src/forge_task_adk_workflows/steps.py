"""
A run's steps, as its run page shows them: what each step of the document did
in the run's ADK session, read from the session's events.

One entry per node of the document, in the document's order::

    {"id", "name", "kind", "status", "output", "error", "started_at", "finished_at", "calls"}

- ``status``:
  - ``done``: it handed on its output (``error`` too when it took its Error
    way, handing on nothing);
  - ``failed``: it failed the run (ADK's error event, e.g. ``RunFailed``, at it
    or inside it), or it's a failed End (the run fails once every way ends);
  - ``waiting``: it asked a person, or the timer, and waits for the answer (a
    pending pause: an approval, human input, a long delay);
  - ``running``: it started (an event at it or inside it) and hasn't finished;
  - ``not_reached``: nothing of it is in the session.
- ``output`` / ``error``: what it handed on, as ``steps.<id>`` reads it
  (``data.py``): an LLM agent's answer as its output schema makes it, If /
  Switch / Match's ``{"branch"}`` (a Match taking every rule that holds,
  ``{"branches"}``), a loop's ``{count, results}``, a saved
  agent's result. A step run more than once (in a loop's body) shows its last.
- ``calls``: the tools its LLM agents called (theirs, and those of the agent
  from the Agents page it is): ``{"id", "agent", "name", "args", "status",
  "response", "at"}``, ``status`` ``done``, ``failed`` (the tool answered an
  error, or a person refused it), ``waiting`` (for a person to confirm it) or
  ``running``; a long response cut short.
- ``started_at`` / ``finished_at``: when a step before it handed on to it (the
  last to, before its first event), else its first event; and the event that
  finished it. ISO 8601 in UTC; None when there's none. ADK records a step only
  once it hands something on (an LLM agent's first event is its answer, an
  inline delay's is its end), so its own first event would say it took no time.

A node's events are found by its ADK name (``names.py``) under the graph's own
path, and under its loops' bodies (``<loop>@1/<loop>__each@item_<i>/``). The
start is its hidden input node (``__input__``); the other hidden nodes (the
finish, the collectors) aren't steps. What runs inside a node (a team's
sub-agents, a saved agent's own steps, a loop's body) counts for that node's
times, failures and pauses, never as steps of their own.

ADK records nothing for a node that hands on nothing and changes no state, so
such a step is taken as done when a step only it leads to ran.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.adk.events import Event
from google.adk.sessions import Session

from forge_task_adk_workflows.graph.data import INPUT_NODE, StepInfo, _record, as_data
from forge_task_adk_workflows.graph.factories.agents import answer_parser
from forge_task_adk_workflows.graph.factories.base import config_of, items, text
from forge_task_adk_workflows.graph.names import adk_name, body_name
from forge_task_adk_workflows.graph.pauses import REQUEST_CONFIRMATION, REQUEST_INPUT, pending_pauses
from forge_task_adk_workflows.support.expressions import as_text

DONE = "done"
FAILED = "failed"
WAITING = "waiting"
RUNNING = "running"
NOT_REACHED = "not_reached"
#: Every status a step can have.
STATUSES = (DONE, FAILED, WAITING, RUNNING, NOT_REACHED)
#: The most of a tool's response a step shows, as JSON text.
MAX_RESPONSE = 4000


@dataclass
class _Seen:
    """What the session says of one step."""

    started: float | None = None
    finished: float | None = None
    output: Any = None
    error: Any = None
    handed_on: bool = False
    failed_at: float | None = None
    waiting: bool = False
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The calls a person is still to confirm: ADK answers each "it needs confirming" meanwhile.
    confirming: set[str] = field(default_factory=set)

    def saw(self, when: float) -> None:
        if self.started is None or when < self.started:
            self.started = when

    @property
    def status(self) -> str:
        if self.waiting:
            return WAITING
        if self.failed_at is not None and (self.finished is None or self.failed_at >= self.finished):
            return FAILED
        if self.handed_on:
            return DONE
        return RUNNING if self.started is not None else NOT_REACHED


def run_steps(session: Session | None, document: dict[str, Any]) -> list[dict[str, Any]]:
    """
    :param session: The run's ADK session, as its session service reads it;
        None before the run has one.
    :param document: The ``forge.agent/v1`` document the run runs.
    :return: Each of its steps, in the document's order, with what it did.
    """
    nodes = [
        node for node in items(document.get("nodes")) if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]
    infos: dict[str, StepInfo] = {}
    loops: set[str] = set()
    for node in nodes:
        kind = text(node.get("kind"))
        if kind == "start":
            infos[INPUT_NODE] = StepInfo(node["id"], kind)
            continue
        name = adk_name(text(node.get("name")))
        if not name:
            continue
        parse = answer_parser(config_of(node)) if kind == "llm" else None
        infos[name] = StepInfo(node["id"], kind, parse)
        if kind == "loop":
            loops.add(name)
    by_id = {info.id: info for info in infos.values()}
    seen = {node["id"]: _Seen() for node in nodes}
    events = _latest_run(session.events) if session is not None else []
    asked = {pause.interrupt_id for pause in pending_pauses(session)} if session is not None else set()

    for event in events:
        path = event.node_info.path or ""
        when = event.timestamp
        for step, _ in _steps_on(path, infos, loops):
            seen[step].saw(when)
            if event.error_code or event.error_message:
                seen[step].failed_at = when
                seen[step].error = {"message": event.error_message or event.error_code, "code": event.error_code}
        for call in event.get_function_calls():
            if call.name in (REQUEST_INPUT, REQUEST_CONFIRMATION) and call.id in asked:
                for step, _ in _steps_on(path, infos, loops):
                    seen[step].waiting = True
        _calls(event, _steps_on(path, infos, loops), seen, asked)
        for target in event.node_info.output_for or [path]:
            chain = _steps_on(target, infos, loops)
            if not chain or not chain[-1][1]:
                continue
            step = chain[-1][0]
            record = _record(event, by_id[step]) or _finished(event, by_id[step])
            if record is None:
                continue
            found = seen[step]
            found.saw(when)
            found.handed_on = True
            found.finished = when
            found.output = as_data(record.get("output"))
            found.error = record.get("error")

    sources = _sources(document, seen)
    _infer_silent(nodes, sources, seen)
    _started_when_handed_on(sources, seen)
    _failed_ends(nodes, seen)
    return [
        {
            "id": node["id"],
            "name": text(node.get("name")),
            "kind": text(node.get("kind")),
            "status": seen[node["id"]].status,
            "output": seen[node["id"]].output,
            "error": seen[node["id"]].error,
            "started_at": _iso(seen[node["id"]].started),
            "finished_at": _iso(seen[node["id"]].finished) if seen[node["id"]].handed_on else None,
            "calls": list(seen[node["id"]].calls.values()),
        }
        for node in nodes
    ]


def _calls(event: Event, chain: list[tuple[str, bool]], seen: Mapping[str, _Seen], asked: set[str]) -> None:
    # The tools an LLM agent called in a step, and what each answered: ADK's
    # own calls (hand-offs, questions) aren't tools.
    if not chain:
        return
    step = seen[chain[-1][0]]
    calls = step.calls
    for call in event.get_function_calls():
        if call.name == REQUEST_CONFIRMATION:
            original = (call.args or {}).get("originalFunctionCall")
            called = calls.get(str(original.get("id"))) if isinstance(original, dict) else None
            if called is not None and call.id in asked:
                called["status"] = WAITING
                step.confirming.add(called["id"])
            continue
        if not call.id or not call.name or call.name.startswith("adk_") or call.name == "transfer_to_agent":
            continue
        calls[call.id] = {
            "id": call.id,
            "agent": event.author,
            "name": call.name,
            "args": as_data(call.args or {}),
            "status": RUNNING,
            "response": None,
            "at": _iso(event.timestamp),
        }
    for response in event.get_function_responses():
        called = calls.get(response.id or "")
        if called is None or called["id"] in step.confirming:
            continue
        answered = as_data(response.response)
        called["status"] = FAILED if _is_error(answered) else DONE
        called["response"] = _shortened(answered)


def _is_error(response: Any) -> bool:
    return isinstance(response, dict) and (
        "error" in response or response.get("status") == "error" or response.get("ok") is False
    )


def _shortened(value: Any) -> Any:
    text_ = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text_) <= MAX_RESPONSE else {"truncated": text_[:MAX_RESPONSE]}


def _failed_ends(nodes: list[dict[str, Any]], seen: dict[str, _Seen]) -> None:
    # An End that ended its way failed: the run fails once every way has
    # ended, so the End itself hands on its {outcome, result}.
    for node in nodes:
        found = seen[node["id"]]
        output = found.output
        if node.get("kind") != "end" or found.status != DONE or not isinstance(output, dict):
            continue
        if output.get("outcome") == "failed":
            found.failed_at = found.finished
            said = as_text(output.get("result"))
            label = f"{text(node.get('name')) or node['id']} ({node['id']})"
            found.error = {"message": f"{label} failed the run" + (f": {said}" if said else ""), "code": "RunFailed"}


def _finished(event: Event, info: StepInfo) -> dict[str, Any] | None:
    # A step's own event that hands on nothing yet says it finished: the way
    # it took, or the start keeping a run's input of null.
    if event.error_code or event.error_message or event.get_function_calls():
        return None
    routed = event.actions is not None and event.actions.route is not None
    return {"output": None} if routed or info.kind == "start" else None


def _latest_run(events: list[Event]) -> list[Event]:
    # The events of the session's latest run (ADK's invocation): a pause
    # resumes the same one.
    latest = next((event.invocation_id for event in reversed(events) if event.node_info.path), None)
    if latest is None:
        return []
    return [event for event in events if event.invocation_id == latest]


def _steps_on(path: str, infos: Mapping[str, StepInfo], loops: set[str]) -> list[tuple[str, bool]]:
    """
    :param path: An event's node path: ``<graph>@1/<node>@1/...``.
    :return: The document's steps it's at or inside, outermost first, each with
        whether it's that step's own (the last one only).
    """
    segments = [segment.partition("@")[0] for segment in path.split("/")] if path else []
    chain: list[tuple[str, bool]] = []
    index = 1  # the first is the graph's own
    while index < len(segments):
        info = infos.get(segments[index])
        if info is None:
            break
        last = index == len(segments) - 1
        chain.append((info.id, last))
        if last:
            break
        # A loop's body runs its steps per item; inside anything else (a team,
        # a saved agent) they aren't the document's.
        if segments[index] in loops and segments[index + 1] == body_name(segments[index]):
            index += 2
            continue
        break
    return chain


def _sources(document: dict[str, Any], seen: Mapping[str, _Seen]) -> dict[str, set[str]]:
    # The steps that lead to each step.
    sources: dict[str, set[str]] = {}
    for edge in items(document.get("edges")):
        if isinstance(edge, dict) and edge.get("source") in seen and edge.get("target") in seen:
            sources.setdefault(str(edge["target"]), set()).add(str(edge["source"]))
    return sources


def _infer_silent(nodes: list[dict[str, Any]], sources: Mapping[str, set[str]], seen: dict[str, _Seen]) -> None:
    # A step that handed on nothing left no event; a step it alone leads to
    # that ran says it finished.
    changed = True
    while changed:
        changed = False
        for node in nodes:
            found = seen[node["id"]]
            if found.started is not None or found.handed_on:
                continue
            if any(
                sources.get(target) == {node["id"]} and (seen[target].started is not None or seen[target].handed_on)
                for target in seen
            ):
                found.handed_on = True
                changed = True


def _started_when_handed_on(sources: Mapping[str, set[str]], seen: Mapping[str, _Seen]) -> None:
    # A step started when the last step before it that handed on to it did so,
    # before its own first event (in a loop's body, the item's steps come
    # later: those keep their first event).
    for step, found in seen.items():
        if found.started is None:
            continue
        handed = [
            when
            for source in sources.get(step, ())
            if (when := seen[source].finished) is not None and when <= found.started
        ]
        if handed:
            found.started = max(handed)


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
