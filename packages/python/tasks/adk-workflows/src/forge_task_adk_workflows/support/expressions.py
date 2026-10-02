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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from forge_jsonata import JException, Jsonata
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.references import normalize_references

TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


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
    conditions, each failing its step with the reason.

    :ivar timeout_ms: How long one evaluation may take, in milliseconds.
    :ivar depth: How deep one evaluation may go.
    """

    timeout_ms: int = 2000
    depth: int = 300

    def evaluate(self, expression: str, data: dict[str, Any], *, what: str = "Its expression") -> Any:
        """
        :return: What the expression gives on ``data``; None for an empty one.
        :raises StepFailed: It failed, saying ``what`` failed and why.
        """
        text = expression.strip()
        if not text:
            return None
        try:
            # One instance per evaluation: an instance isn't safe to share.
            compiled = Jsonata(normalize_references(text), timeout=self.timeout_ms, stack=self.depth)
            return plain(compiled.evaluate(data))
        except JException as exc:
            raise StepFailed(f"{what} failed: {_reason(exc)}", details={"expression": text}) from exc
        except (ValueError, TypeError, ArithmeticError, RecursionError) as exc:
            raise StepFailed(f"{what} failed: {exc}", details={"expression": text}) from exc

    def render(self, template: str, data: dict[str, Any], *, what: str = "Its text") -> str:
        """:return: The text with each ``{{ expression }}`` part replaced by what it gives, as text."""

        def fill(match: re.Match[str]) -> str:
            return as_text(self.evaluate(match.group(1), data, what=what))

        return TEMPLATE.sub(fill, template)

    def truthy(self, expression: str, data: dict[str, Any], *, what: str = "Its condition") -> bool:
        """:return: Whether the condition gives true (anything else is false)."""
        return self.evaluate(expression, data, what=what) is True


def _reason(exc: JException) -> str:
    """The engine's message, with its error code when the message doesn't have it."""
    code = getattr(exc, "error", None)
    text = str(exc)
    return f"{text} ({code})" if code and code not in text else text
