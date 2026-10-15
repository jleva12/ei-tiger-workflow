"""
A Decision board: its choices laid out as their "ask when" says, each asked
of people on its own, each taking its way as soon as it's decided while the
others still wait, and Done once every one is; as a real run of the task
(ADK's runner, a SQLite session) on a local control.
"""

from typing import Any

import pytest
from google.adk.sessions import DatabaseSessionService

from forge_task_adk_workflows.graph import AgentBuildError, build_agent
from forge_task_adk_workflows.steps import run_progress
from forge_tasks.control import AwaitingDecision, Decision
from forge_tasks.tasks import JobStatus

from .test_graph import START_NODE, agent, end, node, transform
from .test_task import payload, sessions, worker

__all__ = ["sessions"]

SHIP = "board@test_agent@1/board__choice__ship@1"
TELL = "board@test_agent@1/board__choice__tell@1"


def board(**config: Any) -> dict[str, Any]:
    return node(
        "board",
        "decision_board",
        {
            "message": "What next for {{ previous.title }}?",
            "approvers": "org:member",
            "timeout_hours": 0,
            "choices": [
                {"id": "ship", "label": "Ship it", "question": "Ship {{ previous.title }}?", "ask_when": ""},
                {"id": "tell", "label": "Tell the team", "question": "", "ask_when": "previous.risky"},
                {"id": "undo", "label": "Roll back", "question": "", "ask_when": "previous.broken = true"},
            ],
            **config,
        },
    )


def release() -> dict[str, Any]:
    """A plan, then a board whose choices each lead somewhere, and a summary at Done."""
    return agent(
        [
            START_NODE,
            transform("plan", '{"title": "v2", "risky": true}'),
            board(),
            transform("shipped", '{"shipped": previous.decided.label, "by": previous.decided.decided_by}'),
            transform("told", '{"told": true}'),
            transform("quiet", '{"quiet": previous.decided.comment}'),
            end("summary", '{"accepted": previous.accepted, "rejected": previous.rejected}'),
        ],
        [
            ("start", "next", "plan"),
            ("plan", "next", "board"),
            ("board", "ship:accepted", "shipped"),
            ("board", "tell:accepted", "told"),
            ("board", "tell:rejected", "quiet"),
            ("board", "done", "summary"),
        ],
    )


async def steps_by_id(w: Any, run: Any) -> dict[str, Any]:
    """The run's steps as its run page shows them, by ID, and the edges it took (``taken``)."""
    session = await w.sessions.get_session(app_name="adk_workflows", user_id=run.run_as, session_id=run.session_id)
    progress = run_progress(session, run.document)
    return {step["id"]: step for step in progress["steps"]} | {"taken": progress["taken_edges"]}


async def test_each_choice_takes_its_way_as_soon_as_its_decided(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    run = payload(release(), {})

    # Laid out as their "ask when" says, and asked together.
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.keys == [SHIP, TELL]
    ship, tell = w.control.gates
    assert ship["reason"] == "Ship v2?" and tell["reason"] == "Tell the team?"
    assert ship["details"]["kind"] == "approval" and ship["details"]["approvers"] == "org:member"
    assert ship["details"]["board"] == {
        "name": "Board",
        "message": "What next for v2?",
        "choice": "ship",
        "label": "Ship it",
        "choices": 2,
    }

    # The second decided first: its way goes on, the first still waits.
    w.control.decide(TELL, Decision(approved=False, comment="Not this time", actor_id="u-2", actor_name="Bea"))
    w.control.gates.clear()
    with pytest.raises(AwaitingDecision) as waiting:
        await w.run(run)
    assert waiting.value.keys == [SHIP]
    steps = await steps_by_id(w, run)
    assert steps["quiet"]["status"] == "done" and steps["quiet"]["output"] == {"quiet": "Not this time"}
    assert (steps["told"]["status"], steps["shipped"]["status"]) == ("not_reached", "not_reached")
    assert steps["board"]["status"] == "waiting"
    assert steps["board"]["output"]["open"] == ["ship"]
    assert steps["board"]["output"]["rejected"] == ["tell"]

    w.control.decide(SHIP, Decision(approved=True, actor_id="u-1", actor_name="Ada"))
    result = await w.run(run)

    assert result.status is JobStatus.OK
    steps = await steps_by_id(w, run)
    assert steps["shipped"]["output"] == {"shipped": "Ship it", "by": "Ada"}
    assert steps["summary"]["status"] == "done"
    assert steps["board"]["status"] == "done"
    assert steps["board"]["output"]["accepted"] == ["ship"]
    assert set(steps["board"]["output"]["decisions"]) == {"ship", "tell"}
    # Every way it took is taken, not only the last.
    assert {
        "board:ship:accepted->shipped",
        "board:tell:rejected->quiet",
        "board:done->summary",
    } <= set(steps["taken"])
    assert "board:tell:accepted->told" not in steps["taken"]
    # Each way's step was handed the board as it stood when its choice was decided.
    assert (steps["quiet"]["input_from"], steps["quiet"]["input"]["decided"]["choice"]) == ("board", "tell")
    assert (steps["shipped"]["input_from"], steps["shipped"]["input"]["decided"]["choice"]) == ("board", "ship")
    assert steps["quiet"]["started_at"] <= steps["quiet"]["finished_at"] < steps["shipped"]["started_at"]
    assert "Board (board), Tell the team: rejected by Bea" in w.control.said()
    assert "Board (board), Ship it: approved by Ada" in w.control.said()
    # Each way that ended is one of the run's endings, in the order they ended.
    assert [(ended["step"], ended["result"]) for ended in result.detail["result"]] == [
        ("quiet", {"quiet": "Not this time"}),
        ("shipped", {"shipped": "Ship it", "by": "Ada"}),
        ("summary", {"accepted": ["ship"], "rejected": ["tell"]}),
    ]


async def test_a_board_that_lays_nothing_out_is_done_at_once(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = agent(
        [
            START_NODE,
            board(choices=[{"id": "ship", "label": "Ship it", "ask_when": "false"}]),
            end("summary", '{"open": previous.open, "accepted": previous.accepted}'),
        ],
        [("start", "next", "board"), ("board", "done", "summary")],
    )

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK
    assert result.detail["result"] == {"open": [], "accepted": []}
    assert w.control.gates == []


async def test_a_board_whose_done_leads_nowhere_ends_its_way_there(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)
    document = agent(
        [START_NODE, board(choices=[{"id": "ship", "label": "Ship it"}])],
        [("start", "next", "board")],
    )
    run = payload(document, {})
    with pytest.raises(AwaitingDecision):
        await w.run(run)
    w.control.decide(
        "board@test_agent@1/board__choice__ship@1", Decision(approved=True, actor_id="u-1", actor_name="Ada")
    )

    result = await w.run(run)

    assert result.status is JobStatus.OK
    assert result.detail["result"]["accepted"] == ["ship"]


def test_a_merge_waiting_for_two_choices_is_refused() -> None:
    document = agent(
        [
            START_NODE,
            board(choices=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]),
            node("both", "merge", {"mode": "all"}),
        ],
        [
            ("start", "next", "board"),
            ("board", "a:accepted", "both"),
            ("board", "b:accepted", "both"),
        ],
    )
    with pytest.raises(AgentBuildError, match="may never go on"):
        build_agent(document)


def test_a_choice_id_that_isnt_one_is_refused() -> None:
    document = agent(
        [START_NODE, board(choices=[{"id": "a:b", "label": "A"}])],
        [("start", "next", "board")],
    )
    with pytest.raises(AgentBuildError, match="choice's ID"):
        build_agent(document)
