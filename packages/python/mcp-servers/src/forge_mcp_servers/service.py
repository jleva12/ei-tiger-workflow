"""What the API and agents do with an MCP server: work out what to send it,
check it, and connect it, through its auth method.

Methods change the server's row (its grant, what a check found) and leave
committing to the caller. A row is anything with a server's fields
(:class:`ServerRow`): the admin API's ``McpServer``, or
:class:`~forge_mcp_servers.store.StoredServer`.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx2 as httpx

from forge_mcp_servers.auth import (
    AuthContext,
    AuthError,
    AuthMethod,
    AuthMethods,
    Authorization,
    Connection,
    InteractiveAuthMethod,
    NotConnected,
)
from forge_mcp_servers.client import Listing, McpConnectionError, list_tools
from forge_mcp_servers.registry import AUTH_METHODS
from forge_mcp_servers.secrets import SecretBox, SecretsError

Lister = Callable[..., Awaitable[Listing]]


class ServerRow(Protocol):
    """A server as it's kept: where it is, how it's signed in to, and what a check found."""

    id: str
    organization_id: str
    url: str
    #: Sent as they are: ``[{"name": …, "value": …}]``.
    headers: list[dict[str, str]]
    timeout_seconds: float
    auth_kind: str
    auth_settings: dict[str, Any]
    #: Ciphertext, or None for none.
    auth_secrets: str | None
    auth_grant: str | None
    status: str
    last_error: str | None
    tools: list[dict[str, Any]]
    server_info: dict[str, Any] | None
    checked_at: datetime | None


def utc_now() -> datetime:
    """:return: The time now in UTC, without a timezone, as MySQL keeps it."""
    return datetime.now(UTC).replace(tzinfo=None)


#: What a check finds.
UNCHECKED, OK, ERROR, NEEDS_AUTH = "unchecked", "ok", "error", "needs_auth"


class McpServers:
    """
    :param box: Encrypts the servers' secrets and grants.
    :param timeout: Seconds an authorization server has to answer.
    :param methods: The auth methods there are.
    :param lister: Lists a server's tools (:func:`list_tools`); tests stand
        one in.
    :param transport: For the auth methods' own requests; tests stand one in.
    """

    def __init__(
        self,
        box: SecretBox,
        *,
        timeout: float = 15.0,
        methods: AuthMethods = AUTH_METHODS,
        lister: Lister = list_tools,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.box = box
        self.timeout = timeout
        self.methods = methods
        self.lister = lister
        self.transport = transport

    def http(self) -> httpx.AsyncClient:
        """:return: A client for an auth method's requests; close it after."""
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    def method(self, server: ServerRow) -> AuthMethod[Any, Any]:
        return self.methods.get(server.auth_kind)

    def secrets(self, server: ServerRow) -> dict[str, Any]:
        """:raises SecretsError: They can't be decrypted."""
        return self.box.open(server.auth_secrets)

    def grant(self, server: ServerRow) -> dict[str, Any]:
        """:return: The grant; empty when it can't be decrypted."""
        try:
            return self.box.open(server.auth_grant)
        except SecretsError:
            return {}

    def set_grant(self, server: ServerRow, grant: dict[str, Any] | None) -> None:
        server.auth_grant = self.box.seal(grant)

    def context(
        self, server: ServerRow, http: httpx.AsyncClient
    ) -> AuthContext[Any, Any]:
        """
        :raises SecretsError: Its secrets can't be decrypted.
        :raises ValueError: Its settings or secrets no longer fit its method.
        """
        method = self.method(server)
        return AuthContext(
            server_url=server.url,
            settings=method.settings(server.auth_settings),
            secrets=method.secrets(self.secrets(server)),
            grant=self.grant(server),
            http=http,
        )

    def connection(self, server: ServerRow) -> Connection | None:
        try:
            return self.method(server).connection(self.grant(server))
        except ValueError:
            return None

    async def headers(self, server: ServerRow) -> dict[str, str]:
        """
        What to send the server: its own headers, then its auth method's.
        A grant the method renewed is kept on the row.

        :raises NotConnected: A person has to connect it first.
        :raises AuthError: Its credentials couldn't be had.
        :raises SecretsError: Its secrets can't be decrypted.
        """
        headers = {row["name"]: row["value"] for row in server.headers}
        async with self.http() as http:
            try:
                context = self.context(server, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            credentials = await self.method(server).credentials(context)
        if credentials.grant is not None:
            self.set_grant(server, credentials.grant)
        return headers | credentials.headers

    async def check(self, server: ServerRow) -> None:
        """
        Connect to the server and list its tools, keeping what was found on
        its row: ``ok`` with the tools, ``needs_auth`` when it has to be
        connected (or refused the credentials), ``error`` otherwise.
        """
        server.checked_at = utc_now()
        try:
            headers = await self.headers(server)
            listing = await self.lister(
                server.url, headers=headers, seconds=server.timeout_seconds
            )
        except NotConnected as error:
            server.status, server.last_error = NEEDS_AUTH, str(error)
            return
        except (AuthError, SecretsError) as error:
            server.status, server.last_error = ERROR, str(error)
            return
        except McpConnectionError as error:
            interactive = self.method(server).interactive
            server.status = NEEDS_AUTH if interactive and error.status == 401 else ERROR
            server.last_error = str(error)
            return
        server.status, server.last_error = OK, None
        server.tools = listing.tools
        server.server_info = listing.server_info

    def interactive(self, server: ServerRow) -> InteractiveAuthMethod[Any, Any]:
        """:raises ValueError: Its method isn't one a person connects."""
        method = self.method(server)
        if not isinstance(method, InteractiveAuthMethod):
            raise ValueError(f"{method.label} isn't connected by signing in")
        return method

    async def begin(
        self, server: ServerRow, *, redirect_uri: str, state: str
    ) -> Authorization:
        """
        :raises ValueError: Its method isn't one a person connects.
        :raises AuthError: Its authorization server can't be found or used.
        """
        method = self.interactive(server)
        async with self.http() as http:
            try:
                context = self.context(server, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            return await method.begin(context, redirect_uri=redirect_uri, state=state)

    async def complete(
        self,
        server: ServerRow,
        *,
        pending: dict[str, Any],
        code: str,
        redirect_uri: str,
        user: str,
    ) -> None:
        """
        Exchange what the person came back with, and keep the grant.

        :raises AuthError: It wasn't exchanged.
        """
        method = self.interactive(server)
        async with self.http() as http:
            try:
                context = self.context(server, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            grant = await method.complete(
                context, pending=pending, code=code, redirect_uri=redirect_uri
            )
        self.set_grant(server, grant | {"connected_by": user})
        server.status, server.last_error = UNCHECKED, None


def epoch(value: float | None) -> datetime | None:
    """:return: Epoch seconds as a UTC time."""
    return None if value is None else datetime.fromtimestamp(value, UTC)
