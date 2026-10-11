"""
MCP tool results checked against their tool's output schema on the bounded
engine (:func:`forge_common.patterns.schema_errors`), rather than with the
``mcp`` client's own check.

``call_tool`` checks a result that isn't an error (``structuredContent``)
against the output schema the server lists for its tool (``outputSchema``),
in ``ClientSession.validate_tool_result``, with stock jsonschema: its
``pattern`` and ``patternProperties`` match with Python's ``re``, in C,
holding the interpreter lock for as long as a pattern backtracks. A server
whose schema says ``^(a|aa)+$`` and whose result is sixty ``a``s and a ``b``
freezes the whole process, every other run and request with it.
:func:`bound_tool_results` swaps that check, for every session of the process
(ADK's ``McpToolset`` makes its own), for :func:`schema_errors` on a worker
thread: all of a result's patterns take ``SCHEMA_SECONDS`` at most, off the
lock, and the event loop goes on meanwhile.

A result that doesn't fit, can't be checked in time, or is missing answers
the call as the tool's error (``isError``), saying why, where the client
raises: as a server answers a call that failed, so the model reads it and the
run goes on.

Needs ``forge-common[mcp]``.
"""

from __future__ import annotations

import logging

import anyio.to_thread
from jsonschema import ValidationError
from jsonschema.exceptions import best_match
from mcp import types
from mcp.client.session import ClientSession

from forge_common.patterns import schema_errors

__all__ = ["bound_tool_results"]

logger = logging.getLogger(__name__)


def bound_tool_results() -> None:
    """
    Have every MCP client session of the process check its tools' results
    with :func:`schema_errors`: ``ClientSession.validate_tool_result``, the
    check ``call_tool`` runs, swapped for one that does. Calling it again
    does nothing.

    :raises RuntimeError: The installed ``mcp`` has no such check to swap.
    """
    current = getattr(ClientSession, "validate_tool_result", None)
    if current is _validate_tool_result:
        return
    if current is None:
        raise RuntimeError(
            "mcp's ClientSession has no validate_tool_result to check tool results "
            "on the bounded engine instead: forge-common[mcp] needs mcp 2.3 or later"
        )
    ClientSession.validate_tool_result = _validate_tool_result  # type: ignore[method-assign]


async def _validate_tool_result(
    self: ClientSession, name: str, result: types.CallToolResult
) -> None:
    """
    ``ClientSession.validate_tool_result`` on the bounded engine. As the
    client's own check does, it lists the server's tools first when the
    tool's output schema isn't known yet, and checks nothing for a tool the
    server doesn't list or that has no output schema.

    :param name: The tool, as its server lists it.
    :param result: What it answered, not an error: made the tool's error, in
        place (``call_tool`` returns it), when it doesn't fit.
    """
    if name not in self._tool_output_schemas:
        await self.list_tools()
    if name not in self._tool_output_schemas:
        logger.warning("The MCP tool %s isn't listed by its server: its result isn't checked", name)
        return
    schema = self._tool_output_schemas[name]
    if schema is None:
        return
    content = result.structured_content
    if content is None and "structured_content" not in result.model_fields_set:
        _refuse(name, result, "it has no structured content, which its output schema calls for")
        return
    try:
        # On a worker thread: its patterns let go of the interpreter lock as
        # they match, but the thread they match on waits for them.
        errors = await anyio.to_thread.run_sync(schema_errors, schema, content)
    except Exception as error:  # noqa: BLE001 - however a schema its server made up fails
        _refuse(name, result, f"it can't be checked against its output schema: {_described(error)}")
        return
    if errors:
        _refuse(name, result, f"it doesn't fit its output schema: {_reason(errors)}")


def _refuse(name: str, result: types.CallToolResult, why: str) -> None:
    # The call answered as the tool's error, as a server answers one that
    # failed: the model reads why, and none of what the result held.
    logger.warning("Refused the result of the MCP tool %s: %s", name, why)
    result.is_error = True
    text = f"The result of the MCP tool {name!r} was refused: {why}"
    result.content = [types.TextContent(type="text", text=text)]
    result.structured_content = None


def _reason(errors: list[ValidationError]) -> str:
    # The error jsonschema finds most telling, as the client names one, and where it is.
    error: ValidationError = best_match(errors) or errors[0]
    return f"{error.message} (at {error.json_path})" if error.absolute_path else error.message


def _described(error: Exception) -> str:
    # An exception as a refusal names it: its first line, which for
    # jsonschema's says what went wrong before the schema and value it dumps.
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return f"{type(error).__name__}: {lines[0].rstrip(':')}" if lines else type(error).__name__
