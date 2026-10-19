"""HTTP egress policy enforced at the socket connection, including DNS rebinding.

The pool keeps the original origin, Host header and TLS hostname. Its backend
resolves and checks every address, then connects only to one of those numeric
addresses. Policies have separate pools: a connection opened with private
network access cannot be reused by a request without it.

Proxies resolve destinations themselves, beyond this guard's control. Configured
proxy routes fail closed; they are never silently bypassed with a direct request.
The httpx integration points below are covered by transport-level regression tests.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from forge_common.adk import ToolFailure

POLICY_EXTENSION = "forge_network_policy"
_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


@dataclass(frozen=True)
class NetworkPolicy:
    allow_private: bool = False
    allowed_hosts: tuple[str, ...] = ()

    def allows(self, host: str) -> bool:
        return self.allow_private or host.lower() in self.allowed_hosts


def network_policy(services: Any) -> NetworkPolicy:
    return NetworkPolicy(services.allow_private, tuple(sorted(h.lower() for h in services.allowed_hosts)))


#: NAT64's well-known prefix: where a network has a NAT64 gateway, its last 32 bits are the IPv4 address reached.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: IPv4-compatible addresses (``::a.b.c.d``), deprecated; ``::`` and ``::1`` among them aren't global anyway.
_COMPATIBLE = ipaddress.IPv6Network("::/96")


def _reached(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """:return: The IPv4 address an IPv6 one carries (mapped, NAT64 or compatible), or the address itself."""
    if isinstance(ip, ipaddress.IPv4Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or ip in _COMPATIBLE:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _refused(address: str) -> bool:
    """
    Anything but a globally reachable unicast address is refused: private,
    loopback, link-local (cloud metadata), carrier-grade NAT (100.64.0.0/10,
    where some clouds' metadata services are), benchmarking, documentation,
    reserved and multicast addresses. An IPv6 address that carries an IPv4
    one is held to what the IPv4 address is.
    """
    ip = _reached(ipaddress.ip_address(address))
    return not ip.is_global or ip.is_multicast


class HostNotFound(ToolFailure):
    """A host whose name doesn't resolve to any address (now)."""


async def addresses_for(host: str, port: int | None, policy: NetworkPolicy) -> list[str]:
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HostNotFound(f"{host} can't be found: {exc.strerror or exc}") from exc
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise HostNotFound(f"{host} has no network addresses.")
    if not policy.allows(host) and any(_refused(address) for address in addresses):
        raise ToolFailure(f"{host} is on a private network, which tools can't reach.")
    return addresses


async def check_url(url: str, policy: NetworkPolicy) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolFailure(f"Its URL must start with http:// or https://, not {url[:80]!r}")
    if not parts.hostname:
        raise ToolFailure(f"Its URL has no host: {url[:80]!r}")
    # Explicit allowlists also work in mock transports without DNS records.
    if not policy.allows(parts.hostname):
        await addresses_for(parts.hostname, parts.port, policy)
    return url


class PinnedBackend(httpcore.AsyncNetworkBackend):
    #: The httpcore it connects for: httpx's, or another with its API (the
    #: MCP client's httpcore2, :mod:`.mcp_guard`), whose errors it raises.
    core: Any = httpcore

    def __init__(self, policy: NetworkPolicy, backend: Any = None):
        self.policy = policy
        self.backend = backend or self.core.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend protocol
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                addresses = await addresses_for(host, port, self.policy)
                for index, address in enumerate(addresses):
                    try:
                        return await self.backend.connect_tcp(
                            address,
                            port,
                            timeout=None if deadline is None else max(0, deadline - time.monotonic()),
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except (self.core.ConnectError, self.core.ConnectTimeout):
                        if index == len(addresses) - 1:
                            raise
        except TimeoutError as exc:
            raise self.core.ConnectTimeout("Timed out resolving or connecting to the host") from exc
        raise self.core.ConnectError("The host has no reachable addresses")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, policy: NetworkPolicy, options: Mapping[str, Any]):
        # HTTPX's adapter maps httpcore exceptions and streams responses. Only
        # its pool construction changes, using httpcore's public backend API.
        limits = options["limits"]
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(
                verify=options["verify"],
                cert=options["cert"],
                trust_env=options["trust_env"],
            ),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=options["http1"],
            http2=options["http2"],
            network_backend=PinnedBackend(policy),
        )


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, **options: Any):
        self.options = options
        self.pools: dict[NetworkPolicy, _PinnedTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        policy = request.extensions.get(POLICY_EXTENSION, NetworkPolicy())
        await check_url(str(request.url), policy)
        if policy not in self.pools:
            self.pools[policy] = _PinnedTransport(policy, self.options)
        return await self.pools[policy].handle_async_request(request)

    async def aclose(self) -> None:
        for pool in self.pools.values():
            await pool.aclose()
        self.pools.clear()


class _RefusedProxyTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise ToolFailure(
            "HTTP tool proxy routes cannot enforce destination IP checks; configure a direct route."
        )


class GuardedClient(httpx.AsyncClient):
    def _init_transport(
        self,
        verify: ssl.SSLContext | str | bool = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> httpx.AsyncBaseTransport:
        return (
            transport
            if transport is not None
            else GuardedTransport(
                verify=verify,
                cert=cert,
                trust_env=trust_env,
                http1=http1,
                http2=http2,
                limits=limits,
            )
        )

    def _init_proxy_transport(self, *args: Any, **options: Any) -> httpx.AsyncBaseTransport:
        return _RefusedProxyTransport()


async def prepare_request(client: httpx.AsyncClient, request: httpx.Request, services: Any) -> None:
    policy = network_policy(services)
    await check_url(str(request.url), policy)
    transport = client._transport_for_url(request.url)
    if not isinstance(transport, (GuardedTransport, httpx.MockTransport, httpx.ASGITransport)):
        if isinstance(transport, _RefusedProxyTransport):
            await transport.handle_async_request(request)
        raise ToolFailure("HTTP tools require http_client() with its guarded network transport.")
    request.extensions[POLICY_EXTENSION] = policy


async def send_guarded(
    client: httpx.AsyncClient,
    request: httpx.Request,
    services: Any,
) -> httpx.Response:
    await prepare_request(client, request, services)
    return await client.send(request, stream=True, follow_redirects=False)


def origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.host, parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


def redirect_headers(headers: Mapping[str, str], previous: str, following: str) -> dict[str, str]:
    """Custom header names can carry secrets too: retain only representation headers across origins."""
    if origin(previous) == origin(following):
        return dict(headers)
    safe = {"accept", "accept-encoding", "accept-language", "content-type", "user-agent"}
    return {name: value for name, value in headers.items() if name.lower() in safe}
