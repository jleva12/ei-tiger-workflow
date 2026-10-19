"""Exercise HTTPX/httpcore's real request and connection paths with a recording socket backend."""

import asyncio
import socket

import httpcore
import httpx
import pytest

from forge_agent_runtime.http_guard import NetworkPolicy, PinnedBackend, check_url, send_guarded
from forge_agent_runtime.openapi import SpecError, fetch_spec, guarded_client
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import HttpTool, http_client
from forge_common.adk import ToolFailure


def http_tool(client, **kwargs):
    return HttpTool(
        name="fetch",
        description="Fetch",
        method="GET",
        url="https://api.example.com/start",
        parameters={},
        confirm=False,
        http=client,
        headers={
            "Authorization": "Bearer secret",
            "X-API-Key": "secret",
            "X-Custom-Token": "secret",
            "Cookie": "secret=1",
            "Accept": "application/json",
        },
        services=RuntimeServices(allowed_hosts=("api.example.com", "other.example.com")),
        **kwargs,
    )


@pytest.mark.parametrize(
    "following",
    ["https://other.example.com/final", "http://api.example.com/final", "https://api.example.com:8443/final"],
)
async def test_redirects_strip_all_custom_credentials_on_origin_changes(following):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "/same-origin"})
        if len(seen) == 2:
            return httpx.Response(302, headers={"Location": following})
        return httpx.Response(200, json={})

    # A supplied client's automatic redirects must not skip our checks.
    async with http_client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        await http_tool(client).run_async(args={}, tool_context=None)
    for name in ("authorization", "x-api-key", "x-custom-token", "cookie"):
        assert name in seen[0].headers and name in seen[1].headers
        assert name not in seen[2].headers
    assert seen[2].headers["accept"] == "application/json"


def dns_rows(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 443))]


async def test_rebinding_between_validation_and_connect_cannot_open_a_private_socket(monkeypatch):
    resolutions = []

    async def resolve(host, port, **kwargs):
        resolutions.append(host)
        return dns_rows("8.8.8.8" if len(resolutions) == 1 else "127.0.0.1", port)

    async def forbidden(*args, **kwargs):
        pytest.fail("A rebinding hostname reached a TCP socket")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    async with http_client(trust_env=False) as client:
        with pytest.raises(ToolFailure, match="private network"):
            await client.get("https://rebind.example.com/")
    assert len(resolutions) == 2


@pytest.mark.parametrize("addresses", [["8.8.8.8", "127.0.0.1"], ["::ffff:127.0.0.1"], ["169.254.169.254"]])
async def test_all_resolved_addresses_are_checked_before_connecting(monkeypatch, addresses):
    async def resolve(host, port, **kwargs):
        return [row for address in addresses for row in dns_rows(address, port)]

    async def forbidden(*args, **kwargs):
        pytest.fail("Refused address reached a socket")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    with pytest.raises(ToolFailure, match="private network"):
        await PinnedBackend(NetworkPolicy()).connect_tcp("rebind.example.com", 443)


NOT_GLOBAL = [
    "100.100.100.200",  # carrier-grade NAT: Alibaba Cloud's metadata service
    "100.64.0.1",
    "::ffff:100.100.100.200",
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "::7f00:1",  # IPv4-compatible 127.0.0.1
    "2002:7f00:1::",  # 6to4
    "198.18.0.1",  # benchmarking
    "192.0.0.1",
    "192.0.2.1",  # documentation
    "240.0.0.1",
    "224.0.0.1",
    "ff02::1",
    "fc00::1",
]


@pytest.mark.parametrize("address", NOT_GLOBAL)
async def test_only_globally_reachable_addresses_are_connected_to(monkeypatch, address):
    async def resolve(host, port, **kwargs):
        return dns_rows(address, port)

    async def forbidden(*args, **kwargs):
        pytest.fail("A refused address reached a socket")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    for host in ("metadata.example.com", address):
        with pytest.raises(ToolFailure, match="private network"):
            await PinnedBackend(NetworkPolicy()).connect_tcp(host, 80)


