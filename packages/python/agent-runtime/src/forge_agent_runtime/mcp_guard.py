"""MCP tools' connections held to the egress policy HTTP tools are (:mod:`.http_guard`).

The MCP SDK (2.x) speaks HTTP with httpx2 and httpcore2, httpx's and
httpcore's continuation under other names: the same API, other classes. The
guard here is http_guard's on them: each connection resolves its host,
checks every address it has and connects to one of those, so a name that
rebinds between a check and the connection reaches nothing private; a proxy
route fails closed; and the client follows no redirect itself (the SDK
follows those within the endpoint's origin, through it).

ADK's ``McpToolset`` makes its clients with its connection's
``httpx_client_factory``. :func:`guarded_mcp_toolset` gives an MCP toolset
this one, however it was made (by URL in a document, or the hosted
runtime's organization servers), and turns off ADK's mTLS probe: when the
machine has a Google client certificate, ADK would connect with Google's
authorized session instead, which sends the deployment's Google credentials
to the server, follows redirects and checks no address.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any, cast

import httpcore2
import httpx2
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset, SseConnectionParams, StreamableHTTPConnectionParams

from forge_agent_runtime.http_guard import HostNotFound, NetworkPolicy, PinnedBackend, check_url
from forge_common.adk import ToolFailure

#: As the SDK's own clients wait: 30 seconds to connect, write or get a connection; 5 minutes to read a stream.
DEFAULT_TIMEOUT = httpx2.Timeout(30.0, read=300.0)

#: Makes a client as ADK and the SDK ask: ``(headers, timeout, auth)``.
McpClientFactory = Callable[..., httpx2.AsyncClient]


class McpGuardError(RuntimeError):
    """An MCP toolset whose connections can't be held to the policy."""


class _PinnedBackend(PinnedBackend):
    core = httpcore2


class _PinnedTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, policy: NetworkPolicy, **options: Any) -> None:
        # httpx2's adapter maps httpcore2's errors and streams responses: only
        # its pool changes, connecting through the pinned backend.
        limits = options["limits"]
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx2.create_ssl_context(
                verify=options["verify"], cert=options["cert"], trust_env=options["trust_env"]
            ),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=options["http1"],
            http2=options["http2"],
            # PinnedBackend is httpcore's; httpcore2 calls the same methods.
            network_backend=cast(httpcore2.AsyncNetworkBackend, _PinnedBackend(policy)),
        )


class _GuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, policy: NetworkPolicy, pool: _PinnedTransport) -> None:
        self.policy, self.pool = policy, pool

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await check_url(str(request.url), self.policy)
        return await self.pool.handle_async_request(request)

    async def aclose(self) -> None:
        await self.pool.aclose()


class _RefusedProxyTransport(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        raise ToolFailure("MCP proxy routes cannot enforce destination IP checks; configure a direct route.")


class GuardedMcpClient(httpx2.AsyncClient):
    """
    An httpx2 client whose every connection is held to ``policy``; its
    proxies (from the environment) refuse instead of connecting unchecked.
    """

    def __init__(self, policy: NetworkPolicy, **options: Any) -> None:
        self._policy = policy
        super().__init__(**options)

    def _init_transport(
        self,
        verify: ssl.SSLContext | str | bool = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: Any = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncBaseTransport:
        if transport is not None:
            return transport
        pool = _PinnedTransport(
            self._policy,
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits or httpx2.Limits(),
        )
        return _GuardedTransport(self._policy, pool)

    def _init_proxy_transport(self, *args: Any, **options: Any) -> httpx2.AsyncBaseTransport:
        return _RefusedProxyTransport()


def mcp_client_factory(policy: NetworkPolicy) -> McpClientFactory:
    """:return: What ADK and the SDK make an MCP connection's clients with: :class:`GuardedMcpClient` for ``policy``."""

    def make(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
    ) -> httpx2.AsyncClient:
        return GuardedMcpClient(
            policy,
            headers=headers,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            auth=auth,
            follow_redirects=False,
        )

    return make


async def check_server_url(url: str, policy: NetworkPolicy) -> str:
    """
    A server's URL, checked before anything connects to it: http or https,
    with a host whose addresses ``policy`` allows. A name that can't be
    found now is left to be checked as it's connected to, as every
    connection is.

    :raises ToolFailure: It's refused, saying why.
    """
    try:
        return await check_url(url, policy)
    except HostNotFound:
        return url


async def _no_mtls() -> None:
    return None


def guarded_mcp_toolset(toolset: BaseToolset, policy: NetworkPolicy) -> BaseToolset:
    """
    Has an MCP toolset connect only as ``policy`` allows: its clients
    made by :func:`mcp_client_factory`, never by ADK's mTLS probe. A toolset
    that isn't ADK's ``McpToolset`` is the deployment's own, and kept as it is.

    :raises McpGuardError: It's an ``McpToolset`` whose connection isn't over
        HTTP, or that ADK no longer makes as this expects.
    """
    if not isinstance(toolset, McpToolset):
        return toolset
    manager = getattr(toolset, "_mcp_session_manager", None)
    params = getattr(manager, "_connection_params", None)
    if not isinstance(params, (SseConnectionParams, StreamableHTTPConnectionParams)) or not hasattr(
        manager, "_get_mtls_transport"
    ):
        raise McpGuardError("Its MCP connection can't be held to the network policy.")
    # ADK types its factories against httpx 1.x; with MCP 2.x it calls them for httpx2 clients.
    params.httpx_client_factory = cast(Any, mcp_client_factory(policy))
    manager._get_mtls_transport = _no_mtls  # type: ignore[union-attr]
    return toolset
