"""A run waiting for a run it started (a workflow its LLM agent called), as
outside apps see it: still working, with nothing for them to answer, while
it waits for that run alone; asking, while it waits for a person too."""

from typing import Any

from a2a.types import TaskState

from forge_admin.adk_workflows.a2a import update_of
from forge_admin.adk_workflows.runs import going
from forge_admin.adk_workflows.runtime import runtime_gates, runtime_pause

CHILD = {
    "id": "g-child",
    "key": "call-1",
    "kind": "child_run",
    "reason": "Waits for Refunds (run cccccccc) to end",
    "details": {"kind": "child_run", "child_run_id": "c" * 32, "step_name": "Helper"},
    "requested_at": "2026-10-18T12:00:00+00:00",
    "deadline": "2026-10-18T12:02:01+00:00",
}
APPROVAL = {
    "id": "g-review",
    "key": "review",
    "kind": "approval",
    "reason": "Ship it?",
    "details": {"kind": "approval", "approvers": "org:member", "step_name": "Review"},
    "requested_at": "2026-10-18T12:00:00+00:00",
    "deadline": None,
}


def paused(*gates: dict[str, Any]) -> dict[str, Any]:
    return {"id": "r" * 32, "status": "paused", "pause": gates[0], "gates": list(gates)}


def test_a_run_waiting_only_for_a_run_it_started_asks_nothing() -> None:
    run = paused(CHILD)

    assert runtime_pause(run) is None
    [gate] = runtime_gates(run)
    assert (gate.kind, gate.reason, gate.deadline) == (
        "child_run",
        CHILD["reason"],
        CHILD["deadline"],
    )
    state, _, _ = update_of(run)
    assert state == TaskState.TASK_STATE_WORKING
    assert going(run)  # a wait for it (the runtime's, a chat agent's) waits on


def test_a_run_waiting_for_a_person_too_asks_them() -> None:
    run = paused(CHILD, APPROVAL)

    pause = runtime_pause(run)
    assert pause is not None and pause.id == "g-review"
    state, _, _ = update_of(run)
    assert state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert not going(run)  # it waits for someone: a wait for it stops
