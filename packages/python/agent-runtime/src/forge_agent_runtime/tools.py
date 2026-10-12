"""The tools a chat agent's ``tools`` way out connects to, as ADK tools.

Every tool is guarded (``forge_common.adk.ForgeBaseToolset``): a call that
fails answers the model with why, instead of failing the turn, and an
oversized answer is cut down to fit.

An HTTP tool's URL and body are templates its author writes
(:mod:`.http_templates`) and the model fills in with its arguments, so a
request must not reach the runtime's own network: private,
loopback, link-local (cloud metadata), multicast and reserved addresses are
refused unless the deployment allows the host or private networks. Redirects
are followed one hop at a time, each checked the same way.

One HTTP client serves every conversation (hosted, every organization's), so
it keeps no cookies (:func:`http_client`): a cookie one response sets never
goes out with anyone else's request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from forge_agent_runtime.http_templates import (
    BODY_METHODS,
    OMITTED,
    UrlTemplate,
    body_arguments,
    parameters_of,
    parse_body,
    parse_query,
    query_arguments,
    render_body,
    render_query,
    render_url,
    run_values,
)
from forge_agent_runtime.services import (
    KnowledgeBases,
    RequestAuth,
    RuntimeServices,
    WorkflowRunner,
)
from forge_common.adk import ForgeBaseToolset, ToolFailure
from forge_common.urls import with_query

#: The most redirects one request follows.
MAX_REDIRECTS = 5
#: ``${NAME}``: a secret or setting, filled in from the environment.
REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def http_client(**options: Any) -> httpx.AsyncClient:
    """
    :param options: ``httpx.AsyncClient``'s (a timeout, a transport).
    :return: An HTTP client that keeps no cookies: whoever's request it sends
        next, it sends none that an earlier response set.
    """
    return httpx.AsyncClient(cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])), **options)


class MissingSetting(LookupError):
    """``${NAME}`` names something the environment doesn't have."""


def fill_references(text: str, environment: Mapping[str, str], *, where: str, note: str = "") -> str:
    """
    :param note: What a missing one's error adds (where a deployment lists them).
    :return: ``text`` with each ``${NAME}`` filled in from ``environment``.
    """

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environment:
            raise MissingSetting(f"{where} uses ${{{name}}}, which isn't set{note}")
        return environment[name]

    return REFERENCE.sub(fill, text)


def header_dict(rows: Any, environment: Mapping[str, str], *, where: str, note: str = "") -> dict[str, str]:
    """:return: A tool's header rows (``[{name, value}]``) as headers, secrets filled in."""
    headers: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            headers[name] = fill_references(str(row.get("value") or ""), environment, where=where, note=note)
    return headers


