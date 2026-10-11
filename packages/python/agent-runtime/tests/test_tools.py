import json

import httpx
import pytest

from forge_agent_runtime.executor import AgentExecutor, DirectoryAgentSource
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools, HttpTool, check_url, http_client
from forge_common.adk import ToolFailure


def tool(
    handler, *, method="GET", url="https://api.example.com/orders/{order_id}", body="", http=None, **services
):
    services.setdefault("allowed_hosts", ("api.example.com",))
    return HttpTool(
        name="look_up_order",
        description="Finds an order.",
        method=method,
        url=url,
        body=body,
        headers={"Accept": "application/json"},
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
        confirm=False,
        services=RuntimeServices(**services),
        http=http or httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def setting_a_cookie(sent: list[str | None]):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie"))
        return httpx.Response(200, json={}, headers={"Set-Cookie": "session=first-callers; Path=/; Secure"})

    return handler


async def test_the_shared_http_client_sends_no_cookie_an_earlier_response_set():
    sent: list[str | None] = []
    handler = setting_a_cookie(sent)
    client = http_client(transport=httpx.MockTransport(handler))

    await client.get("https://api.example.com/login")  # someone else's request
    await tool(handler, http=client).run_async(args={"order_id": "A1"}, tool_context=None)

    assert sent == [None, None]
    assert len(client.cookies.jar) == 0


async def test_the_hosted_runtimes_own_http_client_keeps_no_cookies(tmp_path, models):
    executor = AgentExecutor(DirectoryAgentSource(tmp_path), models=models)
    try:
        request = executor.http.build_request("GET", "https://api.example.com/login")
        executor.http.cookies.extract_cookies(
            httpx.Response(200, headers={"Set-Cookie": "session=org-a; Path=/; Secure"}, request=request)
        )
        assert "cookie" not in executor.http.build_request("GET", "https://api.example.com/orders").headers
    finally:
        await executor.close()


async def test_arguments_fill_the_url_and_the_body_and_go_nowhere_else():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "shipped"})

    url = "https://api.example.com/orders/{order_id}?expand={expand}"
    got = await tool(handler, url=url).run_async(
        args={"order_id": "A/1", "expand": "items", "other": "x"}, tool_context=None
    )
    assert got == {"status": 200, "body": {"status": "shipped"}}
    assert str(seen[0].url) == "https://api.example.com/orders/A%2F1?expand=items"
    assert seen[0].headers["accept"] == "application/json"

    # A query parameter the model leaves out isn't sent.
    await tool(handler, url=url).run_async(args={"order_id": "7"}, tool_context=None)
    assert str(seen[1].url) == "https://api.example.com/orders/7"

    posting = tool(handler, method="POST", body='{"note": {note}, "count": "{count}"}')
    await posting.run_async(args={"order_id": "7", "note": "hi", "count": 2, "other": 1}, tool_context=None)
    assert json.loads(seen[2].content) == {"note": "hi", "count": 2}

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
