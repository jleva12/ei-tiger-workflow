"""Servers kept in the admin's table, read without its models: what a
connection sends, and a renewed grant kept on the row."""

import json
from typing import Any

import pytest
import sqlalchemy as sa
from google.adk.tools.mcp_tool import McpToolset
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from forge_mcp_servers import (
    McpServerGone,
    McpServers,
    SecretBox,
    SqlServerStore,
    mcp_toolset,
)
from forge_mcp_servers.auth import (
    AuthContext,
    AuthMethod,
    AuthMethods,
    Credentials,
    NoFields,
)
from forge_mcp_servers.store import SERVERS

KEY = "k" * 40


class Rotating(BaseModel):
    token: str


class RotatingAuth(AuthMethod[NoFields, Rotating]):
    """Sends its grant's token, and renews it on every connection."""

    kind = "rotating"
    label = "Rotating"
    description = "A token renewed each time."
    secrets_model = Rotating

    async def credentials(
        self, context: AuthContext[NoFields, Rotating]
    ) -> Credentials:
        count = int(context.grant.get("count", 0)) + 1
        token = f"{context.secrets.token}-{count}"
        return Credentials({"Authorization": f"Bearer {token}"}, {"count": count})


@pytest.fixture
async def engine() -> Any:
    made = create_async_engine("sqlite+aiosqlite://")
    async with made.begin() as conn:
        await conn.run_sync(SERVERS.metadata.create_all)
    yield made
    await made.dispose()


async def add(engine: AsyncEngine, box: SecretBox, **row: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(SERVERS).values(
                id="srv_1",
                organization_id="org_1",
                url="https://mcp.example.com/mcp",
                headers=json.dumps([{"name": "X-Team", "value": "support"}]),
                timeout_seconds=12.0,
                auth_kind="rotating",
                auth_settings="{}",
                auth_secrets=box.seal({"token": "t"}),
                auth_grant=None,
                **row,
            )
        )


async def test_a_connection_sends_its_headers_and_keeps_the_renewed_grant(engine):
    box = SecretBox(KEY)
    servers = McpServers(box, methods=AuthMethods([RotatingAuth()]))
    store = SqlServerStore(engine)
    await add(engine, box)

    async with store.open("srv_1") as server:
        assert server is not None
        assert (server.url, server.timeout_seconds) == (
            "https://mcp.example.com/mcp",
            12.0,
        )
        first = await servers.headers(server)
    assert first == {"X-Team": "support", "Authorization": "Bearer t-1"}
    async with store.open("srv_1") as server:
        assert server is not None
        assert await servers.headers(server) == {
            "X-Team": "support",
            "Authorization": "Bearer t-2",
        }
    async with engine.connect() as conn:
        grant = (await conn.execute(sa.select(SERVERS.c.auth_grant))).scalar_one()
    assert box.open(grant) == {"count": 2}

    async with store.open("srv_nobody") as missing:
        assert missing is None


async def test_a_toolset_is_the_organizations_server_with_its_headers_each_time(engine):
    box = SecretBox(KEY)
    servers = McpServers(box, methods=AuthMethods([RotatingAuth()]))
    store = SqlServerStore(engine)
    await add(engine, box)

    toolset = await mcp_toolset(
        servers,
        store,
        organization_id="org_1",
        config={"server": "srv_1", "tools": "search, fetch", "confirm": True},
    )
    assert isinstance(toolset, McpToolset)
    assert toolset._header_provider is not None
    sent = await toolset._header_provider(None)  # type: ignore[arg-type]
    assert sent["Authorization"] == "Bearer t-1"

    with pytest.raises(McpServerGone):
        await mcp_toolset(
            servers, store, organization_id="org_2", config={"server": "srv_1"}
        )