def _refused(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def check_url(url: str, services: RuntimeServices) -> str:
    """
    :return: The URL, when a tool may call it.
    :raises ToolFailure: It isn't http(s), has no host, or its host is (or
        resolves to) an address tools may not reach.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolFailure(f"Its URL must start with http:// or https://, not {url[:80]!r}")
    host = parts.hostname
    if not host:
        raise ToolFailure(f"Its URL has no host: {url[:80]!r}")
    if services.allow_private or host.lower() in {h.lower() for h in services.allowed_hosts}:
        return url
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parts.port or None, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise ToolFailure(f"{host} can't be found: {exc.strerror or exc}") from exc
        addresses = sorted({str(info[4][0]) for info in infos})
    if any(_refused(address) for address in addresses):
        raise ToolFailure(f"{host} is on a private network, which tools can't reach.")
    return url


def _body(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


class HttpTool(BaseTool):
    """
    An HTTP endpoint the model calls with the arguments its URL and body name
    (:mod:`.http_templates`): ``{name}`` takes the argument of that name,
    ``{{ expression }}`` one of the run's values. It answers with the status
    and the body (JSON when it's JSON).

    With a ``credential`` (one of the organization's), each request is signed
    with it, where it may go: the first is refused when it may not, and a
    redirect elsewhere goes without it.

    :param url: Its URL, a template.
    :param query: Its query parameters, ``[{name, value}]``, each a template:
        added to the URL's query, each name and value percent-encoded.
    :param body: Its JSON body, a template; sent by POST, PUT and PATCH only.
    :param parameters: A JSON Schema of the arguments: their types and
        descriptions. The model is offered those the URL and body name.
    :raises MissingSetting: A ``${NAME}`` in them isn't in the environment.
    :raises TemplateProblem: The body isn't JSON, or a ``{{ }}`` is empty.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        method: str,
        url: str,
        headers: dict[str, str],
        parameters: dict[str, Any],
        confirm: bool,
        services: RuntimeServices,
        http: httpx.AsyncClient,
        body: str = "",
        query: Any = (),
        credential: str = "",
        organization_id: str | None = None,
        label: str = "",
    ) -> None:
        super().__init__(name=name, description=description or f"Calls {method} {url}")
        label = label or name

        def fill(setting: str, *, where: str) -> str:
            return fill_references(
                f"${{{setting}}}", services.environment, where=where, note=services.environment_note
            )

        self.method = method.upper()
        self.url = UrlTemplate.parse(
            url, lambda n: fill(n, where=f"{label!r}'s URL"), what=f"{label!r}'s URL"
        )
        self.body = (
            parse_body(body, lambda n: fill(n, where=f"{label!r}'s body"), what=f"{label!r}'s body")
            if self.method in BODY_METHODS
            else None
        )
        self.query = parse_query(
            query, lambda n: fill(n, where=f"{label!r}'s query"), what=f"{label!r}'s query parameter"
        )
        self.headers = headers
        names, required = self.url.arguments()
        in_query, query_needs = query_arguments(self.query)
        names += [n for n in in_query if n not in names]
        required |= query_needs
        in_body = body_arguments(self.body)
        optional = {n for n in names if n not in required and n not in in_body}
        names += [n for n in in_body if n not in names]
        self.parameters = parameters_of(
            parameters if isinstance(parameters, dict) else {}, names, required, optional
        )
        self.confirm = confirm
        self.services = services
        self.http = http
        self.credential = credential
        self.organization_id = organization_id

    async def _signed(self, url: str, *, first: bool) -> RequestAuth:
        """What the credential has a request to ``url`` send; nothing without one."""
        if not self.credential:
            return RequestAuth()
        provider = self.services.credentials
        if provider is None:
            raise ToolFailure(
                "It uses one of the organization's credentials, which only the hosted runtime has."
            )
        try:
            return await provider(self.credential, organization_id=self.organization_id, url=url)
        except LookupError as error:
            if first:
                raise ToolFailure(str(error)) from None
            # A redirect away from where the credential may go: without it.
            return RequestAuth()

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.parameters
        )

    async def check_require_confirmation(self, args: dict[str, Any], tool_context: ToolContext) -> bool:
        return self.confirm

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        # Only what its URL and body name: anything else the model sends goes nowhere.
        values = run_values(self.services.tool_values, tool_context, what=f"{self.name!r}")
        # The listed query parameters after the URL's own, each name and value encoded.
        url = with_query(
            await render_url(self.url, args, values), await render_query(self.query, args, values)
        )
        body = OMITTED if self.body is None else await render_body(self.body, args, values)
        for hop in range(MAX_REDIRECTS + 1):
            await check_url(url, self.services)
            signed = await self._signed(url, first=hop == 0)
            params = dict(signed.params)
            request = self.http.build_request(
                self.method,
                # Added to the URL's own query (httpx's params would replace it).
                httpx.URL(url).copy_merge_params(params) if params else url,
                headers={**self.headers, **signed.headers},
                json=None if body is OMITTED else body,
                timeout=self.services.http_timeout,
            )
            response = await self.http.send(request, stream=True)
            try:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                raw = b""
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > self.services.max_response_bytes:
                        raise ToolFailure(f"Its response is over {self.services.max_response_bytes:,} bytes.")
                return {
                    "status": response.status_code,
                    "body": _body(raw, response.headers.get("content-type", "")),
                }
            finally:
                await response.aclose()
        raise ToolFailure(f"It was redirected more than {MAX_REDIRECTS} times.")


class WorkflowTool(BaseTool):
    """One of the organization's workflows, run with the input the model gives its start."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        workflow_id: str,
        organization_id: str | None,
        schema: dict[str, Any],
        runner: WorkflowRunner,
    ) -> None:
        super().__init__(name=name, description=description)
        self.workflow_id = workflow_id
        self.organization_id = organization_id
        self.schema = schema if schema else {"type": "object", "properties": {}}
        self.runner = runner

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.schema
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        return await self.runner.run(
            self.workflow_id, dict(args), organization_id=self.organization_id, context=tool_context
        )


class KnowledgeBaseTool(BaseTool):
    """Searches some of the organization's knowledge bases (their documents,
    parsed, chunked and embedded) for the passages that best match what the
    model asks, by meaning and by its words."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        knowledge_base_ids: list[str],
        organization_id: str | None,
        max_results: int,
        knowledge_bases: KnowledgeBases,
        knowledge_base_names: list[str] | None = None,
    ) -> None:
        super().__init__(name=name, description=description)
        self.knowledge_base_ids = knowledge_base_ids
        # What its results say it searched (a chat UI's card for the call).
        self.knowledge_base_names = knowledge_base_names or list(knowledge_base_ids)
        self.organization_id = organization_id
        self.max_results = max_results
        self.knowledge_bases = knowledge_bases

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, as a question or the words the passages would use.",
                    }
                },
                "required": ["query"],
            },
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolFailure("Say what to look for in query.")
        passages = await self.knowledge_bases.search(
            self.knowledge_base_ids, query, organization_id=self.organization_id, limit=self.max_results
        )
        if not passages:
            return {
                "searched": self.knowledge_base_names,
                "passages": [],
                "note": "Nothing in the knowledge base matches; don't make up an answer.",
            }
        return {"searched": self.knowledge_base_names, "passages": passages}


