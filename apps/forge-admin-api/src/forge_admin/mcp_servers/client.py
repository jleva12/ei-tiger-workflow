"""Talking to an MCP server over streamable HTTP: what checking one does."""

from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

# The most pages of tools read; a server that pages forever stops here.
MAX_PAGES = 20


class McpConnectionError(Exception):
    """The server couldn't be reached, refused, or didn't speak MCP."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        #: The HTTP status the server answered with, when it did.
        self.status = status


@dataclass(frozen=True)
class Listing:
    """What a server said it is, and its tools."""

    tools: list[dict[str, Any]]
    server_info: dict[str, Any] | None = field(default=None)


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


_HINTS = {
    401: ": it refused the credentials",
    403: ": the credentials may not use it",
    404: ": there's no MCP endpoint at that URL",
    405: ": it doesn't take streamable HTTP at that URL",
}


def _explained(error: BaseException, status: int | None) -> McpConnectionError:
    """
    The likeliest reason among what an MCP session's task group raised.

    :param status: The HTTP error status the server last answered with: the
        SDK reports it only as an internal error.
    """
    if status is not None:
        return McpConnectionError(
            f"The server answered {status}{_HINTS.get(status, '')}", status=status
        )
    leaves = _leaves(error)
    for leaf in leaves:
        cause: BaseException | None = leaf
        while cause is not None:
            if isinstance(cause, httpx.TimeoutException):
                return McpConnectionError("The server didn't answer in time")
            if isinstance(cause, httpx.ConnectError):
                return McpConnectionError("The server can't be reached at that URL")
            cause = cause.__cause__
    first = leaves[0] if leaves else error
    return McpConnectionError(str(first) or type(first).__name__)


async def list_tools(url: str, *, headers: dict[str, str], seconds: float) -> Listing:
    """
    Connect to a server, and list its tools.

    :param url: Its MCP endpoint.
    :param headers: What to send with every request: its own headers and
        its auth method's.
    :param seconds: How long it has to answer each request.
    :return: Its tools, and what it said it is.
    :raises McpConnectionError: It couldn't be reached, refused, or didn't
        speak MCP.
    """
    http = create_mcp_http_client(
        headers=headers, timeout=httpx.Timeout(seconds, read=seconds)
    )
    refused: list[int] = []

    async def record(response: httpx.Response) -> None:
        if response.status_code >= 400:
            refused.append(response.status_code)

    http.event_hooks["response"].append(record)
    try:
        async with (
            http,
            Client(
                streamable_http_client(url, http_client=http, terminate_on_close=True),
                read_timeout_seconds=seconds,
                raise_exceptions=True,
            ) as client,
        ):
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                page = await client.list_tools(cursor=cursor)
                tools.extend(
                    {
                        "name": tool.name,
                        "title": getattr(tool, "title", None),
                        "description": tool.description or "",
                    }
                    for tool in page.tools
                )
                cursor = page.next_cursor
                if not cursor:
                    break
            info = client.server_info
            return Listing(
                tools=tools,
                server_info={"name": info.name, "version": info.version}
                if info
                else None,
            )
    except McpConnectionError:
        raise
    except Exception as error:
        raise _explained(error, refused[0] if refused else None) from None
