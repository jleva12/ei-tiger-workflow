"""JSONata's regular expressions in a run (support/patterns.py), on
forge-common's bounded engine (forge_common.patterns, whose tests hold its
uses, compiling and JSON Schemas' patterns to their time)."""

import time

import pytest
from forge_common.patterns import InvalidPattern, PatternTimeout

from forge_jsonata.regex_engine import RegexFlags
from forge_task_adk_workflows.support.patterns import regex_engine

RUNAWAY = "^(a|aa)+$"
LONG = "a" * 60 + "b"


def test_jsonatas_regular_expressions_are_bounded_and_keep_their_flags() -> None:
    compile_bounded = regex_engine(0.05)

    began = time.perf_counter()
    with pytest.raises(PatternTimeout, match=r"took longer than 50 ms"):
        compile_bounded(RUNAWAY, RegexFlags()).search(LONG)
    assert time.perf_counter() - began < 0.5
    assert compile_bounded("HELLO", RegexFlags(case_insensitive=True)).search("say hello") is not None
    assert compile_bounded("^b", RegexFlags(multiline=True)).search("a\nb") is not None
    assert compile_bounded("^b", RegexFlags()).search("a\nb") is None
    with pytest.raises(InvalidPattern, match="doesn't compile: its repeats come to more than"):
        compile_bounded("((a{1000}){1000}){1000}", RegexFlags())
