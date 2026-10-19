"""Instructions as templates: ``{{ expression }}`` parts, in JSONata, filled
in before each model call from what the chat sent.

An agent's instruction reads two things:

- ``request``: the run request this turn came with: ``appName``, ``userId``,
  ``sessionId``, ``newMessage`` and ``streaming``. The executor keeps it as
  the session state ``temp:request``, which lasts for the turn.
- ``state``: the conversation's state: what the chat sends in ``stateDelta``
  (``model``, ``thinking_level`` and what the agent declares), and what tools
  wrote. ADK's scoped keys (``app:``, ``user:``, ``temp:``) aren't in it.

Each part is written into the text: a string as it is, ``true``/``false``,
nothing for null or a path that finds nothing, anything else as JSON. A part
that fails (a JSONata error, a timeout) fails the turn, saying where.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext

from forge_common.patterns import BoundedPattern, compiled
from forge_jsonata import JException, Jsonata
from forge_jsonata.regex_engine import RegexFlags

#: The session state the executor keeps the turn's run request in.
REQUEST_KEY = "temp:request"

TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
# Keep CPU work away from the event loop and its default DNS executor.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="forge-chat-templates")


class TemplateError(ValueError):
    """A ``{{ }}`` part that couldn't be filled in, and why."""


def _plain(value: Any) -> Any:
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


def evaluate(
    expression: str,
    data: Mapping[str, Any],
    *,
    what: str = "The instruction",
    timeout_ms: int = 2000,
    depth: int = 300,
) -> Any:
    """
    :param expression: One ``{{ }}`` part's JSONata.
    :param data: What it reads.
    :param what: What's being filled in, for errors.
    :param timeout_ms: How long it may take to evaluate.
    :param depth: How deep it may recurse.
    :return: What it gives, as plain JSON (None for a path that finds nothing).
    :raises TemplateError: It's empty, or it failed.
    """
    expression = expression.strip()
    if not expression:
        raise TemplateError(f"{what} has an empty {{{{ }}}}.")
    try:
        # One instance per evaluation: an instance isn't safe to share.
        def regex_engine(pattern: str, flags: RegexFlags) -> BoundedPattern:
            return compiled(
                pattern,
                seconds=timeout_ms / 1000,
                ignore_case=flags.case_insensitive,
                multiline=flags.multiline,
            )

        value = Jsonata(expression, regex_engine, timeout=timeout_ms, stack=depth).evaluate(
            _plain(dict(data))
        )
    except JException as exc:
        raise TemplateError(f"{what} couldn't fill in {{{{ {expression} }}}}: {exc}") from exc
    except (ValueError, TypeError, ArithmeticError, RecursionError, re.error) as exc:
        raise TemplateError(f"{what} couldn't fill in {{{{ {expression} }}}}: {exc}") from exc
    return _plain(value)


def render(
    template: str,
    data: Mapping[str, Any],
    *,
    what: str = "The instruction",
    timeout_ms: int = 2000,
    depth: int = 300,
) -> str:
    """
    :param template: Text with ``{{ expression }}`` parts.
    :param data: What the expressions read.
    :param what: What's being filled in, for errors.
    :param timeout_ms: How long one part may take to evaluate.
    :param depth: How deep one part may recurse.
    :return: The text with each part replaced by what it gives.
    :raises TemplateError: A part failed.
    """
    if "{{" not in template:
        return template
    plain = _plain(dict(data))

    def fill(match: re.Match[str]) -> str:
        return as_text(evaluate(match.group(1), plain, what=what, timeout_ms=timeout_ms, depth=depth))

    return TEMPLATE.sub(fill, template)


async def evaluate_async(expression: str, data: Mapping[str, Any], **options: Any) -> Any:
    """Evaluate a tool template off the event loop, with bounded regular expressions."""
    call = functools.partial(evaluate, expression, data, **options)
    return await asyncio.get_running_loop().run_in_executor(_pool, contextvars.copy_context().run, call)


def template_data(state: Mapping[str, Any]) -> dict[str, Any]:
    """:return: What a template reads, from the session's state."""
    request = state.get(REQUEST_KEY)
    return {
        "request": request if isinstance(request, dict) else {},
        "state": {key: value for key, value in state.items() if ":" not in key},
    }


def instruction_provider(template: str, *, what: str) -> Callable[[ReadonlyContext], Awaitable[str]]:
    """
    :param template: An agent's instruction.
    :param what: Which agent's it is, for errors.
    :return: An ADK instruction provider that fills the template in before
        each model call. (ADK leaves a provider's ``{name}`` text alone, so
        braces in the instruction are kept as they are.)
    """

    async def provide(context: ReadonlyContext) -> str:
        if "{{" not in template:
            return template
        call = functools.partial(render, template, template_data(dict(context.state)), what=what)
        return await asyncio.get_running_loop().run_in_executor(_pool, contextvars.copy_context().run, call)

    return provide
