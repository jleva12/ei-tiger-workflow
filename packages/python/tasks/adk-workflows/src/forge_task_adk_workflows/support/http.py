"""HTTP nodes' requests: where one may go, and reading what comes back.

A node's URL is written by a member of the organization, and its data may come
from anywhere (an agent's answer, another system's response), so a request
must not reach the worker's own network: private, loopback, link-local (cloud
metadata), multicast and reserved addresses are refused, unless the host is
one a deployment allows (``HYBRID_ADK_WORKFLOWS__HTTP_ALLOWED_HOSTS``) or it
allows private networks (``HYBRID_ADK_WORKFLOWS__HTTP_ALLOW_PRIVATE``, for
local runs).

The check resolves the host name and refuses it if any address it resolves
to is refused. Redirects are followed the same way, one hop at a time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge_task_adk_workflows.support.errors import StepFailed

#: The most redirects one request follows.
MAX_REDIRECTS = 5
#: Statuses worth trying again.
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _refused(address: str) -> bool:
    """Whether an IP address is one a request may not reach (an IPv4-mapped IPv6 address as its IPv4)."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


async def check_url(url: str, *, allow_private: bool, allowed_hosts: list[str]) -> str:
    """
    :param url: Where the request goes.
    :param allow_private: Whether private networks may be reached.
    :param allowed_hosts: Hosts always allowed, unresolved.
    :return: The URL, when a request may go there.
    :raises StepFailed: It isn't http(s), has no host, its host can't be
        found, or any address its host resolves to is refused.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise StepFailed(f"Its URL must start with http:// or https://, not {url[:80]!r}")
    host = parts.hostname
    if not host:
        raise StepFailed(f"Its URL has no host: {url[:80]!r}")
    if allow_private or host.lower() in {h.lower() for h in allowed_hosts}:
        return url
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise StepFailed(f"{host} can't be found: {exc.strerror or exc}") from exc
        addresses = sorted({str(info[4][0]) for info in infos})
    if any(_refused(address) for address in addresses):
        raise StepFailed(
            f"{host} is on a private network, which HTTP steps can't reach "
            "(a deployment can allow it with HYBRID_ADK_WORKFLOWS__HTTP_ALLOWED_HOSTS)"
        )
    return url


async def read_body(response: httpx.Response, limit: int) -> bytes:
    """
    :return: A streamed response's body.
    :raises StepFailed: It's over ``limit`` bytes.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise StepFailed(f"Its response is over {limit:,} bytes, the most an HTTP step reads")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_body(raw: bytes, content_type: str) -> Any:
    """:return: A body as JSON when it's JSON (by its type, or its first character), else as text."""
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text
