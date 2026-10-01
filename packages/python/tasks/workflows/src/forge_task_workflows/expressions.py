"""JSONata, the one expression language of a workflow's settings, evaluated
with Forge's own Python engine (``forge_jsonata``).

Every expression is evaluated against one object, what the step can read:

    {"input": ..., "steps": {"<id>": {"output": ..., "error": ...}},
     "previous": ..., "<a loop's item name>": ..., "index": ...}

so ``{{ steps.classify.output.kind }} = "bug"`` reads the classify step's
output. Bare paths also work. In expressions, braces group the typed value;
they never substitute a value into executable expression text.
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
from forge_task_workflows.errors import StepFailed
from forge_task_workflows.references import normalize_references

TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def plain(value: Any) -> Any:
    """
    Parses a Python object, serializes it into a JSON string, and deserializes it
    back into a Python object to ensure all object data is JSON-compatible.

    This function is particularly useful for deep copying Python objects into a
    JSON-compatible format or for preparing data for systems that require strict
    JSON compliance, such as APIs. Special care is taken to ensure non-JSON
    data types are converted to strings where necessary, avoiding serialization
    errors.

    :param value: A Python object to be processed. The input can be of any type,
        but only JSON-compatible data will remain unchanged after the method
        execution.
    :return: A JSON-compatible Python object. JSON-incompatible values are
        converted into string representations.
    """
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def as_text(value: Any) -> str:
    """
    Converts a given value to its string representation. This utility is particularly useful for
    handling various data types such as `None`, `str`, `bool`, or other serializable objects.

    :param value: The input value to be converted to a string. This can be of any type.
    :return: A string representation of the input value. If the value is `None`, it returns an
        empty string. If the value is a string, it is returned as-is. If the value is a boolean,
        it returns "true" for `True` and "false" for `False`. For other data types, the method
        attempts to serialize them to a JSON string.
    :rtype: str
    """
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
    Represents an evaluator for processing and evaluating expressions, rendering templates,
    and determining truthiness conditions. It provides functionalities to evaluate JSONata
    expressions, render text templates with embedded expressions, and assess the truthiness
    of conditional expressions.

    The primary purpose of the class is to simplify working with structured data and textual
    templates through evaluative mechanisms, ensuring consistent processing and error handling.

    :ivar timeout_ms: The maximum execution time for evaluating an expression, in milliseconds.
    :type timeout_ms: int
    :ivar depth: The maximum stack depth allowed during expression evaluation.
    :type depth: int
    """

    timeout_ms: int = 2000
    depth: int = 300

    def evaluate(self, expression: str, data: dict[str, Any], *, what: str = "Its expression") -> Any:
        """
        Evaluates a given JSONata expression against a provided dataset, returning the result.

        The method processes the provided expression by stripping any extraneous whitespace
        and then attempts to evaluate it against the given data context. A newly compiled
        instance of the JSONata engine is created for each evaluation to ensure thread safety.
        If any error occurs during the evaluation, the method raises a `StepFailed`
        exception with a detailed error message and context information.

        :param expression: JSONata expression to evaluate.
        :type expression: str
        :param data: Dataset to evaluate the JSONata expression against.
        :type data: dict[str, Any]
        :param what: Description of the expression or operation, used in error messages.
                     Defaults to "Its expression".
        :type what: str
        :return: The result of the evaluated expression.
        :rtype: Any
        :raises StepFailed: If the evaluation fails due to any exception or error during processing.
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
        """
        Render a template string by replacing placeholders with evaluated data.

        This method processes a template string, identifies placeholders, and substitutes
        them with values derived from the provided data. Each placeholder in the template
        is processed using the `evaluate` method, allowing dynamic content generation.

        :param template: The template string containing placeholders to be replaced.
        :param data: A dictionary containing the data used to evaluate and fill the placeholders.
        :param what: Optional string used as context or additional information during evaluation.
                     Defaults to "Its text".
        :return: A resulting string with placeholders in the template replaced by evaluated values.
        :rtype: str
        """

        def fill(match: re.Match[str]) -> str:
            return as_text(self.evaluate(match.group(1), data, what=what))

        return TEMPLATE.sub(fill, template)

    def truthy(self, expression: str, data: dict[str, Any], *, what: str = "Its condition") -> bool:
        """
        Evaluate the truthiness of a given expression in the provided context and return
        whether the result of the evaluation is `True`. This method uses the `evaluate`
        logic to determine the final outcome.

        :param expression: A string that represents the expression to be evaluated.
        :param data: A dictionary representing the context in which the expression
            will be evaluated.
        :param what: An optional string describing the purpose or context of the
            evaluation. Defaults to "Its condition".
        :return: Boolean value indicating whether the evaluated expression is
            truthy (i.e., equals `True`).
        """
        return self.evaluate(expression, data, what=what) is True


def _reason(exc: JException) -> str:
    """
    Formats the error message from a given Java exception. This function extracts
    the error code, if available, and appends it to the exception's string
    representation, ensuring the resulting message contains both the textual
    description and the error code when applicable.

    :param exc: The Java exception (`JException`) to extract and format the
        error message from.
    :return: A formatted string representation of the exception that includes
        the error details and code, if present.
    :rtype: str
    """
    code = getattr(exc, "error", None)
    text = str(exc)
    return f"{text} ({code})" if code and code not in text else text


def compile_errors(expression: str) -> str | None:
    """
    Validates the syntax of a given Jsonata expression and returns details about any
    compilation errors. If the expression is valid, no errors will be returned.

    :param expression: A Jsonata expression to validate.
    :type expression: str
    :return: A detailed error message if the expression fails to compile, or None if
        the expression is valid.
    :rtype: str | None
    """
    text = expression.strip()
    if not text:
        return None
    try:
        Jsonata(normalize_references(text))
    except JException as exc:
        return _reason(exc)
    except ValueError as exc:
        return str(exc)
    return None
