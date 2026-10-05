"""The tools a chat agent's ``tools`` way out connects to, as ADK tools.

Every tool is guarded (``forge_common.adk.ForgeBaseToolset``): a call that
fails answers the model with why, instead of failing the turn, and an
oversized answer is cut down to fit.

An HTTP tool's URL is written by the agent's author and its arguments by the
model, so a request must not reach the runtime's own network: private,
loopback, link-local (cloud metadata), multicast and reserved addresses are
refused unless the deployment allows the host or private networks. Redirects
are followed one hop at a time, each checked the same way.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from forge_agent_runtime.services import KnowledgeBases, RuntimeServices, WorkflowRunner
from forge_common.adk import ForgeBaseToolset, ToolFailure

#: The most redirects one request follows.
MAX_REDIRECTS = 5
#: ``${NAME}``: a secret or setting, filled in from the environment.
REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: ``{name}`` in an HTTP tool's URL: one of its arguments.
ARGUMENT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingSetting(LookupError):
    """``${NAME}`` names something the environment doesn't have."""


def fill_references(text: str, environment: Mapping[str, str], *, where: str) -> str:
    """:return: ``text`` with each ``${NAME}`` filled in from ``environment``."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environment:
            raise MissingSetting(f"{where} uses ${{{name}}}, which isn't set")
        return environment[name]

    return REFERENCE.sub(fill, text)


def header_dict(rows: Any, environment: Mapping[str, str], *, where: str) -> dict[str, str]:
    """:return: A tool's header rows (``[{name, value}]``) as headers, secrets filled in."""
    headers: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            headers[name] = fill_references(str(row.get("value") or ""), environment, where=where)
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
    An HTTP endpoint the model calls with arguments its JSON Schema declares.
    ``{name}`` in the URL takes the argument of that name; the rest go in the
    query (GET, DELETE) or as a JSON body. It answers with the status and
    the body (JSON when it's JSON).
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
    ) -> None:
        super().__init__(name=name, description=description or f"Calls {method} {url}")
        self.method = method.upper()
        self.url = url
        self.headers = headers
        self.parameters = parameters if parameters else {"type": "object", "properties": {}}
        self.confirm = confirm
        self.services = services
        self.http = http

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.parameters
        )

    async def check_require_confirmation(self, args: dict[str, Any], tool_context: ToolContext) -> bool:
        return self.confirm

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        rest = dict(args)

        def put(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in rest:
                raise ToolFailure(f"Its URL needs the {name!r} argument.", [f"Call it again with {name}."])
            return quote(str(rest.pop(name)), safe="")

        url = ARGUMENT.sub(put, self.url)
        query, body = (rest, None) if self.method in ("GET", "DELETE", "HEAD") else (None, rest)
        for _ in range(MAX_REDIRECTS + 1):
            await check_url(url, self.services)
            request = self.http.build_request(
                self.method,
                url,
                headers=self.headers,
                params=query or None,
                json=body,
                timeout=self.services.http_timeout,
            )
            response = await self.http.send(request, stream=True)
            try:
                if response.is_redirect and "location" in response.headers:
                    url, query = urljoin(url, response.headers["location"]), None
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
    ) -> None:
        super().__init__(name=name, description=description)
        self.knowledge_base_ids = knowledge_base_ids
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
            return {"passages": [], "note": "Nothing in the knowledge base matches; don't make up an answer."}
        return {"passages": passages}


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
