"""The engine: routing, loops, merges, replay after a wait, limits and failures."""

from __future__ import annotations

import httpx
import pytest

from forge_task_workflows.errors import WorkflowFailed
from forge_tasks.control import AwaitingDecision, Decision, LocalJobControl, WaitingUntil
from forge_tasks.tasks import JobStatus

from .helpers import Clock, run, services, workflow

START = (
    "start",
    "entry",
    {"input_schema": {"type": "object", "required": ["issue"], "properties": {"issue": {"type": "object"}}}},
)


async def test_a_straight_run_hands_on_what_each_step_makes() -> None:
    doc = workflow(
        [
            START,
            ("shape", "transform", {"expression": '{"key": input.issue.key, "loud": $uppercase(input.issue.key)}'}),
            ("done", "end", {"outcome": "succeeded", "result": 'steps.shape.output.loud & "!"'}),
        ],
        [("start", "next", "shape"), ("shape", "next", "done")],
    )
    result = await run(doc, input={"issue": {"key": "xmen-12"}})
    assert result.status is JobStatus.OK
    assert result.detail["result"] == "XMEN-12!"
    assert result.detail["steps"] == 3  # start, shape and the end


async def test_an_input_that_doesnt_fit_the_start_step_fails_the_run() -> None:
    doc = workflow([START], [])
    with pytest.raises(WorkflowFailed, match="doesn't fit its start step"):
        await run(doc, input={"nothing": True})


@pytest.mark.parametrize("count, expected", [(10, "true"), (9, "false"), (0, "false")])
@pytest.mark.parametrize("kind", ["if", "match"])
async def test_autocompleted_references_route_conditions(kind: str, count: int, expected: str) -> None:
    condition = '{{ steps.measure.output.count }} > 9 and {{ previous.priority }} = "high"'
    config = {"condition": condition} if kind == "if" else {"arms": [{"id": "true", "condition": condition}]}
    doc = workflow(
        [
            ("start", "entry", {}),
            ("measure", "transform", {"expression": '{"count": input.count, "priority": "high"}'}),
            ("check", kind, config),
            ("yes", "end", {"result": '"true"'}),
            ("no", "end", {"result": '"false"'}),
        ],
        [
            ("start", "next", "measure"),
            ("measure", "next", "check"),
            ("check", "true", "yes"),
            ("check", "false" if kind == "if" else "otherwise", "no"),
        ],
    )
    result = await run(doc, input={"count": count})
    assert result.status is JobStatus.OK
    assert result.detail["result"] == expected


async def test_logic_steps_route_and_hand_on_what_came_into_them() -> None:
    doc = workflow(
        [
            START,
            ("classify", "transform", {"expression": '{"kind": "bug", "severity": "high"}'}),
            (
                "route",
                "match",
                {
                    "arms": [
                        {
                            "id": "urgent",
                            "label": "Urgent",
                            "condition": 'steps.classify.output.kind = "bug" and steps.classify.output.severity = "high"',
                        },
                        {"id": "bug", "label": "Bug", "condition": 'steps.classify.output.kind = "bug"'},
                    ]
                },
            ),
            ("check", "if", {"condition": 'previous.severity = "high"'}),
            ("switch", "switch", {"value": "previous.kind", "cases": [{"id": "is_bug", "value": "bug"}]}),
            ("urgent_end", "end", {"outcome": "succeeded", "result": '"urgent " & previous.kind'}),
            ("other_end", "end", {"outcome": "succeeded", "result": '"other"'}),
        ],
        [
            ("start", "next", "classify"),
            ("classify", "next", "route"),
            ("route", "urgent", "check"),
            ("route", "bug", "other_end"),
            ("check", "true", "switch"),
            ("switch", "is_bug", "urgent_end"),
        ],
    )
    result = await run(doc, input={"issue": {}})
    # previous passes through the match, the if and the switch: still classify's output.
    assert result.detail["result"] == "urgent bug"


async def test_a_loop_runs_its_body_per_item_several_at_once() -> None:
    doc = workflow(
        [
            START,
            (
                "each",
                "loop",
                {"items": "input.issue.labels", "item_name": "label", "concurrency": 3, "max_iterations": 10},
            ),
            ("shout", "transform", {"expression": '$uppercase(label) & "#" & $string(index)'}),
            ("done", "end", {"outcome": "succeeded", "result": "steps.each.output"}),
        ],
        [("start", "next", "each"), ("each", "each", "shout"), ("shout", "next", "each"), ("each", "done", "done")],
    )
    result = await run(doc, input={"issue": {"labels": ["a", "b", "c", "d"]}})
    assert result.detail["result"] == {"count": 4, "results": ["A#0", "B#1", "C#2", "D#3"]}


