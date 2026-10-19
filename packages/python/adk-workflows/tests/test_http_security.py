import asyncio
import socket

import httpcore
import httpx
import pytest
from forge_agent_runtime.tools import http_client

from forge_task_adk_workflows.graph.factories.actions import send
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.errors import StepFailed


async def test_workflow_redirects_strip_static_credentials_across_origins():
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(307, headers={"Location": "https://other.example.com/new"})
        return httpx.Response(200, json={})

    async with http_client(transport=httpx.MockTransport(handle)) as client:
        services = RunServices(http=client, allowed_hosts=("api.example.com", "other.example.com"))
        result = await send(
            services,
            "POST",
            "https://api.example.com/start",
            {
                "Authorization": "Bearer secret",
                "X-API-Key": "secret",
                "X-Secret": "secret",
                "Accept": "application/json",
            },
            {"json": {"value": 1}},
            seconds=5,
        )
    assert result["status"] == 200
    assert seen[1].headers["accept"] == "application/json"
    assert all(name not in seen[1].headers for name in ("authorization", "x-api-key", "x-secret"))


async def test_workflow_requests_use_pinned_tcp_addresses(monkeypatch):
    connected = []
    backend = httpcore.AsyncMockBackend([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"])

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]

    async def connect(self, host, port, **kwargs):
        connected.append(host)
        return await backend.connect_tcp(host, port, **kwargs)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with http_client(trust_env=False) as client:
        result = await send(RunServices(http=client), "GET", "https://api.example.com/data", {}, {}, seconds=5)
    assert result["status"] == 200
    assert connected == ["8.8.8.8"]


async def test_workflow_requests_refuse_unguarded_network_clients():
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(StepFailed, match="guarded network transport"):
            await send(RunServices(http=client), "GET", "https://8.8.8.8", {}, {}, seconds=5)
