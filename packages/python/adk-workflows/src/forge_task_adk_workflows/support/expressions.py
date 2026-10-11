"""JSONata, the one expression language of a step's settings, evaluated
with Forge's own Python engine (``forge_jsonata``).

Every expression is evaluated against what the node can read
(``graph.data``), so ``{{ steps.classify.output.kind }} = "bug"`` reads the
classify step's output. Bare paths also work. In expressions, braces group the
typed value; they never substitute a value into executable expression text.
Text settings hold ``{{ expression }}`` parts, each written into the text:
strings as they are, anything else as JSON, nothing for null.

An expression that fails (a type error, a timeout) fails its step with the
reason; one that finds nothing gives ``None``.

Evaluating is CPU work, so it never runs on the worker's event loop, which
renews runs' leases, stops their jobs and runs other runs meanwhile: every
evaluation runs on a thread of a pool of its own (``THREADS``, apart from the
loop's default executor, whose DNS lookups it mustn't hold up), bounded by its
timeout. A condition read for each item of a list (a filter's) is read in
batches of at most ``BATCH_SECONDS`` each, the loop free between them (where
the step can be cancelled), and all of it within ``list_timeout_ms``.

A regular expression (``/.../``) runs on the ``regex`` engine, not Python's
``re`` (``support.patterns``, on ``forge_common.patterns``): it lets go of the
interpreter lock while it matches, so the event loop and the watchdog run
meanwhile, and stops, failing its step, once it has taken the evaluation's
timeout. One that doesn't compile, or would hold the lock too long compiling,
fails its step too.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from forge_jsonata import JException, Jsonata
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.patterns import regex_engine
from forge_task_adk_workflows.support.references import normalize_references

TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: Threads evaluating expressions at once, for every run of the process.
THREADS = 4
#: The most seconds one batch of a list's items holds its thread (one item's
#: evaluation can go on to its own timeout past it).
BATCH_SECONDS = 0.05

_pool = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="forge-expressions")
#: What an evaluation's failure can be, besides JSONata's own errors: a value
#: of the wrong kind, a regular expression that doesn't compile (``re.error``,
#: ``forge_common.patterns.InvalidPattern``) or takes too long
#: (``forge_common.patterns.PatternTimeout``).
_FAILURES = (ValueError, TypeError, ArithmeticError, RecursionError, re.error)


def plain(value: Any) -> Any:
    """:return: ``value`` as plain JSON values: anything JSON can't hold as its text."""
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def as_text(value: Any) -> str:
    """:return: ``value`` as text: nothing for None, a string as it is, true or false, else JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Evaluator:
    """
    Evaluates expressions, renders text with ``{{ }}`` parts and checks
    conditions, each failing its step with the reason, off the event loop.

    :ivar timeout_ms: How long one evaluation may take, in milliseconds.
    :ivar depth: How deep one evaluation may go.
    :ivar list_timeout_ms: How long a condition read for each item of a list
        may take in all (one item's evaluation can go on to its own timeout
        past it).
    """

    timeout_ms: int = 2000
    depth: int = 300
    list_timeout_ms: int = 30_000

    async def evaluate(self, expression: str, data: dict[str, Any], *, what: str = "Its expression") -> Any:
        """
        :return: What the expression gives on ``data``; None for an empty one.
        :raises StepFailed: It failed, saying ``what`` failed and why.
        """
        if not expression.strip():
            return None
        return await _off_loop(self._evaluate, expression, data, what)

    async def render(self, template: str, data: dict[str, Any], *, what: str = "Its text") -> str:
        """:return: The text with each ``{{ expression }}`` part replaced by what it gives, as text."""
        if not TEMPLATE.search(template):
            return template
        return await _off_loop(self._render, template, data, what)

    async def truthy(self, expression: str, data: dict[str, Any], *, what: str = "Its condition") -> bool:
        """:return: Whether the condition gives true (anything else is false)."""
        return await self.evaluate(expression, data, what=what) is True

    async def kept(
        self, condition: str, items: list[Any], data: dict[str, Any], *, item_name: str, what: str = "Its condition"
    ) -> list[Any]:
        """
        :param items: The list, as plain JSON (what an expression gave).
        :param data: What the condition reads besides, as plain JSON
            (``graph.data``): each item is read under ``item_name``, with its
            ``index``.
        :return: The items the condition is true for, in order.
        :raises StepFailed: The condition failed on an item (``what (item
            <index>)``), or reading it took longer than ``list_timeout_ms``.
        """
        text = condition.strip()
        if not text or not items:
            return []
        deadline = time.monotonic() + self.list_timeout_ms / 1000
        # Made plain JSON once, here: each item's evaluation then skips
        # JSONata's own check of all it reads, which made a list cost its
        # length squared.
        checked = await _off_loop(plain, {"data": data, "items": items})
        data, items = checked["data"], checked["items"]
        kept: list[Any] = []
        start = 0
        while start < len(items):
            start, found = await _off_loop(self._batch, text, items, data, item_name, start, deadline, what)
            kept.extend(found)
        return kept

    # ------------------------------------------------------------------ on a thread of the pool

    def _evaluate(self, expression: str, data: dict[str, Any], what: str) -> Any:
        text = expression.strip()
        if not text:
            return None
        try:
            # One instance per evaluation: an instance isn't safe to share.
            compiled = self._compiled(text)
            return plain(compiled.evaluate(data))
        except JException as exc:
            raise StepFailed(f"{what} failed: {_reason(exc)}", details={"expression": text}) from exc
        except _FAILURES as exc:
            raise StepFailed(f"{what} failed: {exc}", details={"expression": text}) from exc

    def _compiled(self, text: str) -> Jsonata:
        """:return: The expression, compiled, its regular expressions on the bounded engine."""
        engine = regex_engine(self.timeout_ms / 1000)
        return Jsonata(normalize_references(text), engine, self.timeout_ms, self.depth)

    def _render(self, template: str, data: dict[str, Any], what: str) -> str:
        def fill(match: re.Match[str]) -> str:
            return as_text(self._evaluate(match.group(1), data, what))

        return TEMPLATE.sub(fill, template)

    def _batch(
        self,
        text: str,
        items: list[Any],
        data: dict[str, Any],
        item_name: str,
        start: int,
        deadline: float,
        what: str,
    ) -> tuple[int, list[Any]]:
        """
        The items from ``start`` on that the condition is true for, until
        BATCH_SECONDS have gone.

        :return: Where the next batch starts, and the items kept.
        """
        ends = min(time.monotonic() + BATCH_SECONDS, deadline)
        try:
            compiled = self._compiled(text)
        except JException as exc:
            raise StepFailed(f"{what} failed: {_reason(exc)}", details={"expression": text}) from exc
        except _FAILURES as exc:
            raise StepFailed(f"{what} failed: {exc}", details={"expression": text}) from exc
        compiled.set_validate_input(False)  # plain JSON (kept)
        found: list[Any] = []
        index = start
        while index < len(items):
            if time.monotonic() >= deadline:
                seconds = self.list_timeout_ms / 1000
                raise StepFailed(
                    f"{what} took longer than {seconds:g} seconds over the list's {len(items)} items "
                    f"(it got to item {index})",
                    details={"expression": text},
                )
            item = items[index]
            try:
                holds = compiled.evaluate({**data, item_name: item, "index": index}) is True
            except JException as exc:
                raise StepFailed(f"{what} (item {index}) failed: {_reason(exc)}", details={"expression": text}) from exc
            except _FAILURES as exc:
                raise StepFailed(f"{what} (item {index}) failed: {exc}", details={"expression": text}) from exc
            if holds:
                found.append(item)
            index += 1
            if time.monotonic() >= ends:
                break
        return index, found


async def _off_loop[T](call: Callable[..., T], *args: Any) -> T:
    """:return: What ``call`` gives, run on a thread of the pool (with the caller's context vars)."""
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(_pool, functools.partial(context.run, call, *args))


def _reason(exc: JException) -> str:
    """The engine's message, with its error code when the message doesn't have it."""
    code = getattr(exc, "error", None)
    text = str(exc)
    return f"{text} ({code})" if code and code not in text else text
