"""The editor's references must compile and evaluate the same way on a worker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_task_workflows.errors import StepFailed
from forge_task_workflows.expressions import Evaluator, compile_errors

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "expression-references.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["expression"])
def test_editor_references_compile_and_keep_their_types(case: dict) -> None:
    assert compile_errors(case["expression"]) is None
    actual = Evaluator().evaluate(case["expression"], FIXTURES["data"])
    assert actual == case["value"]
    assert type(actual) is type(case["value"])


@pytest.mark.parametrize("expression", FIXTURES["invalid"])
def test_invalid_references_fail_preflight_and_execution(expression: str) -> None:
    assert compile_errors(expression)
    with pytest.raises(StepFailed):
        Evaluator().evaluate(expression, FIXTURES["data"])


def test_text_settings_still_interpolate_references() -> None:
    assert Evaluator().render("Count: {{ previous.count }}", FIXTURES["data"]) == "Count: 10"
