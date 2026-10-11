import json

import httpx
import pytest

from forge_agent_runtime.build import BuildError, build_tools
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.http_templates import TemplateProblem, upgraded_http_tool
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools, HttpTool, MissingSetting
from forge_common.adk import ToolFailure

HOSTS = ("api.example.com",)


class State:
    def __init__(self, values):
        self.values = values

    def to_dict(self):
        return dict(self.values)


class Context:
    def __init__(self, **state):
        self.state = State(state)


def recording(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def http_tool(seen, *, url, method="POST", body="", parameters=None, **services):
    services.setdefault("allowed_hosts", HOSTS)
    return HttpTool(
        name="create_order",
        description="Creates an order.",
        method=method,
        url=url,
        body=body,
        headers={},
        parameters=parameters or {},
        confirm=False,
        services=RuntimeServices(**services),
        http=recording(seen),
    )


async def test_a_whole_value_keeps_its_type_and_one_in_text_is_written_in():
    seen: list[httpx.Request] = []
    body = """{
      "order": {"id": {order_id}, "items": "{items}", "note": "Deliver to {name}, please"},
      "tags": ["{tag}", "fixed"],
      "token": "${ORDERS_TOKEN}"
    }"""
    made = http_tool(
        seen, url="https://api.example.com/orders", body=body, environment={"ORDERS_TOKEN": 'a"b'}
    )
    await made.run_async(
        args={"order_id": 7, "items": [{"sku": "A"}], "name": "Sam", "tag": "rush"},
        tool_context=None,
    )
    assert json.loads(seen[0].content) == {
        "order": {"id": 7, "items": [{"sku": "A"}], "note": "Deliver to Sam, please"},
        "tags": ["rush", "fixed"],
        "token": 'a"b',
    }

    # What the model leaves out: a whole value's key and list item go, text loses it.
    await made.run_async(args={"order_id": 8}, tool_context=None)
    assert json.loads(seen[1].content) == {
        "order": {"id": 8, "note": "Deliver to , please"},
        "tags": ["fixed"],
        "token": 'a"b',
    }


async def test_the_model_is_offered_only_the_arguments_the_url_and_body_name():
    made = http_tool(
        [],
        url="https://api.example.com/stores/{store}/orders?status={status}",
        body='{"note": {note}, "total": {total}}',
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "shipped"]},
                "note": {"type": "string", "description": "For the courier."},
                "unused": {"type": "string"},
            },
            "required": ["unused"],
        },
    )
    declared = made._get_declaration().parameters_json_schema
    assert declared == {
        "type": "object",
        "properties": {
            "store": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "shipped"]},
            "note": {"type": "string", "description": "For the courier."},
            "total": {"type": "string"},
        },
        # The path's, and those not declared; the query's and the declared note are optional.
        "required": ["store", "total"],
    }

    # Undeclared, a query pair's own argument is optional; the path's is required.
    paging = http_tool([], method="GET", url="https://api.example.com/{store}/orders?page={page}")
    assert paging._get_declaration().parameters_json_schema["required"] == ["store"]

    # A GET sends no body: what only its body names isn't offered.
    getting = http_tool([], method="GET", url="https://api.example.com/orders", body='{"note": {note}}')
    assert getting._get_declaration().parameters_json_schema == {"type": "object", "properties": {}}


async def test_run_values_come_from_the_chat_or_the_runs_provider():
    seen: list[httpx.Request] = []
    url = "https://api.example.com/{{ state.region }}/orders?user={{ request.userId }}"
    body = '{"customer": {{ state.customer }}, "summary": "for {{ state.customer.name }}"}'
    made = http_tool(seen, url=url, body=body)
    context = Context(region="eu", customer={"name": "Ada"}, **{"temp:request": {"userId": "u1"}})
    await made.run_async(args={}, tool_context=context)
    assert str(seen[0].url) == "https://api.example.com/eu/orders?user=u1"
    assert json.loads(seen[0].content) == {"customer": {"name": "Ada"}, "summary": "for Ada"}

    asked: list[str] = []

    async def provider(expression, context, *, what):
        asked.append(expression)
        return {"input.region": "us", "input.customer": {"id": 3}}.get(expression)

    from_run = http_tool(
        seen,
        url="https://api.example.com/{{ input.region }}/orders",
        body='{"customer": "{{ input.customer }}"}',
        tool_values=provider,
    )
    await from_run.run_async(args={}, tool_context=None)
    assert str(seen[1].url) == "https://api.example.com/us/orders"
    assert json.loads(seen[1].content) == {"customer": {"id": 3}}
    assert asked == ["input.region", "input.customer"]

    broken = http_tool(seen, url="https://api.example.com/{{ $nope( }}")
    with pytest.raises(ToolFailure, match="couldn't fill in"):
        await broken.run_async(args={}, tool_context=Context())


