"""Regressions for compiled-expression reuse and evaluator limits."""

import re

import pytest

from forge_jsonata import JException, Jsonata, Timebox


@pytest.fixture
def clock(monkeypatch):
    milliseconds = [0]
    monkeypatch.setattr(Timebox, "current_milli_time", staticmethod(lambda: milliseconds[0]))
    monkeypatch.setattr(Timebox, "monotonic_milli_time", staticmethod(lambda: milliseconds[0]))
    return milliseconds


def test_timeout_starts_at_evaluation_and_resets_on_reuse(clock):
    expression = Jsonata("$", timeout=10)
    clock[0] = 1000
    assert expression.evaluate(1) == 1
    clock[0] = 2000
    assert expression.evaluate(2) == 2


def test_timeout_raises_exact_code_and_can_be_reused(clock):
    expression = Jsonata("$work($)", timeout=10)

    def work(milliseconds):
        clock[0] += milliseconds
        return milliseconds

    expression.register_lambda("work", work)
    with pytest.raises(JException) as error:
        expression.evaluate(11)
    assert error.value.error == "D1012"
    assert expression.evaluate(0) == 0


def test_stack_limit_raises_exact_code_and_can_be_reused():
    expression = Jsonata("($f := function($n) { $n = 0 ? 0 : $f($n - 1) + 1 }; $f($))", stack=10)
    with pytest.raises(JException) as error:
        expression.evaluate(100)
    assert error.value.error == "D1011"
    assert expression.evaluate(0) == 0


def test_binding_frame_limits_are_reset_for_each_evaluation(clock):
    bindings = Jsonata.Frame(None)
    bindings.set_runtime_bounds(10, 50)
    expression = Jsonata("$work($)")

    def work(milliseconds):
        clock[0] += milliseconds
        return milliseconds

    expression.register_lambda("work", work)
    with pytest.raises(JException) as error:
        expression.evaluate(11, bindings)
    assert error.value.error == "D1012"
    assert expression.evaluate(0, bindings) == 0


def test_reused_expression_eval_keeps_its_regex_engine():
    compiled_patterns = []

    def recording_engine(pattern, flags):
        compiled_patterns.append(pattern)
        return re.compile(pattern, re.I if flags.case_insensitive else 0)

    expression = Jsonata("$eval('$contains(\"abc\", /a/)')", recording_engine)
    unrelated_expression = Jsonata("1")
    assert unrelated_expression.evaluate(None) == 1
    assert expression.evaluate(None) is True
    assert compiled_patterns == ["a"]


def test_nested_python_evaluation_restores_outer_context():
    outer = Jsonata("($nested(); $eval('$value'))")
    inner = Jsonata("$value")
    outer.register_lambda("nested", lambda: inner.evaluate(None, {"value": "inner"}))
    assert outer.evaluate(None, {"value": "outer"}) == "outer"


def test_nested_eval_obeys_outer_timeout(clock):
    expression = Jsonata("$eval('$work()')", timeout=10)

    def work():
        clock[0] += 11

    expression.register_lambda("work", work)
    with pytest.raises(JException) as error:
        expression.evaluate(None)
    # $eval wraps runtime failures using its documented dynamic-evaluation code.
    assert error.value.error == "D3121"


def test_wall_clock_changes_do_not_change_elapsed_budget(monkeypatch, clock):
    expression = Jsonata("$work()", timeout=10)

    def work():
        monkeypatch.setattr(Timebox, "current_milli_time", staticmethod(lambda: 10**12))
        clock[0] += 1
        return 42

    expression.register_lambda("work", work)
    assert expression.evaluate(None) == 42