async def test_the_run_activity_names_the_step_each_checkpoint_is_at() -> None:
    doc = workflow(
        [
            START,
            ("each", "loop", {"items": "input.issue.labels", "item_name": "label", "max_iterations": 10}),
            ("shout", "transform", {"expression": "$uppercase(label)"}),
            ("done", "end", {"outcome": "succeeded", "result": "steps.each.output.count"}),
        ],
        [("start", "next", "each"), ("each", "each", "shout"), ("shout", "next", "each"), ("each", "done", "done")],
    )
    control = LocalJobControl()
    await run(doc, input={"issue": {"labels": ["a", "b"]}}, control=control)
    assert control.checkpoint_messages == [
        "Start (start)",
        "Shout (shout), item 1 of Each",
        "Shout (shout), item 2 of Each",
        "Each (each)",
        "Done (done)",
        "The run succeeded",
    ]
    # Notes on a loop's body say which item, too.
    assert "Shout (shout), item 2 of Each finished" in [message for message, _ in control.notes]


async def test_a_loop_over_too_many_items_fails() -> None:
    doc = workflow(
        [START, ("each", "loop", {"items": "[1..5]", "max_iterations": 3}), ("x", "transform", {"expression": "1"})],
        [("start", "next", "each"), ("each", "each", "x"), ("x", "next", "each")],
    )
    with pytest.raises(WorkflowFailed, match="has 5 items to go through; the most it may is 3"):
        await run(doc, input={"issue": {}})


async def test_merge_all_waits_for_every_way_and_any_goes_on_at_the_first() -> None:
    def doc(mode: str) -> dict:
        return workflow(
            [
                START,
                ("a", "transform", {"expression": '"A"'}),
                ("b", "transform", {"expression": '"B"'}),
                ("join", "merge", {"mode": mode}),
                ("after", "transform", {"expression": "previous"}),
            ],
            [
                ("start", "next", "a"),
                ("start", "next", "b"),
                ("a", "next", "join"),
                ("b", "next", "join"),
                ("join", "next", "after"),
            ],
        )

    all_of = await run(doc("all"), input={"issue": {}})
    assert all_of.detail["result"] == {"a": "A", "b": "B"}
    any_of = await run(doc("any"), input={"issue": {}})
    assert any_of.detail["result"] == {"a": "A"}


async def test_an_approval_pauses_and_the_run_carries_on_after_the_decision_without_repeating_a_step() -> None:
    sent: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    doc = workflow(
        [
            START,
            (
                "move",
                "http",
                {
                    "method": "POST",
                    "url": "https://api.example.com/issues/{{ input.issue.key }}/transitions",
                    "body": '{"to": "review"}',
                },
            ),
            ("review", "approval", {"message": "Ship {{ input.issue.key }}?", "approvers": "org:admin"}),
            ("yes", "end", {"outcome": "succeeded", "result": "steps.review.output.comment"}),
            ("no", "end", {"outcome": "failed", "result": '"rejected"'}),
        ],
        [
            ("start", "next", "move"),
            ("move", "success", "review"),
            ("review", "approved", "yes"),
            ("review", "rejected", "no"),
        ],
    )
    control = LocalJobControl()
    svc = services(http=api)
    with pytest.raises(AwaitingDecision) as waiting:
        await run(doc, input={"issue": {"key": "XMEN-12"}}, control=control, svc=svc)
    assert waiting.value.reason == "Ship XMEN-12?"
    control.decide(waiting.value.key, Decision(approved=True, comment="Looks right", actor_name="Lead"))
    result = await run(doc, input={"issue": {"key": "XMEN-12"}}, control=control, svc=svc)
    assert result.detail["result"] == "Looks right"
    # The request went out once, not again when the run carried on.
    assert [str(request.url) for request in sent] == ["https://api.example.com/issues/XMEN-12/transitions"]


async def test_a_rejection_takes_the_rejected_way() -> None:
    doc = workflow(
        [
            START,
            ("review", "approval", {"message": "Ship?"}),
            ("no", "end", {"outcome": "failed", "result": "steps.review.output"}),
        ],
        [("start", "next", "review"), ("review", "rejected", "no")],
    )
    control = LocalJobControl()
    with pytest.raises(AwaitingDecision) as waiting:
        await run(doc, input={"issue": {}}, control=control)
    control.decide(waiting.value.key, Decision(approved=False, comment="Not yet", actor_name="Lead"))
    result = await run(doc, input={"issue": {}}, control=control)
    assert result.status is JobStatus.FAILED
    assert result.detail["result"]["approved"] is False
    assert result.detail["result"]["decided_by"] == "Lead"