async def test_an_argument_cant_change_where_the_request_goes():
    seen: list[httpx.Request] = []
    made = http_tool(seen, method="GET", url="https://api.example.com/orders/{id}?q={q}")
    await made.run_async(args={"id": "../admin", "q": "a&admin=1"}, tool_context=None)
    assert str(seen[0].url) == "https://api.example.com/orders/..%2Fadmin?q=a%26admin%3D1"


def test_a_body_that_isnt_json_or_a_missing_setting_fails_to_build():
    with pytest.raises(TemplateProblem, match="isn't JSON"):
        http_tool([], url="https://api.example.com/", body='{"note": note}')
    with pytest.raises(TemplateProblem, match="empty"):
        http_tool([], url="https://api.example.com/{{ }}")
    with pytest.raises(MissingSetting, match="ORDERS_TOKEN"):
        http_tool([], url="https://api.example.com/", body='{"t": "${ORDERS_TOKEN}"}', environment={})


async def test_build_tools_says_why_a_body_cant_be_built(models):
    with pytest.raises(BuildError, match="isn't JSON"):
        await build_tools(
            "Create order",
            "http_tool",
            {"method": "POST", "url": "https://api.example.com/", "body": "{nope"},
            models=models,
            services=RuntimeServices(allowed_hosts=HOSTS),
        )


def test_an_http_tool_from_before_its_body_sends_what_it_sent():
    parameters = {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "expand": {"type": "string"},
            "note": {"type": "string"},
        },
    }
    getting = {"method": "GET", "url": "https://api.example.com/orders/{order_id}", "parameters": parameters}
    assert upgraded_http_tool(getting) == {
        **getting,
        "url": "https://api.example.com/orders/{order_id}?expand={expand}&note={note}",
        "body": "",
    }
    with_query = {**getting, "url": "https://api.example.com/orders/{order_id}?v=2"}
    assert upgraded_http_tool(with_query)["url"].endswith("?v=2&expand={expand}&note={note}")

    posting = {**getting, "method": "POST"}
    upgraded = upgraded_http_tool(posting)
    assert upgraded["url"] == getting["url"]
    assert upgraded["body"] == '{\n  "expand": {expand},\n  "note": {note}\n}'

    # One that has a body is as it is.
    current = {**posting, "body": ""}
    assert upgraded_http_tool(current) == current


async def test_an_older_documents_http_tool_is_upgraded_as_its_read(models):
    seen: list[httpx.Request] = []
    doc = parse_document(
        {
            "format": "forge.chat_agent/v1",
            "id": "ca_orders",
            "name": "Orders",
            "nodes": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "name": "Orders",
                    "config": {"instruction": "Help."},
                    "outputs": ["tools", "agents"],
                },
                {
                    "id": "find",
                    "kind": "http_tool",
                    "name": "Find order",
                    "config": {
                        "method": "GET",
                        "url": "https://api.example.com/orders/{order_id}",
                        "parameters": {
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}, "expand": {"type": "string"}},
                        },
                    },
                    "outputs": [],
                },
            ],
            "edges": [{"id": "e", "source": "agent", "source_output": "tools", "target": "find"}],
        }
    )
    config = doc.nodes[1].config
    assert (
        config["url"] == "https://api.example.com/orders/{order_id}?expand={expand}" and config["body"] == ""
    )

    [guarded] = await build_tools(
        "Find order",
        "http_tool",
        {
            "method": "GET",
            "url": "https://api.example.com/orders/{order_id}",
            "parameters": {"type": "object", "properties": {"expand": {"type": "string"}}},
        },
        models=models,
        services=RuntimeServices(allowed_hosts=HOSTS),
        http=recording(seen),
    )
    assert isinstance(guarded, GuardedTools)
    [made] = await guarded.get_tools()
    await made.run_async(args={"order_id": "1", "expand": "items"}, tool_context=None)
    assert str(seen[0].url) == "https://api.example.com/orders/1?expand=items"
