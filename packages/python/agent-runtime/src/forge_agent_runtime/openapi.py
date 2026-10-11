"""OpenAPI specs as an agent's tools: fetched as an HTTP tool's requests are
(never from the runtime's own network), read with ADK's parser, and listed
for people to pick the operations an agent may call.

ADK makes each operation a tool named after its ``operationId`` in snake case
(``getInvoice``: ``get_invoice``; one without an ID after its path and
method), cut to 60 characters: :func:`describe_spec` lists them by that name,
which is what an OpenAPI tool's ``operations`` holds. Every request a tool
makes goes through :func:`guarded_client`: refused when it would reach a
private address, and given the credential the tool names, where it may go.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
import yaml
from google.adk.tools.openapi_tool import OpenAPIToolset

from forge_agent_runtime.services import RequestAuth, RuntimeServices
from forge_agent_runtime.tools import MAX_REDIRECTS, check_url, http_client
from forge_common.adk import ToolFailure

#: The most of a spec that's read.
MAX_SPEC_BYTES = 10_000_000


class SpecError(ValueError):
    """A spec that can't be fetched or read, and why."""


@dataclass(frozen=True)
class Operation:
    """One of a spec's operations, as an agent's tool."""

    #: The tool's name, as the agent calls it and an OpenAPI tool's
    #: ``operations`` names it.
    name: str
    #: As the spec names it; empty when it names none.
    operation_id: str
    method: str
    path: str
    summary: str = ""
    description: str = ""


@dataclass(frozen=True)
class SecurityScheme:
    """A way the spec says its API takes credentials, for people to match one to."""

    name: str
    #: OpenAPI's: apiKey, http, oauth2, openIdConnect, mutualTLS.
    type: str
    #: For apiKey: header, query or cookie; its parameter's name.
    location: str = ""
    parameter: str = ""
    #: For http: basic, bearer…
    scheme: str = ""


@dataclass(frozen=True)
class SpecSummary:
    title: str
    version: str
    #: Its servers' URLs, where its operations are called.
    servers: list[str] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)
    security: list[SecurityScheme] = field(default_factory=list)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def load_spec(text: str) -> dict[str, Any]:
    """
    :return: A spec as JSON or YAML has it.
    :raises SpecError: It's neither, or isn't an object.
    """
    kind: Literal["json", "yaml"] = "json" if text.lstrip().startswith("{") else "yaml"
    try:
        loaded = json.loads(text) if kind == "json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as error:
        raise SpecError(f"It isn't JSON or YAML: {str(error).splitlines()[0][:200]}") from None
    if not isinstance(loaded, dict):
        raise SpecError("An OpenAPI spec is an object, with openapi and paths.")
    if not isinstance(loaded.get("paths"), dict):
        raise SpecError("It has no paths: it isn't an OpenAPI spec.")
    return loaded


def toolset_of(
    spec: Mapping[str, Any],
    *,
    operations: list[str] | None = None,
    client: Callable[[], httpx.AsyncClient] | None = None,
) -> OpenAPIToolset:
    """
    :param operations: The tools it offers, by name; None for every one.
    :param client: Makes the client each request goes through.
    :raises SpecError: ADK can't read it.
    """
    try:
        return OpenAPIToolset(spec_dict=dict(spec), tool_filter=operations, httpx_client_factory=client)
    except Exception as error:  # The parser raises whatever the spec trips it on.
        raise SpecError(f"It can't be read: {str(error)[:300]}") from None


async def describe_spec(spec: Mapping[str, Any]) -> SpecSummary:
    """
    :return: What a spec offers: its operations as an agent's tools, in the
        spec's order, and the credentials it says it takes.
    :raises SpecError: ADK can't read it.
    """
    info = _object(spec.get("info"))
    tools = await toolset_of(spec).get_tools()
    operations = [
        Operation(
            name=tool.name,
            operation_id=str(tool.operation.operationId or ""),
            method=tool.endpoint.method.upper(),
            path=tool.endpoint.path,
            summary=str(tool.operation.summary or ""),
            description=str(tool.operation.description or "")[:500],
        )
        for tool in tools
    ]
    servers = [
        str(server.get("url"))
        for server in spec.get("servers") or []
        if isinstance(server, dict) and server.get("url")
    ]
    schemes = _object(_object(spec.get("components")).get("securitySchemes"))
    security = [
        SecurityScheme(
            name=str(name),
            type=str(scheme.get("type") or ""),
            location=str(scheme.get("in") or ""),
            parameter=str(scheme.get("name") or ""),
            scheme=str(scheme.get("scheme") or ""),
        )
        for name, scheme in schemes.items()
        if isinstance(scheme, dict)
    ]
    return SpecSummary(
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
        servers=servers,
        operations=operations,
        security=security,
    )


async def fetch_spec(
    url: str,
    *,
    services: RuntimeServices,
    http: httpx.AsyncClient,
    auth: RequestAuth | None = None,
) -> str:
    """
    A spec at a URL, fetched as a tool's request would be: each hop of a
    redirect checked, a credential sent only to where it was given for.

    :param auth: What the request sends (a credential's), to ``url`` alone.
    :raises SpecError: It can't be reached, or answered with an error.
    """
    sent = auth or RequestAuth()
    first = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            await check_url(url, services)
        except ToolFailure as failure:
            raise SpecError(str(failure)) from None
        own = url == first
        try:
            request = http.build_request(
                "GET",
                # Added to the URL's own query (httpx's params would replace it).
                httpx.URL(url).copy_merge_params(dict(sent.params)) if own and sent.params else url,
                headers=dict(sent.headers) if own else {},
                timeout=services.http_timeout,
            )
            response = await http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise SpecError(f"{url} didn't answer: {type(error).__name__}") from None
        try:
            if response.is_redirect and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code >= 400:
                raise SpecError(f"{url} answered {response.status_code}")
            raw = b""
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_SPEC_BYTES:
                    raise SpecError(f"The spec is over {MAX_SPEC_BYTES:,} bytes")
            return raw.decode("utf-8", errors="replace")
        finally:
            await response.aclose()
    raise SpecError(f"{first} redirected more than {MAX_REDIRECTS} times")


def guarded_client(
    services: RuntimeServices,
    *,
    credential: str = "",
    organization_id: str | None = None,
) -> Callable[[], httpx.AsyncClient]:
    """
    The clients an OpenAPI tool's requests go through: each request is
    refused when it would reach the runtime's own network, and given the
    credential's headers and query parameters (``credential``, the
    organization's), when it may go where the request goes.

    :return: Makes a fresh client per request, as ADK asks.
    """

    async def guard(request: httpx.Request) -> None:
        url = str(request.url)
        await check_url(url, services)
        if not credential:
            return
        provider = services.credentials
        if provider is None:
            raise ToolFailure(
                "It uses one of the organization's credentials, which only the hosted runtime has."
            )
        try:
            auth = await provider(credential, organization_id=organization_id, url=url)
        except LookupError as error:
            raise ToolFailure(str(error)) from None
        for name, value in auth.headers.items():
            request.headers[name] = value
        if auth.params:
            request.url = request.url.copy_merge_params(dict(auth.params))

    def make() -> httpx.AsyncClient:
        return http_client(
            timeout=services.http_timeout,
            follow_redirects=False,
            event_hooks={"request": [guard]},
        )

    return make
