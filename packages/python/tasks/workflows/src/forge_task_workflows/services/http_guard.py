"""Where an HTTP step may send a request.

A workflow's URL is written by a member of the organization, and its data may come from
anywhere (an event, an agent's answer), so a request must not reach the
worker's own network: private, loopback, link-local (cloud metadata),
multicast and reserved addresses are refused, unless the host is one a
deployment allows (``HYBRID_WORKFLOWS__HTTP_ALLOWED_HOSTS``) or it allows
private networks (``HYBRID_WORKFLOWS__HTTP_ALLOW_PRIVATE``, for local runs).

The check resolves the host name and refuses it if any address it resolves
to is refused. Redirects are followed the same way, one hop at a time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from forge_task_workflows.errors import StepFailed


def _refused(address: str) -> bool:
    """
    Determines if the given address is refused based on its properties.

    This function evaluates an IP address to classify it as a refused address based
    on specific criteria such as whether the address is private, loopback, link-local,
    multicast, reserved, or unspecified. IPv6-mapped IPv4 addresses are converted
    to their IPv4 counterparts before evaluation.

    :param address: The IP address to evaluate.
    :type address: str
    :return: True if the address is considered refused, False otherwise.
    :rtype: bool
    """
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


async def check_url(url: str, *, allow_private: bool, allowed_hosts: list[str]) -> str:
    """
    Checks the validity of a URL, ensuring it adheres to required protocols, contains a valid
    host, and optionally verifies whether the host is allowed or resides within a private network.

    The function scrutinizes the components of the given URL using URL splitting and DNS
    resolution to identify accessible and secure hosts. It restricts private network access unless
    explicitly allowed and ensures the host exists within the approved list if provided.

    :param url: The URL to validate.
    :type url: str
    :param allow_private: Boolean flag indicating whether private network access is permitted.
    :type allow_private: bool
    :param allowed_hosts: A list of allowed hostnames; validation skips DNS checks for these hosts.
    :type allowed_hosts: list[str]
    :return: The original URL if all validation conditions are met.
    :rtype: str
    :raises StepFailed: If the URL does not meet the validation requirements, such as failed
        DNS resolution, invalid scheme, missing host, or restricted access to private networks.
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
            "(a deployment can allow it with HYBRID_WORKFLOWS__HTTP_ALLOWED_HOSTS)"
        )
    return url
