"""The organization's credentials on HTTP and OpenAPI tools: each request
signed where the credential may go, refused where it may not; OpenAPI specs
fetched and called as HTTP tools' requests are (never the runtime's own
network); and a spec's operations listed by the names its tools get."""

import asyncio
import copy
import json
import time
from typing import Any, cast

import httpx
import pytest
from google.adk.tools.tool_context import ToolContext

from forge_agent_runtime.build import BuildError, build_tools
from forge_agent_runtime.openapi import (
    MAX_SPEC_VALUES,
    SpecError,
    describe_spec,
    expanded_size,
    fetch_spec,
    guarded_client,
    load_spec,
    read_spec,
    spec_toolset,
    toolset_of,
)
from forge_agent_runtime.services import RequestAuth, RuntimeServices
from forge_agent_runtime.tools import GuardedTools, HttpTool, MissingSetting, fill_references, http_client
from forge_common.adk import ToolFailure
from tests.conftest import BILLING_SPEC, EXAMPLE_HOSTS

# These credential-only requests deliberately omit the optional chat state.
NO_CONTEXT = cast(ToolContext, None)


class Credentials:
    """The organization's credentials, as the hosted runtime signs with them:
    ``cred_1`` for https://api.example.com, in org_1."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None, str]] = []

    async def __call__(self, credential_id: str, *, organization_id: str | None, url: str) -> RequestAuth:
        self.asked.append((credential_id, organization_id, url))
        if credential_id != "cred_1" or organization_id != "org_1":
            raise LookupError("The organization has no credential: it was deleted")
        if not url.startswith("https://api.example.com/"):
            raise LookupError("The credential Orders API is only sent to https://api.example.com")
        return RequestAuth(headers={"Authorization": "Bearer t0k"}, params={"key": "k1"})


def services(**more: Any) -> RuntimeServices:
    return RuntimeServices(allowed_hosts=(*EXAMPLE_HOSTS, "elsewhere.example.com"), **more)


def tool(handler, *, url="https://api.example.com/orders/{id}", credential="cred_1", **more) -> HttpTool:
    return HttpTool(
        name="look_up_order",
        description="Finds an order.",
        method="GET",
        url=url,
        headers={"Accept": "application/json"},
        parameters={"type": "object", "properties": {"id": {"type": "string"}}},
        confirm=False,
        services=services(**more),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        credential=credential,
        organization_id="org_1",
    )


async def test_an_http_tool_signs_its_request_with_its_credential():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    provider = Credentials()
    made = tool(handler, url="https://api.example.com/orders/{id}?page={page}", credentials=provider)
    got = await made.run_async(args={"id": "7", "page": 2}, tool_context=NO_CONTEXT)
    assert got == {"status": 200, "body": {"ok": True}}
    assert seen[0].headers["authorization"] == "Bearer t0k"
    assert seen[0].headers["accept"] == "application/json"
    assert dict(seen[0].url.params) == {"page": "2", "key": "k1"}
    assert provider.asked == [("cred_1", "org_1", "https://api.example.com/orders/7?page=2")]


async def test_a_url_keeps_its_own_query_beside_the_arguments_and_the_credentials():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await tool(
        handler,
        url="https://api.example.com/search?type=issue&id={id}&page={page}",
        credentials=Credentials(),
    ).run_async(args={"id": "7", "page": 2}, tool_context=NO_CONTEXT)
    assert dict(seen[0].url.params) == {"type": "issue", "id": "7", "page": "2", "key": "k1"}


async def test_a_credential_refused_for_where_the_tool_calls_fails_the_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Nothing is sent")

    with pytest.raises(ToolFailure, match="only sent to https://api.example.com"):
        await tool(handler, url="https://elsewhere.example.com/x/{id}", credentials=Credentials()).run_async(
            args={"id": "1"}, tool_context=NO_CONTEXT
        )


async def test_a_redirect_elsewhere_goes_without_the_credential():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://elsewhere.example.com/moved"})
        return httpx.Response(200, json={})

    await tool(handler, credentials=Credentials()).run_async(args={"id": "1"}, tool_context=NO_CONTEXT)
    assert seen[0].headers["authorization"] == "Bearer t0k"
    assert "authorization" not in seen[1].headers
    assert "key" not in dict(seen[1].url.params)


async def test_a_tool_naming_a_credential_needs_the_hosted_runtime(models, web):
    with pytest.raises(BuildError, match="only the hosted runtime has"):
        await build_tools(
            "Orders",
            "http_tool",
            {"url": "https://api.example.com/orders", "credential": "cred_1"},
            models=models,
            http=web(),
            services=services(),
        )
    built = await build_tools(
        "Orders",
        "http_tool",
        {"url": "https://api.example.com/orders", "credential": "cred_1"},
        models=models,
        http=web(),
        services=services(credentials=Credentials()),
        organization_id="org_1",
    )
    group = built[0]
    assert isinstance(group, GuardedTools)
    made = group.items[0]
    assert isinstance(made, HttpTool)
    assert made.credential == "cred_1"


# ----------------------------------------------------------------- OpenAPI


async def test_a_spec_lists_its_operations_by_their_tools_names():
    spec: dict[str, Any] = copy.deepcopy(BILLING_SPEC)
    spec["paths"]["/charges"]["get"]["summary"] = "Lists the charges"
    spec["paths"]["/health"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    spec["components"] = {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}}
    found = await describe_spec(spec)
    assert (found.title, found.version, found.servers) == (
        "Billing",
        "1",
        ["https://api.example.com/billing"],
    )
    assert [(o.name, o.operation_id, o.method, o.path) for o in found.operations] == [
        ("get_invoice", "getInvoice", "GET", "/invoices/{id}"),
        ("list_charges", "listCharges", "GET", "/charges"),
        ("preview_refund", "previewRefund", "POST", "/refunds"),
        # No operationId: named after its path and method, as ADK names it.
        ("health_get", "health_get", "GET", "/health"),
    ]
    assert found.operations[1].summary == "Lists the charges"
    [scheme] = found.security
    assert (scheme.type, scheme.location, scheme.parameter) == ("apiKey", "header", "X-Api-Key")


def test_a_spec_that_isnt_one_says_why():
    with pytest.raises(SpecError, match="JSON or YAML"):
        load_spec("{not json")
    with pytest.raises(SpecError, match="no paths"):
        load_spec("openapi: 3.0.0\ninfo: {title: x}\n")
    assert load_spec("openapi: 3.0.0\npaths: {}\n")["openapi"] == "3.0.0"


def alias_bomb(levels: int = 9) -> str:
    """About 500 bytes of YAML that come to 10^levels values once its aliases are followed."""
    lines = ["openapi: 3.0.0", "paths: {}", "l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, levels):
        lines.append(f"l{level}: &l{level} [{', '.join([f'*l{level - 1}'] * 10)}]")
    return "\n".join(lines) + "\n"


def multiplying(levels: int) -> dict[str, Any]:
    """A JSON spec whose schemas each refer twice to the next: 2^levels values with each $ref written out."""
    schemas: dict[str, Any] = {
        f"L{i}": {
            "type": "object",
            "properties": {name: {"$ref": f"#/components/schemas/L{i + 1}"} for name in ("a", "b")},
        }
        for i in range(levels)
    }
    schemas[f"L{levels}"] = {"type": "string"}
    spec: dict[str, Any] = copy.deepcopy(BILLING_SPEC)
    spec["components"] = {"schemas": schemas}
    get = spec["paths"]["/charges"]["get"]
    get["responses"] = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/L0"}}},
        }
    }
    return spec


def test_a_spec_with_yaml_aliases_is_refused_before_theyre_followed():
    started = time.monotonic()
    with pytest.raises(SpecError, match=r"YAML alias \(line 4\)"):
        load_spec(alias_bomb())
    assert time.monotonic() - started < 0.5
    # An anchor alone repeats nothing.
    assert load_spec("openapi: 3.0.0\npaths: &paths {}\n")["paths"] == {}


def test_a_spec_whose_references_multiply_is_refused_before_adk_writes_them_out():
    assert expanded_size(multiplying(10)) < MAX_SPEC_VALUES
    assert load_spec(json.dumps(multiplying(10)))["components"]
    # A few kilobytes that ADK's parser would take minutes to write out.
    started = time.monotonic()
    with pytest.raises(SpecError, match=r"\$ref written out"):
        load_spec(json.dumps(multiplying(40)))
    assert time.monotonic() - started < 0.5
    # A schema that refers to itself is cut there, as the parser cuts it.
    looped: dict[str, Any] = copy.deepcopy(BILLING_SPEC)
    looped["components"] = {
        "schemas": {"Node": {"properties": {"next": {"$ref": "#/components/schemas/Node"}}}}
    }
    assert expanded_size(looped) < 100


async def test_a_big_spec_is_read_off_the_event_loop_which_carries_on_meanwhile():
    # About half a second of ADK's parser: the loop serves everyone else meanwhile.
    text = json.dumps(multiplying(14))
    ticks: list[float] = []
    reading = True

    async def tick() -> None:
        while reading:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    started = time.monotonic()
    toolset = await spec_toolset(text, operations=["list_charges"])
    finished = time.monotonic()
    reading = False
    await ticker
    assert [tool.name for tool in await toolset.get_tools()] == ["list_charges"]
    during = [at for at in ticks if started < at < finished]
    assert finished - started > 0.1
    assert len(during) >= 5, f"the loop ran {len(during)} times in {finished - started:.2f} s"

    assert (await read_spec(text))["components"]
    with pytest.raises(SpecError, match="YAML alias"):
        await read_spec(alias_bomb())


async def test_an_openapi_tool_with_a_spec_that_multiplies_doesnt_build_pasted_or_fetched(models):
    bomb = alias_bomb()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=bomb)

    for config in ({"source": "inline", "spec": bomb}, {"url": "https://api.example.com/openapi.yaml"}):
        with pytest.raises(BuildError, match="YAML alias"):
            await build_tools(
                "Billing",
                "openapi",
                config,
                models=models,
                http=http_client(transport=httpx.MockTransport(handler)),
                services=services(),
            )


async def test_a_spec_is_fetched_as_a_tools_request_is():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/old.json":
            return httpx.Response(301, headers={"location": "https://api.example.com/openapi.json"})
        return httpx.Response(200, json=BILLING_SPEC)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await fetch_spec(
        "https://api.example.com/old.json",
        services=services(),
        http=http,
        auth=RequestAuth(headers={"Authorization": "Bearer t0k"}),
    )
    assert load_spec(text)["info"]["title"] == "Billing"
    # The credential went with the request it was given for, not the redirect.
    assert seen[0].headers["authorization"] == "Bearer t0k"
    assert "authorization" not in seen[1].headers
    for private in ("http://169.254.169.254/latest/meta-data", "http://10.0.0.5/spec.json"):
        with pytest.raises(SpecError, match="private network"):
            await fetch_spec(private, services=RuntimeServices(), http=http)


async def test_an_openapi_tools_calls_are_guarded_and_signed():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "in_1"})

    transport = httpx.MockTransport(handler)
    provider = Credentials()
    make = guarded_client(services(credentials=provider), credential="cred_1", organization_id="org_1")

    def client() -> httpx.AsyncClient:
        made = make()
        made._transport = transport  # The network, answered here.
        return made

    toolset = toolset_of(BILLING_SPEC, operations=["get_invoice"], client=client)
    [invoice] = await toolset.get_tools()
    answered = await invoice.call(args={"id": "in_1"}, tool_context=None)
    assert answered == {"id": "in_1"}
    assert seen[0].headers["authorization"] == "Bearer t0k"
    assert dict(seen[0].url.params) == {"key": "k1"}
    assert provider.asked[0][2] == "https://api.example.com/billing/invoices/in_1"

    # A spec whose server is the runtime's own network: refused.
    private = copy.deepcopy(BILLING_SPEC)
    private["servers"] = [{"url": "http://169.254.169.254/latest"}]
    [leak] = await toolset_of(private, operations=["get_invoice"], client=client).get_tools()
    with pytest.raises(ToolFailure, match="private network"):
        await leak.call(args={"id": "x"}, tool_context=None)


def test_a_missing_name_says_where_the_deployment_lists_them():
    with pytest.raises(MissingSetting) as missing:
        fill_references(
            "Bearer ${TOKEN}",
            {},
            where="'Orders'",
            note=" (hosted runs read only the names in FORGE_ADMIN_AGENT_TOOL_ENVIRONMENT)",
        )
    assert str(missing.value) == (
        "'Orders' uses ${TOKEN}, which isn't set (hosted runs read only the names in "
        "FORGE_ADMIN_AGENT_TOOL_ENVIRONMENT)"
    )
