"""Where a request came from and how its caller signed in, for the audit of
what it starts (an ADK workflow run's provenance).

The security dependency records it once per request (``security._identify``):
the caller's address and user agent, the request's ID (``X-Request-ID``, as its
logs have it), how the caller proved who they are, and who acts for them when
it isn't them (the assistant). The assistant's tools call this API in the
process, as the person, while it serves the person's own request: those calls
keep that request's origin, not the process's.
"""

from contextvars import ContextVar
from dataclasses import dataclass, replace

from fastapi import Request

#: How a caller proved who they are: a sign-in token, an organization's API
#: key, the local development user (no credentials), or nothing.
JWT, API_KEY, LOCAL, ANONYMOUS = "jwt", "api_key", "local", "anonymous"


@dataclass(frozen=True)
class RequestOrigin:
    """Where a request came from, and how its caller signed in."""

    client_ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    #: ``jwt``, ``api_key``, ``local`` or ``anonymous``.
    auth_method: str = ANONYMOUS
    #: Who acts for the caller, when it isn't them: ``assistant``.
    actor: str | None = None


_origin: ContextVar[RequestOrigin | None] = ContextVar(
    "forge_admin_request_origin", default=None
)


def origin_of(
    request: Request, *, auth_method: str, actor: str | None = None
) -> RequestOrigin:
    """
    :return: Where the request came from: its address (as the server has it,
        behind its trusted proxies), user agent and ID; and how its caller
        signed in. A call the assistant makes keeps the address, user agent
        and ID of the person's request it serves.
    """
    outer = _origin.get()
    if actor is not None and outer is not None:
        return replace(outer, auth_method=auth_method, actor=actor)
    # Read leniently: whatever the server's scope left out is unknown.
    client = getattr(request, "client", None)
    headers = getattr(request, "headers", None)
    return RequestOrigin(
        client_ip=client.host if client else None,
        user_agent=headers.get("user-agent") if headers is not None else None,
        request_id=getattr(request.state, "request_id", None),
        auth_method=auth_method,
        actor=actor,
    )


def set_origin(origin: RequestOrigin) -> None:
    """Record where this request came from, for the rest of it (and the
    tasks it starts, which copy it)."""
    _origin.set(origin)


def current_origin() -> RequestOrigin:
    """:return: Where the request being served came from; nowhere known
    outside one."""
    return _origin.get() or RequestOrigin()