@pytest.mark.parametrize("address", ["8.8.8.8", "::ffff:8.8.8.8", "64:ff9b::808:808", "2606:4700:4700::1111"])
async def test_public_addresses_however_written_are_connected_to(monkeypatch, address):
    connected = []

    async def connect(self, host, port, **kwargs):
        connected.append(host)
        return RecordedStream([], [])

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    await PinnedBackend(NetworkPolicy()).connect_tcp(address, 443)
    assert connected == [address]


async def test_a_deployment_can_still_allow_private_networks_or_a_host(monkeypatch):
    connected = []

    async def resolve(host, port, **kwargs):
        return dns_rows("100.100.100.200", port)

    async def connect(self, host, port, **kwargs):
        connected.append(host)
        return RecordedStream([], [])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    await PinnedBackend(NetworkPolicy(allow_private=True)).connect_tcp("metadata.example.com", 80)
    await PinnedBackend(NetworkPolicy(allowed_hosts=("metadata.example.com",))).connect_tcp(
        "metadata.example.com", 80
    )
    assert connected == ["100.100.100.200", "100.100.100.200"]


class RecordedStream(httpcore.AsyncNetworkStream):
    def __init__(self, written, tls):
        self.written, self.tls = written, tls
        self.response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"

    async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109 - httpcore protocol
        result, self.response = self.response, b""
        return result

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109 - httpcore protocol
        self.written.append(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109 - httpcore protocol
        self.tls.append((server_hostname, ssl_context.check_hostname))
        return self

    async def aclose(self):
        pass


@pytest.mark.parametrize("route", ["tool", "spec", "openapi"])
async def test_every_http_entry_point_connects_to_numeric_ip_and_preserves_host_and_tls(monkeypatch, route):
    connected: list[tuple[str, int]] = []
    written: list[bytes] = []
    tls: list[tuple[str, bool]] = []

    async def resolve(host, port, **kwargs):
        return dns_rows("8.8.8.8", port)

    async def connect(self, host, port, **kwargs):
        connected.append((host, port))
        return RecordedStream(written, tls)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    services = RuntimeServices()
    if route == "openapi":
        # Clear proxy configuration so this exercises the direct socket path.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        client = guarded_client(services)()
    else:
        client = http_client(trust_env=False)
    async with client:
        if route == "tool":
            made = http_tool(client)
            made.services = services
            assert await made.run_async(args={}, tool_context=None) == {"status": 200, "body": {}}
        elif route == "spec":
            assert await fetch_spec("https://api.example.com/start", services=services, http=client) == "{}"
        else:
            assert (await client.get("https://api.example.com/start")).json() == {}
    assert connected == [("8.8.8.8", 443)]
    assert tls == [("api.example.com", True)]
    assert b"Host: api.example.com\r\n" in b"".join(written)


async def test_numeric_ipv6_is_pinned_without_resolving_it(monkeypatch):
    connected = []

    async def resolve(*args, **kwargs):
        pytest.fail("A numeric address must not be resolved again")

    async def connect(self, host, port, **kwargs):
        connected.append(host)
        return RecordedStream([], [])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    await PinnedBackend(NetworkPolicy()).connect_tcp("2606:4700:4700::1111", 443)
    assert connected == ["2606:4700:4700::1111"]


async def test_unguarded_injected_clients_fail_closed():
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ToolFailure, match="guarded network transport"):
            await send_guarded(client, client.build_request("GET", "https://8.8.8.8"), RuntimeServices())
        with pytest.raises(SpecError, match="guarded network transport"):
            await fetch_spec("https://8.8.8.8", services=RuntimeServices(), http=client)


async def test_proxy_routes_are_never_silently_bypassed(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9123")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    async with http_client() as client:
        with pytest.raises(ToolFailure, match="proxy routes"):
            await send_guarded(client, client.build_request("GET", "https://8.8.8.8"), RuntimeServices())


async def test_explicitly_allowed_hosts_can_connect_to_private_addresses(monkeypatch):
    connected = []

    async def resolve(host, port, **kwargs):
        return dns_rows("127.0.0.1", port)

    async def connect(self, host, port, **kwargs):
        connected.append(host)
        return RecordedStream([], [])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    policy = NetworkPolicy(allowed_hosts=("intranet",))
    assert await check_url("http://intranet/", policy)
    await PinnedBackend(policy).connect_tcp("intranet", 80)
    assert connected == ["127.0.0.1"]
