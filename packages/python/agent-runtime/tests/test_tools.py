import json

import httpx
import pytest

from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools, HttpTool, check_url
from forge_common.adk import ToolFailure


def tool(handler, *, method="GET", url="https://api.example.com/orders/{order_id}", **services):
    services.setdefault("allowed_hosts", ("api.example.com",))
    return HttpTool(
        name="look_up_order",
        description="Finds an order.",
        method=method,
        url=url,
        headers={"Accept": "application/json"},
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
        confirm=False,
        services=RuntimeServices(**services),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_arguments_fill_the_url_then_the_query_or_the_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "shipped"})

    got = await tool(handler).run_async(args={"order_id": "A/1", "expand": "items"}, tool_context=None)
    assert got == {"status": 200, "body": {"status": "shipped"}}
    assert str(seen[0].url) == "https://api.example.com/orders/A%2F1?expand=items"
    assert seen[0].headers["accept"] == "application/json"

    await tool(handler, method="POST").run_async(args={"order_id": "7", "note": "hi"}, tool_context=None)
    assert json.loads(seen[1].content) == {"note": "hi"}

    with pytest.raises(ToolFailure, match="order_id"):
        await tool(handler).run_async(args={}, tool_context=None)


async def test_tools_cant_reach_the_runtimes_own_network():
    services = RuntimeServices()
    for url in (
        "http://127.0.0.1/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "ftp://x",
    ):
        with pytest.raises(ToolFailure):
            await check_url(url, services)
    assert await check_url("http://127.0.0.1/", RuntimeServices(allow_private=True))
    assert await check_url("http://internal.svc/", RuntimeServices(allowed_hosts=("internal.svc",)))

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(ToolFailure, match="private network"):
        await tool(redirect).run_async(args={"order_id": "1"}, tool_context=None)


async def test_a_big_answer_is_refused_and_a_guarded_tool_answers_instead_of_raising():
    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 100)

    small = tool(big, max_response_bytes=10)
    with pytest.raises(ToolFailure, match="over 10 bytes"):
        await small.run_async(args={"order_id": "1"}, tool_context=None)

    [guarded] = await GuardedTools([small]).get_tools()
    answer = await guarded.run_async(args={"order_id": "1"}, tool_context=None)
    assert answer["status"] == "failed" and "over 10 bytes" in answer["reason"]

    [confirmed] = await GuardedTools([small], confirm=True).get_tools()
    assert await confirmed.check_require_confirmation({}, None) is True
