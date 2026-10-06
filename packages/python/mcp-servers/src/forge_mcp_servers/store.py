"""The admin MySQL's ``mcp_servers`` rows, for a process without the admin
API's models (the async worker): read a server, and keep its renewed grant.

The admin API owns the table (its migrations); this reads the columns a
connection needs. JSON columns are kept as text (the admin's ``JSONText``).
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

#: The columns of the admin's table a connection reads, and the grant it keeps.
SERVERS = sa.Table(
    "mcp_servers",
    sa.MetaData(),
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("organization_id", sa.String(36)),
    sa.Column("url", sa.Text),
    sa.Column("headers", sa.Text),
    sa.Column("timeout_seconds", sa.Float),
    sa.Column("auth_kind", sa.String(64)),
    sa.Column("auth_settings", sa.Text),
    sa.Column("auth_secrets", sa.Text),
    sa.Column("auth_grant", sa.Text),
)


@dataclass
class StoredServer:
    """A server as :class:`SqlServerStore` reads it: a :class:`.service.ServerRow`."""

    id: str
    organization_id: str
    url: str
    headers: list[dict[str, str]] = field(default_factory=list)
    timeout_seconds: float = 30.0
    auth_kind: str = "none"
    auth_settings: dict[str, Any] = field(default_factory=dict)
    auth_secrets: str | None = None
    auth_grant: str | None = None
    # What a check found: the admin API's; kept here only as the row's shape.
    status: str = "unchecked"
    last_error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    server_info: dict[str, Any] | None = None
    checked_at: datetime | None = None


def _json(text: str | None, empty: Any) -> Any:
    if not text:
        return empty
    try:
        return json.loads(text)
    except ValueError:
        return empty


class SqlServerStore:
    """
    Servers in the admin's database, a row locked (``FOR UPDATE``) while
    it's open, so a grant is renewed by one process at a time.

    :param engine: The admin MySQL (or a test's SQLite, which doesn't lock).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def open(self, server_id: str) -> AsyncIterator[StoredServer | None]:
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(SERVERS)
                        .where(SERVERS.c.id == server_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                yield None
                return
            server = StoredServer(
                id=row["id"],
                organization_id=row["organization_id"],
                url=row["url"],
                headers=_json(row["headers"], []),
                timeout_seconds=float(row["timeout_seconds"] or 30.0),
                auth_kind=row["auth_kind"] or "none",
                auth_settings=_json(row["auth_settings"], {}),
                auth_secrets=row["auth_secrets"],
                auth_grant=row["auth_grant"],
            )
            kept = server.auth_grant
            yield server
            if server.auth_grant != kept:
                await conn.execute(
                    sa.update(SERVERS)
                    .where(SERVERS.c.id == server_id)
                    .values(auth_grant=server.auth_grant)
                )
