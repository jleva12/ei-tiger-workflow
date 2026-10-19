"""The tools a chat agent's ``tools`` way out connects to, as ADK tools.

Every tool is guarded (``forge_common.adk.ForgeBaseToolset``): a call that
fails answers the model with why, instead of failing the turn, and an
oversized answer is cut down to fit.

An HTTP tool's URL and body are templates its author writes
(:mod:`.http_templates`) and the model fills in with its arguments, so a
request must not reach the runtime's own network: anything but a globally
reachable address (private, loopback, link-local and carrier-grade NAT, where
clouds' metadata services are, multicast and reserved) is refused unless the
deployment allows the host or private networks. Redirects
are followed one hop at a time, each checked the same way.

One HTTP client serves every conversation (hosted, every organization's), so
it keeps no cookies (:func:`http_client`): a cookie one response sets never
goes out with anyone else's request.

A built agent's toolsets serve every conversation too, until whatever holds
the agent closes it, so an agent called as a tool owns the toolsets of the
agents it runs (:class:`CalledAgent`): each call's own runner, which closes
what it finds as the call ends, is only lent them.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urljoin

import httpx
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.auth.auth_tool import AuthConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from forge_agent_runtime.http_guard import (
    GuardedClient,
    network_policy,
    redirect_headers,
    send_guarded,
)
from forge_agent_runtime.http_guard import (
    check_url as check_network_url,
)
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

log = logging.getLogger(__name__)

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
    return GuardedClient(cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])), **options)


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


async def check_url(url: str, services: RuntimeServices) -> str:
    """Validate a destination. Socket connections independently enforce the same policy."""
    return await check_network_url(url, network_policy(services))


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
        # Its headers go only where its author's URL says: never a host the model picks.
        self.url.check_origin(headers=bool(headers), what=f"{label!r}'s URL")
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
        headers = dict(self.headers)
        for hop in range(MAX_REDIRECTS + 1):
            await check_url(url, self.services)
            signed = await self._signed(url, first=hop == 0)
            params = dict(signed.params)
            request = self.http.build_request(
                self.method,
                # Added to the URL's own query (httpx's params would replace it).
                httpx.URL(url).copy_merge_params(params) if params else url,
                headers={**headers, **signed.headers},
                json=None if body is OMITTED else body,
                timeout=self.services.http_timeout,
            )
            response = await send_guarded(self.http, request, self.services)
            try:
                if response.is_redirect and "location" in response.headers:
                    following = urljoin(url, response.headers["location"])
                    headers = redirect_headers(headers, url, following)
                    url = following
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
    """
    One of the organization's workflows, run with the input the model gives its start.

    A runner may wait for the run apart from the call (its ``waits_apart``,
    for a call made in a context: a workflow run's worker, whose run pauses
    until the run it started ends, giving its worker back meanwhile). Such a
    call is long-running (:class:`WorkflowTools`): the runner answers it
    later, and the call itself returns nothing; should it fail, the runner is
    handed its failure to answer it with (its ``refused``).
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        workflow_id: str,
        organization_id: str | None,
        schema: dict[str, Any],
        runner: WorkflowRunner,
        wait: float | None = None,
    ) -> None:
        """:param wait: Seconds to wait for each run; None for the runner's default."""
        super().__init__(name=name, description=description)
        self.workflow_id = workflow_id
        self.organization_id = organization_id
        self.schema = schema if schema else {"type": "object", "properties": {}}
        self.runner = runner
        self.wait = wait

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.schema
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        return await self.runner.run(
            self.workflow_id,
            dict(args),
            organization_id=self.organization_id,
            context=tool_context,
            wait=self.wait,
        )

    def waits_apart(self, context: ReadonlyContext | None) -> bool:
        """:return: Whether its runner waits for a run started in ``context`` apart from the call."""
        waits = getattr(self.runner, "waits_apart", None)
        return context is not None and callable(waits) and bool(waits(context))


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
            if isinstance(item, ForgeBaseToolset):
                # Its tools failing to list are this toolset's to give or raise, as its caller asks.
                tools.extend(await item.get_tools_or_raise(readonly_context))
            elif isinstance(item, BaseToolset):
                tools.extend(await item.get_tools(readonly_context))
            else:
                tools.append(item)
        if self.confirm:
            for tool in tools:
                tool.check_require_confirmation = _always  # type: ignore[method-assign]
        return tools

    async def close(self) -> None:
        try:
            for item in self.items:
                if isinstance(item, BaseToolset):
                    await item.close()
        finally:
            # The results it kept whole in files go too, however its items' closing ended.
            await super().close()