class SystemDesignTool(BaseTool):
    """Reads the design of the system design knowledge bases a knowledge base
    tool searches: their applications, the other parts of their systems
    (databases, queues, external systems…), the groups those are in and how
    they all connect; whole, or around one part (``focus``). Built beside
    the search when it searches one."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        knowledge_base_ids: list[str],
        organization_id: str | None,
        knowledge_bases: KnowledgeBases,
    ) -> None:
        super().__init__(name=name, description=description)
        self.knowledge_base_ids = knowledge_base_ids
        self.organization_id = organization_id
        self.knowledge_bases = knowledge_bases

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "An application's, component's or group's name (or part of it): "
                        "only that part, what it connects to and how. Leave it out for the whole design.",
                    }
                },
            },
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        focus = str(args.get("focus") or "").strip()
        found = await self.knowledge_bases.design(
            self.knowledge_base_ids, organization_id=self.organization_id, focus=focus
        )
        return found if found is not None else {"systems": []}


async def _always(args: dict[str, Any], tool_context: ToolContext) -> bool:
    return True


class GuardedTools(ForgeBaseToolset):
    """
    Tools and toolsets that answer instead of raising (``ForgeBaseToolset``),
    optionally each confirmed by a person before it runs.
    """

    default_suggested_fixes = ("Check the arguments against the tool's parameters and call it again.",)

    def __init__(
        self,
        items: list[BaseTool | BaseToolset],
        *,
        confirm: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self.items = items
        self.confirm = confirm

    async def get_raw_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for item in self.items:
            if isinstance(item, BaseToolset):
                tools.extend(await item.get_tools(readonly_context))
            else:
                tools.append(item)
        if self.confirm:
            for tool in tools:
                tool.check_require_confirmation = _always  # type: ignore[method-assign]
        return tools

    async def close(self) -> None:
        for item in self.items:
            if isinstance(item, BaseToolset):
                await item.close()
