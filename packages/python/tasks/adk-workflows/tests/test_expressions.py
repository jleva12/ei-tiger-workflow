"""The builder's references must evaluate the same way on a worker."""

import json
from pathlib import Path

import pytest

from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.expressions import Evaluator, as_text, plain

# The web console's tests read the same cases.
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "expression-references.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["expression"])
def test_builder_references_keep_their_types(case: dict) -> None:
    actual = Evaluator().evaluate(case["expression"], FIXTURES["data"])
    assert actual == case["value"]
    assert type(actual) is type(case["value"])


@pytest.mark.parametrize("expression", FIXTURES["invalid"])
def test_invalid_references_fail_their_step(expression: str) -> None:
    with pytest.raises(StepFailed) as failed:
        Evaluator().evaluate(expression, FIXTURES["data"])
    assert failed.value.details == {"expression": expression.strip()}


def test_text_settings_still_interpolate_references() -> None:
    assert Evaluator().render("Count: {{ previous.count }}", FIXTURES["data"]) == "Count: 10"


def test_a_failing_expression_says_what_failed() -> None:
    with pytest.raises(StepFailed, match=r"^Its URL failed: "):
        Evaluator().evaluate("$number('x') + ", {}, what="Its URL")


def test_conditions_hold_only_when_true() -> None:
    evaluator = Evaluator()
    assert evaluator.truthy("previous.approved", FIXTURES["data"])
    assert not evaluator.truthy("previous.count", FIXTURES["data"])
    assert not evaluator.truthy("previous.missing", FIXTURES["data"])


def test_values_as_text_and_plain_json() -> None:
    assert [as_text(value) for value in (None, "a", True, 1.5, {"k": [1]})] == ["", "a", "true", "1.5", '{"k": [1]}']
    assert plain({"when": Path("/x"), "items": (1, 2)}) == {"when": "/x", "items": [1, 2]}