class WorkflowTools(GuardedTools):
    """
    A workflow tool, guarded as every tool is. Where its runner waits for the
    run apart from the call (:meth:`WorkflowTool.waits_apart`, asked of each
    model request's context), the model is given a long-running copy of it:
    ADK pauses its agent at the call, and the runner answers it once the run
    ends. A call ADK pauses at can't answer itself, so one that fails (input
    that doesn't fit the workflow's start, a run that can't be started) hands
    its runner its failure, as the guard words it, and returns nothing too;
    and it has no time limit, its call only starting the run. Anywhere else
    (a chat agent's runtime, a sub-agent's own runner) it's the tool as it
    is, its call waiting for the run.
    """

    def __init__(self, tool: WorkflowTool, *, timeout_seconds: float | None = None) -> None:
        super().__init__([tool], timeout_seconds=timeout_seconds)
        self.tool = tool

    async def get_raw_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        if not self.tool.waits_apart(readonly_context):
            return [self.tool]
        apart = copy.copy(self.tool)
        apart.is_long_running = True
        run = apart.run_async
        refused = getattr(self.tool.runner, "refused", None)

        async def run_async(*, args: dict[str, Any], tool_context: ToolContext) -> Any:
            try:
                return await run(args=args, tool_context=tool_context)
            except Exception as error:
                if not callable(refused):
                    raise
                refused(tool_context, dict(self._failed(apart, args, error)))
                return None

        apart.run_async = run_async  # type: ignore[method-assign]
        return [apart]

    def timeout_for(self, tool: BaseTool) -> float | None:
        # A long-running copy's call only starts its run: its wait is its runner's.
        return None if tool.is_long_running else super().timeout_for(tool)


class LentToolset(BaseToolset):
    """
    A toolset lent to a runner that doesn't own it: its tools and what it
    adds to model requests are the toolset's own, and closing it does
    nothing, its owner closing the toolset itself.
    """

    def __init__(self, toolset: BaseToolset) -> None:
        super().__init__()
        self.toolset = toolset
        # The toolset keeps its tools for an invocation, or not, as it does.
        self._use_invocation_cache = False

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return await self.toolset.get_tools_with_prefix(readonly_context)

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        await self.toolset.process_llm_request(tool_context=tool_context, llm_request=llm_request)

    def get_auth_config(self) -> AuthConfig | None:
        return self.toolset.get_auth_config()

    async def close(self) -> None:
        """Leaves the toolset open: its owner closes it."""


class CalledAgent(BaseToolset):
    """
    An agent called as a tool (ADK's ``AgentTool``), with the toolsets of
    the agents it runs, which it owns.

    ADK runs each call on a runner of its own, which closes every toolset it
    finds on the agents as the call ends. Those agents are part of a built
    graph every conversation shares, so one chat's call ending would close
    an MCP session another chat's call is using. Their toolsets are lent to
    those runners instead (:class:`LentToolset`), and closed with this
    toolset by whatever owns the graph: the runner it's built for, a
    workflow's run.
    """

    def __init__(self, agent: BaseAgent) -> None:
        super().__init__()
        self.owned = _lent(agent)
        self.tool = AgentTool(agent)

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        return [self.tool]

    async def close(self) -> None:
        """Closes the toolsets it owns, each though another's closing fails."""
        for toolset in self.owned:
            try:
                await toolset.close()
            except Exception:
                log.warning("Closing %s of %s failed", type(toolset).__name__, self.tool.name, exc_info=True)


def _lent(agent: BaseAgent) -> list[BaseToolset]:
    """
    Give an agent, and the agents it hands off to, lent toolsets in place of
    their own (:class:`LentToolset`).

    :return: Their own, for their owner to close.
    """
    owned: list[BaseToolset] = []
    if isinstance(agent, LlmAgent):
        tools: list[Any] = []
        for tool in agent.tools:
            if isinstance(tool, BaseToolset):
                owned.append(tool)
                tool = LentToolset(tool)
            tools.append(tool)
        agent.tools = tools
    for sub_agent in agent.sub_agents:
        owned.extend(_lent(sub_agent))
    return owned