async def test_a_long_delay_lets_the_worker_go_and_a_short_one_waits_in_place() -> None:
    doc = workflow(
        [
            START,
            ("wait", "delay", {"amount": 2, "unit": "hours"}),
            ("done", "end", {"outcome": "succeeded", "result": "steps.wait.output"}),
        ],
        [("start", "next", "wait"), ("wait", "next", "done")],
    )
    clock = Clock()
    control = LocalJobControl()
    svc = services(clock=clock)
    with pytest.raises(WaitingUntil) as waiting:
        await run(doc, input={"issue": {}}, control=control, svc=svc)
    assert waiting.value.when == clock.now.replace(hour=14)
    clock.advance(hours=1, minutes=59, seconds=50)
    result = await run(doc, input={"issue": {}}, control=control, svc=svc)
    assert clock.slept == [10.0]
    assert result.detail["result"]["resumed_at"].startswith("2026-09-26T14:00:00")


async def test_a_failing_step_takes_its_error_way_or_fails_the_run() -> None:
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    read = ("read", "http", {"url": "https://api.example.com/issues/1"})
    with_way = workflow(
        [START, read, ("failed", "end", {"outcome": "succeeded", "result": "steps.read.error"})],
        [("start", "next", "read"), ("read", "error", "failed")],
    )
    result = await run(with_way, input={"issue": {}}, svc=services(http=gone))
    assert result.detail["result"] == {"message": "It answered 404", "status": 404, "body": "gone"}
    without = workflow([START, read], [("start", "next", "read")])
    with pytest.raises(WorkflowFailed, match=r"Read \(read\) failed: It answered 404"):
        await run(without, input={"issue": {}}, svc=services(http=gone))


async def test_a_connection_that_comes_back_runs_steps_again_until_the_limit() -> None:
    doc = workflow(
        [START, ("a", "transform", {"expression": "1"}), ("b", "transform", {"expression": "2"})],
        [("start", "next", "a"), ("a", "next", "b"), ("b", "next", "a")],
    )
    with pytest.raises(WorkflowFailed, match="ran 5 times in one run"):
        await run(doc, input={"issue": {}}, svc=services(max_visits_per_step=5))


async def test_a_polling_loop_through_a_condition_stops_when_it_holds() -> None:
    clock = Clock()
    doc = workflow(
        [
            START,
            ("check", "transform", {"expression": "$count($keys(steps)) > 2"}),
            ("ready", "if", {"condition": "steps.check.output"}),
            ("pause", "delay", {"amount": 5, "unit": "seconds"}),
            ("done", "end", {"outcome": "succeeded", "result": '"ready"'}),
        ],
        [
            ("start", "next", "check"),
            ("check", "next", "ready"),
            ("ready", "true", "done"),
            ("ready", "false", "pause"),
            ("pause", "next", "check"),
        ],
    )
    result = await run(doc, input={"issue": {}}, svc=services(clock=clock))
    assert result.detail["result"] == "ready"
    assert clock.slept == [5.0]


async def test_an_expression_that_fails_says_where() -> None:
    doc = workflow([START, ("bad", "transform", {"expression": "$uppercase(1)"})], [("start", "next", "bad")])
    with pytest.raises(WorkflowFailed, match=r"Bad \(bad\) failed: Its expression failed"):
        await run(doc, input={"issue": {}})


async def test_a_workflow_with_expressions_that_dont_compile_never_starts() -> None:
    sent: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    doc = workflow(
        [
            ("start", "entry", {}),
            ("call", "http", {"method": "POST", "url": "https://api.example.com/x", "body": '{"key": }'}),
            ("ask", "approval", {"message": "Ship {{ input.key ) }}?"}),
            ("done", "end", {"outcome": "succeeded", "result": '"ok"'}),
        ],
        [("start", "next", "call"), ("call", "success", "ask"), ("ask", "approved", "done")],
    )
    with pytest.raises(WorkflowFailed) as failed:
        await run(doc, svc=services(http=api))
    message = str(failed.value)
    assert "Call (call): its body isn't valid JSONata" in message
    assert "Ask (ask): its message has {{ input.key ) }}" in message
    assert sent == []  # nothing ran
