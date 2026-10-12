"""
A run's trace, as its run page's Trace tab shows it to the organization's
admins: every event of the run in its ADK session, each with what it said and
did, and the session's state before and after the run, so the state at any
event can be rebuilt by applying each event's ``state_delta`` in turn.

::

    {"events": [...], "state_before": {...}, "state_after": {...}, "state": {...},
     "later_changes": bool, "omitted": int, "invocation_id": str | None}

Each event, in the session's order::

    {"seq", "id", "invocation_id", "at", "author", "role", "branch", "path", "step", "steps",
     "own", "text", "thought", "calls", "responses", "files", "output",
     "has_output", "output_for", "state_delta", "artifact_delta", "route",
     "transfer_to_agent", "escalate", "error", "usage", "model", "metadata",
     "partial", "interrupted", "long_running"}

- ``step`` / ``steps`` / ``own``: the document's step it's at (the innermost),
  the steps it's inside, outermost first, and whether it's that step's own
  event (not a team's sub-agent's, nor a saved agent's step's); None and
  ``[]`` for the person's message and ADK's own.
- ``text`` / ``thought``: what its message says, and the model's thoughts.
- ``calls`` / ``responses``: the functions it called (``{"id", "name",
  "args"}``) and the answers it carries (``{"id", "name", "response"}``),
  ADK's own (questions, confirmations, hand-offs) too.
- ``files``: the files its message carries, by type and size, never their
  bytes.
- ``output``: what it handed on (``has_output`` says it did, for a None);
  ``output_for`` the node paths it hands on for.
- ``state_delta`` / ``artifact_delta``: what it set in the session's state,
  and the artifacts it saved (name to version).
- ``usage``: the model's tokens, when it's a model's answer.
- ``metadata``: what Forge rode on it (an Error way's error, a tool's cut
  off answer).

``state_before`` is the session's state as the first listed event found it:
the state the session started with (what no event set) and what earlier
events set (an earlier run in the same session, the events left out).
``state_after`` is it once the run's events applied; ``state`` the session's
now, which a later run in the same session may have changed
(``later_changes``). ``temp:`` keys aren't kept, so they're in no state.

Long strings are cut short (``MAX_TEXT``), and a run with more than
``MAX_EVENTS`` events lists its latest (``omitted``: how many earlier ones
aren't).
"""

from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from google.adk.events import Event
from google.adk.sessions import Session
from google.genai import types

from forge_task_adk_workflows.graph.data import as_data
from forge_task_adk_workflows.steps import document_nodes, latest_run, step_infos, steps_on

#: The longest a string in a trace is, in characters.
MAX_TEXT = 20_000
#: The most events a trace lists: a run's latest.
MAX_EVENTS = 2_000


def run_trace(session: Session | None, document: dict[str, Any], *, invocation_id: str | None = None) -> dict[str, Any]:
    """
    :param session: The run's ADK session, as its session service reads it;
        None before the run has one.
    :param document: The ``forge.agent/v1`` document the run runs.
    :param invocation_id: The run's ADK invocation: only its events are
        listed. None reads the session's latest.
    :return: The run's trace.
    """
    if session is None:
        return {
            "events": [],
            "state_before": {},
            "state_after": {},
            "state": {},
            "later_changes": False,
            "omitted": 0,
            "invocation_id": invocation_id or None,
        }
    infos, loops = step_infos(document_nodes(document))
    if invocation_id is None:
        mine = latest_run(session.events)
        invocation_id = mine[0].invocation_id if mine else None
    else:
        mine = [event for event in session.events if event.invocation_id == invocation_id]
    traced = _traced(session, mine, {invocation_id} if invocation_id else set(), infos, loops)
    return {**traced, "invocation_id": invocation_id or None}


def session_trace(session: Session, *, invocations: Collection[str] | None = None) -> dict[str, Any]:
    """
    A conversation's trace, as a run's (:func:`run_trace`): every event of
    an ADK session, each turn's (invocation's) in turn, and the session's
    state before its first and after its last.

    :param session: The session.
    :param invocations: Only these invocations' events (the turns one may
        read); None for every one.
    :return: ``{"events", "state_before", "state_after", "state",
        "later_changes", "omitted"}``; ``step`` is None throughout.
    """
    mine = [event for event in session.events if invocations is None or event.invocation_id in invocations]
    turns = {event.invocation_id for event in mine}
    return _traced(session, mine, turns, {}, set())


def _traced(
    session: Session,
    mine: list[Event],
    turns: Collection[str],
    infos: Mapping[str, Any],
    loops: set[str],
) -> dict[str, Any]:
    # The events listed (the latest MAX_EVENTS of ``mine``), and the state
    # before and after them, ``turns``' changes applied.
    listed = mine[-MAX_EVENTS:]
    first = listed[0] if listed else None
    last = listed[-1] if listed else None
    state = dict(session.state)
    if first is None or last is None:
        before = after = state
        later = False
    else:
        # What the session started with (what no event set), then what each
        # event before the first listed set.
        touched = {key for event in session.events for key in _delta(event)}
        before = {key: value for key, value in state.items() if key not in touched}
        at = session.events.index(first)
        for event in session.events[:at]:
            before.update(_delta(event))
        after = dict(before)
        for event in session.events[at:]:
            if event.invocation_id in turns:
                after.update(_delta(event))
        later = any(
            _delta(event)
            for event in session.events[session.events.index(last) + 1 :]
            if event.invocation_id not in turns
        )
    return {
        "events": [
            _event(index, event, infos, loops) for index, event in enumerate(listed, len(mine) - len(listed) + 1)
        ],
        "state_before": _clipped(as_data(before)),
        "state_after": _clipped(as_data(after)),
        "state": _clipped(as_data(state)),
        "later_changes": later,
        "omitted": len(mine) - len(listed),
    }


