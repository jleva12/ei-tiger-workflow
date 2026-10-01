"""Cyclic-flow rejection at definition time and the runner's visit-count backstop
(audit T7/T28)."""

from __future__ import annotations

import pytest

from etf import BatchStatus, ExitStatus, FlowTransition, JobDefinition, JobLauncher, Step
from etf.exceptions import EtfError
from helpers import RecordingStep, launch_request, make_cfg


def steps(*names: str) -> list[Step]:
    return [RecordingStep(n) for n in names]


def test_transition_cycle_is_rejected():
    with pytest.raises(ValueError, match="cycle"):
        JobDefinition(
            name="looper",
            steps=steps("a", "b"),
            transitions=[FlowTransition("b", "*", "a")],
        )


def test_self_loop_is_rejected():
    with pytest.raises(ValueError, match="cycle"):
        JobDefinition(
            name="self_looper",
            steps=steps("a"),
            transitions=[FlowTransition("a", "RETRY_ME", "a")],
        )


def test_cycle_via_linear_fallthrough_is_rejected():
    # b jumps back to a on one code; a falls through linearly to b -> a cycle.
    with pytest.raises(ValueError, match="cycle"):
        JobDefinition(
            name="fallthrough_looper",
            steps=steps("a", "b"),
            transitions=[FlowTransition("b", "REDO", "a")],
        )


def test_wildcard_masking_fallthrough_is_not_a_false_positive():
    # b's "*" wildcard ends the job, so b's linear fallthrough is unreachable and a
    # backward-looking layout ["b", "a"] with an explicit a->b edge is still acyclic.
    definition = JobDefinition(
        name="acyclic",
        steps=steps("b", "a"),
        transitions=[
            FlowTransition("b", "*", None),
            FlowTransition("a", "*", "b"),
        ],
    )
    assert definition.first_step == "b"


def test_branching_forward_flow_is_accepted():
    definition = JobDefinition(
        name="branching",
        steps=steps("a", "b", "c"),
        transitions=[
            FlowTransition("a", "SKIP_B", "c"),
            FlowTransition("b", "DONE", None),
        ],
    )
    assert definition.next_step("a", ExitStatus("SKIP_B")) == "c"
    assert definition.next_step("a", ExitStatus.COMPLETED) == "b"  # linear fallthrough
    assert definition.next_step("b", ExitStatus("DONE")) is None


async def test_visit_counter_backstop_aborts_runaway_flow():
    """A cycle smuggled in after validation (mutated definition) must abort, not spin
    in an infinite IDEMPOTENT_SKIP loop."""
    cfg, store = make_cfg(*steps("a", "b"), max_step_visits=5)
    definition = cfg.registry.get("job")
    # Bypass __post_init__ validation the way a buggy integration could: mutate the
    # live transition list to point b back at a.
    definition.transitions.append(FlowTransition("b", "*", "a"))

    with pytest.raises(EtfError, match="more than 5 times"):
        await JobLauncher(cfg).launch(launch_request())

    # Fail-fast (T1) recorded the abort; the run is not stuck RUNNING.
    run = (await store.list_runs())[0]
    assert run.status is BatchStatus.FAILED
