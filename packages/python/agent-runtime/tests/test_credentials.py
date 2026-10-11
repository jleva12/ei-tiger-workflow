"""The organization's credentials on HTTP and OpenAPI tools: each request
signed where the credential may go, refused where it may not; OpenAPI specs
fetched and called as HTTP tools' requests are (never the runtime's own
network); and a spec's operations listed by the names its tools get."""

import copy
from typing import Any

import httpx
import pytest

from forge_agent_runtime.build import BuildError, build_tools
from forge_agent_runtime.openapi import (
    SpecError,
    describe_spec,
    fetch_spec,
    guarded_client,
    load_spec,
    toolset_of,
)
from forge_agent_runtime.services import RequestAuth, RuntimeServices
from forge_agent_runtime.tools import HttpTool, MissingSetting, fill_references
from forge_common.adk import ToolFailure
from tests.conftest import BILLING_SPEC, EXAMPLE_HOSTS


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
    got = await made.run_async(args={"id": "7", "page": 2}, tool_context=None)
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
    ).run_async(args={"id": "7", "page": 2}, tool_context=None)
    assert dict(seen[0].url.params) == {"type": "issue", "id": "7", "page": "2", "key": "k1"}


async def test_a_credential_refused_for_where_the_tool_calls_fails_the_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Nothing is sent")

    with pytest.raises(ToolFailure, match="only sent to https://api.example.com"):
        await tool(handler, url="https://elsewhere.example.com/x/{id}", credentials=Credentials()).run_async(
            args={"id": "1"}, tool_context=None
        )


async def test_a_redirect_elsewhere_goes_without_the_credential():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://elsewhere.example.com/moved"})
        return httpx.Response(200, json={})

    await tool(handler, credentials=Credentials()).run_async(args={"id": "1"}, tool_context=None)
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
    assert built[0].items[0].credential == "cred_1"


# ----------------------------------------------------------------- OpenAPI


async def test_a_spec_lists_its_operations_by_their_tools_names():
    spec = copy.deepcopy(BILLING_SPEC)
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