def _delta(event: Event) -> dict[str, Any]:
    delta = event.actions.state_delta if event.actions else None
    return {key: value for key, value in (delta or {}).items() if not key.startswith("temp:")}


def _event(seq: int, event: Event, infos: Mapping[str, Any], loops: set[str]) -> dict[str, Any]:
    path = event.node_info.path or ""
    chain = steps_on(path, infos, loops)
    content = event.content
    parts = (content.parts or []) if content else []
    actions = event.actions
    usage = event.usage_metadata
    error = (
        {"code": event.error_code, "message": event.error_message} if event.error_code or event.error_message else None
    )
    return {
        "seq": seq,
        "id": event.id,
        "invocation_id": event.invocation_id,
        "at": _iso(event.timestamp),
        "author": event.author,
        "role": content.role if content else None,
        "branch": event.branch,
        "path": path,
        "step": chain[-1][0] if chain else None,
        "steps": [step for step, _ in chain],
        "own": bool(chain and chain[-1][1]),
        "text": _clip("".join(part.text for part in parts if part.text and not part.thought)) or None,
        "thought": _clip("".join(part.text for part in parts if part.text and part.thought)) or None,
        "calls": [
            {"id": call.id, "name": call.name, "args": _clipped(as_data(call.args or {}))}
            for call in event.get_function_calls()
        ],
        "responses": [
            {"id": response.id, "name": response.name, "response": _clipped(as_data(response.response))}
            for response in event.get_function_responses()
        ],
        "files": [found for part in parts if (found := _file(part))],
        "output": _clipped(as_data(event.output)),
        "has_output": event.output is not None,
        "output_for": list(event.node_info.output_for or []),
        "state_delta": _clipped(as_data(_delta(event))),
        "artifact_delta": dict(actions.artifact_delta) if actions and actions.artifact_delta else {},
        "route": _clipped(as_data(actions.route)) if actions else None,
        "transfer_to_agent": actions.transfer_to_agent if actions else None,
        "escalate": bool(actions and actions.escalate),
        "error": error,
        "usage": _usage(usage) if usage else None,
        "model": event.model_version,
        "metadata": _clipped(as_data(event.custom_metadata)) if event.custom_metadata else None,
        "partial": bool(event.partial),
        "interrupted": bool(event.interrupted),
        "long_running": sorted(event.long_running_tool_ids or []),
    }


def _file(part: types.Part) -> dict[str, Any] | None:
    # A file a message carries, by type and size: never its bytes.
    if part.inline_data is not None:
        data = part.inline_data.data or b""
        return {
            "name": part.inline_data.display_name,
            "media_type": part.inline_data.mime_type,
            "size_bytes": len(data),
        }
    if part.file_data is not None:
        return {
            "name": part.file_data.display_name,
            "media_type": part.file_data.mime_type,
            "uri": part.file_data.file_uri,
        }
    return None


def _usage(usage: types.GenerateContentResponseUsageMetadata) -> dict[str, int | None]:
    return {
        "prompt_tokens": usage.prompt_token_count,
        "output_tokens": usage.candidates_token_count,
        "thoughts_tokens": usage.thoughts_token_count,
        "cached_tokens": usage.cached_content_token_count,
        "tool_prompt_tokens": usage.tool_use_prompt_token_count,
        "total_tokens": usage.total_token_count,
    }


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return f"{text[:MAX_TEXT]}… ({len(text) - MAX_TEXT:,} more characters)"


def _clipped(value: Any) -> Any:
    # JSON with each long string cut short.
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        return {key: _clipped(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clipped(item) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value):,} bytes>"
    return value


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def saved_files(
    session: Session | None, document: dict[str, Any], *, invocation_id: str | None
) -> dict[tuple[str, int], dict[str, Any]]:
    """
    :param session: The run's ADK session; None before it has one.
    :param document: The document the run runs.
    :param invocation_id: The run's ADK invocation: only its events count.
    :return: Each artifact version the run's events saved (``artifact_delta``),
        by name and version: the step that saved it (the innermost), who (its
        agent) and when.
    """
    if session is None or not invocation_id:
        return {}
    infos, loops = step_infos(document_nodes(document))
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for event in session.events:
        delta = event.actions.artifact_delta if event.actions else None
        if event.invocation_id != invocation_id or not delta:
            continue
        chain = steps_on(event.node_info.path or "", infos, loops)
        for name, version in delta.items():
            found[(name, int(version))] = {
                "step": chain[-1][0] if chain else None,
                "author": event.author,
                "at": _iso(event.timestamp),
            }
    return found
