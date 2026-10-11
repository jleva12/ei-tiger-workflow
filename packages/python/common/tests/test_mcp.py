"""MCP tools' results checked against their output schemas on the bounded
engine (forge_common.mcp), against a fake MCP server (the SDK's low-level one,
in-process): a runaway pattern answered in time as the tool's error, the rest
of the process going on meanwhile; a result that doesn't fit, is missing or
can't be checked answered as the tool's error too; one that fits left as it is.

A regression to the client's own check, on Python's re, would hang the runaway
test rather than fail it, so it's held to a timeout (pytest-timeout's signal,
which re answers)."""

import asyncio
import threading
import time
from typing import Any

import pytest
from mcp import types
from mcp.client import Client
from mcp.client.session import ClientSession
from mcp.server import Server

from forge_common.mcp import _validate_tool_result, bound_tool_results

RUNAWAY = "^(a|aa)+$"
LONG = "a" * 60 + "b"


@pytest.fixture(autouse=True)
def bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # The swap is the process's for good: each test's is undone after it.
    monkeypatch.setattr(ClientSession, "validate_tool_result", ClientSession.validate_tool_result)
    bound_tool_results()


def fake_server(output_schema: dict[str, Any] | None, answer: types.CallToolResult) -> Server:
    """A server with one tool, lookup, that lists ``output_schema`` as its
    output schema and answers ``answer`` whatever it's called with."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        tool = types.Tool(
            name="lookup", input_schema={"type": "object"}, output_schema=output_schema
        )
        return types.ListToolsResult(tools=[tool])

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        return answer.model_copy(deep=True)

    return Server("Fake", on_list_tools=list_tools, on_call_tool=call_tool)


def structured(content: Any) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="as JSON")], structured_content=content
    )


def refused(why: str) -> list[types.TextContent]:
    text = f"The result of the MCP tool 'lookup' was refused: {why}"
    return [types.TextContent(type="text", text=text)]


async def called(server: Server) -> types.CallToolResult:
    async with Client(server) as client:
        return await client.call_tool("lookup", {})


def longest_gap(times: list[float]) -> float:
    return max(b - a for a, b in zip(times, times[1:], strict=False))


@pytest.mark.timeout(10)
async def test_a_runaway_pattern_is_answered_in_time_while_the_process_goes_on() -> None:
    server = fake_server(
        {"type": "object", "properties": {"code": {"type": "string", "pattern": RUNAWAY}}},
        structured({"code": LONG}),
    )
    ticks: list[float] = []  # another thread's
    beats: list[float] = []  # the event loop's
    stop = threading.Event()

    def ticking() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            time.sleep(0.002)

    async def beating() -> None:
        while not stop.is_set():
            beats.append(time.perf_counter())
            await asyncio.sleep(0.002)

    async with Client(server) as client:
        await client.list_tools()  # as an ADK toolset does before its tools are called
        thread = threading.Thread(target=ticking)
        thread.start()
        heart = asyncio.create_task(beating())
        try:
            began = time.perf_counter()
            result = await client.call_tool("lookup", {})
            took = time.perf_counter() - began
        finally:
            stop.set()
            thread.join()
            await heart

    assert took < 1.0
    assert result.is_error and result.structured_content is None
    assert result.content == refused(
        "it doesn't fit its output schema: its regular expression /^(a|aa)+$/ took longer "
        "than the 500 ms a value's regular expressions get (at $.code)"
    )
    assert len(ticks) > 20 and longest_gap(ticks) < 0.1
    assert len(beats) > 20 and longest_gap(beats) < 0.2


async def test_a_result_that_fits_its_output_schema_is_left_as_it_is() -> None:
    answer = structured({"code": "aaaa", "count": 2})
    schema = {
        "type": "object",
        "properties": {"code": {"pattern": RUNAWAY}, "count": {"type": "integer"}},
        "required": ["code"],
    }

    def kept(result: types.CallToolResult) -> tuple[Any, ...]:
        return result.is_error, result.content, result.structured_content

    assert kept(await called(fake_server(schema, answer))) == kept(answer)
    # Nor is a tool's result checked when it has no output schema.
    unchecked = structured({"code": LONG})
    assert kept(await called(fake_server(None, unchecked))) == kept(unchecked)


@pytest.mark.parametrize(
    ("schema", "answer", "why"),
    [
        (
            {"type": "object", "properties": {"count": {"type": "integer"}}},
            structured({"count": "three"}),
            "it doesn't fit its output schema: 'three' is not of type 'integer' (at $.count)",
        ),
        (
            {"type": "object", "required": ["code"]},
            structured({}),
            "it doesn't fit its output schema: 'code' is a required property",
        ),
        (
            {"type": "object"},
            types.CallToolResult(content=[types.TextContent(type="text", text="no JSON")]),
            "it has no structured content, which its output schema calls for",
        ),
        (
            {"type": "object", "properties": {"code": {"maxLength": "5"}}},
            structured({"code": "x"}),
            "it can't be checked against its output schema: TypeError: '>' not supported "
            "between instances of 'int' and 'str'",
        ),
    ],
)
async def test_a_result_that_doesnt_fit_is_answered_as_the_tools_error(
    schema: dict[str, Any], answer: types.CallToolResult, why: str
) -> None:
    result = await called(fake_server(schema, answer))

    assert result.is_error and result.structured_content is None
    assert result.content == refused(why)


def test_the_clients_own_check_is_swapped_once_and_only_where_there_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_tool_results()
    assert ClientSession.validate_tool_result is _validate_tool_result

    monkeypatch.delattr(ClientSession, "validate_tool_result")
    with pytest.raises(RuntimeError, match="needs mcp 2.3 or later"):
        bound_tool_results()
