"""The builder's references must evaluate the same way on a worker, off its
event loop; a condition read for each item of a list, in batches, within its
list timeout."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.expressions import Evaluator, as_text, plain

# The web console's tests read the same cases.
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "expression-references.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["expression"])
async def test_builder_references_keep_their_types(case: dict) -> None:
    actual = await Evaluator().evaluate(case["expression"], FIXTURES["data"])
    assert actual == case["value"]
    assert type(actual) is type(case["value"])


@pytest.mark.parametrize("expression", FIXTURES["invalid"])
async def test_invalid_references_fail_their_step(expression: str) -> None:
    with pytest.raises(StepFailed) as failed:
        await Evaluator().evaluate(expression, FIXTURES["data"])
    assert failed.value.details == {"expression": expression.strip()}


async def test_text_settings_still_interpolate_references() -> None:
    assert await Evaluator().render("Count: {{ previous.count }}", FIXTURES["data"]) == "Count: 10"
    assert await Evaluator().render("No parts", FIXTURES["data"]) == "No parts"


async def test_a_failing_expression_says_what_failed() -> None:
    with pytest.raises(StepFailed, match=r"^Its URL failed: "):
        await Evaluator().evaluate("$number('x') + ", {}, what="Its URL")


async def test_conditions_hold_only_when_true() -> None:
    evaluator = Evaluator()
    assert await evaluator.truthy("previous.approved", FIXTURES["data"])
    assert not await evaluator.truthy("previous.count", FIXTURES["data"])
    assert not await evaluator.truthy("previous.missing", FIXTURES["data"])


def test_values_as_text_and_plain_json() -> None:
    assert [as_text(value) for value in (None, "a", True, 1.5, {"k": [1]})] == ["", "a", "true", "1.5", '{"k": [1]}']
    assert plain({"when": Path("/x"), "items": (1, 2)}) == {"when": "/x", "items": [1, 2]}


# ---------------------------------------------------------------------- a condition per item

#: A condition that takes a while on each item (milliseconds).
SLOW = "$count([1..300][$ % 7 = 0]) > 0 and item.amount > 10"


def orders(count: int) -> list[dict]:
    return [{"id": index, "amount": index % 50, "note": "x" * 100} for index in range(count)]


async def test_a_lists_items_are_kept_when_the_condition_holds_in_order() -> None:
    items = orders(500)
    # What the condition reads besides holds the list too, as a step's output would.
    data = {"input": {"orders": items}, "steps": {"load": {"output": items}}, "limit": 40}

    kept = await Evaluator().kept("order.amount > limit and index % 2 = 0", items, data, item_name="order")

    assert kept == [item for index, item in enumerate(items) if item["amount"] > 40 and index % 2 == 0]
    assert await Evaluator().kept("", items, data, item_name="order") == []
    assert await Evaluator().kept("true", [], data, item_name="order") == []


async def test_a_long_list_takes_time_in_proportion_to_its_length() -> None:
    # Each item's evaluation used to check all the data it reads, the list among it: its length squared.
    items = orders(5000)
    data = {"steps": {"load": {"output": items}}}

    began = time.perf_counter()
    kept = await Evaluator().kept("item.amount > 10", items, data, item_name="item")

    assert len(kept) == sum(1 for item in items if item["amount"] > 10)
    assert time.perf_counter() - began < 2.0


async def test_a_lists_condition_leaves_the_event_loop_free() -> None:
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticking() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(ticking())
    began = time.perf_counter()
    try:
        kept = await Evaluator().kept(SLOW, orders(300), {}, item_name="item")
    finally:
        done.set()
        await ticker
    took = time.perf_counter() - began

    assert len(kept) == sum(1 for item in orders(300) if item["amount"] > 10)
    assert took > 0.2  # long enough to tell
    # The loop ran all along: never held for more than a moment.
    assert max(gaps) < 0.15, max(gaps)


async def test_a_lists_condition_past_its_list_timeout_fails() -> None:
    evaluator = Evaluator(list_timeout_ms=100)

    began = time.perf_counter()
    with pytest.raises(StepFailed, match=r"^Its condition took longer than 0.1 seconds over the list's 5000 items"):
        await evaluator.kept(SLOW, orders(5000), {}, item_name="item")

    # Within its list timeout, give or take a batch.
    assert time.perf_counter() - began < 1.0


async def test_a_lists_condition_that_fails_says_on_which_item() -> None:
    with pytest.raises(StepFailed, match=r"^Its condition \(item 1\) failed: "):
        await Evaluator().kept('$uppercase(item) = "A"', ["a", 1], {}, item_name="item")


async def test_a_lists_condition_stops_at_once_when_its_step_is_cancelled() -> None:
    evaluator = Evaluator()
    going = asyncio.create_task(evaluator.kept(SLOW, orders(5000), {}, item_name="item"))
    await asyncio.sleep(0.1)

    began = time.perf_counter()
    going.cancel()
    with pytest.raises(asyncio.CancelledError):
        await going

    assert time.perf_counter() - began < 0.1  # between two batches, not after the last


# ---------------------------------------------------------------------- regular expressions

CATASTROPHIC = {"text": "a" * 60 + "b"}  # ^(a|aa)+$ backtracks on it for ages


async def test_a_regular_expression_that_runs_away_fails_its_step_in_time_leaving_the_loop_free() -> None:
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticking() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(ticking())
    began = time.perf_counter()
    try:
        with pytest.raises(StepFailed, match=r"/\^\(a\|aa\)\+\$/ took longer than 200 ms"):
            await Evaluator(timeout_ms=200).evaluate("$match(text, /^(a|aa)+$/)", CATASTROPHIC)
    finally:
        done.set()
        await ticker

    assert time.perf_counter() - began < 1.0
    # Python's re would have held the interpreter lock all along: here the loop ran.
    assert max(gaps) < 0.1, max(gaps)


async def test_a_regular_expression_in_a_lists_condition_is_held_to_its_time_too() -> None:
    with pytest.raises(StepFailed, match=r"^Its condition \(item 0\) failed: its regular expression"):
        await Evaluator(timeout_ms=100).kept(
            "$contains(item, /^(a|aa)+$/)", [CATASTROPHIC["text"]], {}, item_name="item"
        )


async def test_regular_expressions_read_as_jsonata_has_them() -> None:
    evaluator = Evaluator()
    assert await evaluator.evaluate('$replace("abc", /(b)/, "[$1]")', {}) == "a[b]c"
    assert await evaluator.evaluate('$replace("abc", /(b)/, "$12")', {}) == "ab2c"  # group 1, then a 2
    assert await evaluator.evaluate('$split("a1b2c3", /[0-9]/)', {}) == ["a", "b", "c", ""]
    assert await evaluator.evaluate('$contains("Hello", /hello/i)', {}) is True
    assert await evaluator.evaluate('$match("a-b", /(\\w)-(\\w)/).groups', {}) == ["a", "b"]


async def test_a_regular_expression_that_doesnt_compile_fails_its_step() -> None:
    with pytest.raises(StepFailed, match=r"^Its expression failed: its regular expression /a\{2,1\}/ doesn't compile"):
        await Evaluator().evaluate('$match("x", /a{2,1}/)', {})
