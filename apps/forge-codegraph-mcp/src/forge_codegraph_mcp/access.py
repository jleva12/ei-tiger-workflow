"""Who may call the MCP endpoint, and which repositories they read.

Every caller sends a Forge credential as its bearer token: an organization's
API key (``fk_…``), or a Forge token minted for an organization
(``forge-admin-token --organization``, whose ``org_id`` claim names it). The
server doesn't judge it itself: it passes it on, as it came, to the admin API
(``GET <mcp.auth.admin_url>/code-graph/access``), which answers whose it is
and the code repositories of its organization, and needs
``repositories:read`` there. That answer is kept for the same credential for
``mcp.auth.cache_seconds``, so a key deleted, a member removed or a
repository added shows within that long.

Each tool then reads only those repositories (``forge_codegraph``'s
:class:`RepositoryScope`, which the admin API's assistant uses too): the
graph has one repository per GitHub URL, whose ID follows from the URL, so an
organization reads the graphs of its repositories' URLs and nothing else,
cross-repository links included. The same answer carries how the
organization's repositories connect, as people drew them on its system
design knowledge bases' maps (``repository_connections``), each of those
knowledge bases' whole design, with the databases, queues and external
systems the repositories use and the groups they're in (``system_design``),
and who keeps its cross-repository links in the graph (``kb:<id>``): a graph
shared with another organization holds that one's links too, which are left
out.

- A credential the admin API refuses: 401, as for a missing one.
- One that names no organization, or lacks ``repositories:read`` in it: 403
  (``insufficient_scope``).
- The admin API can't be reached or fails: 503, and nothing is kept.
"""

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from forge_codegraph import READ, RepositoryScope, repository_id
from forge_common.logging import get_logger
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from starlette.authentication import AuthenticationError
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from forge_codegraph_mcp.core.settings import McpAuthSettings

log = get_logger(__name__)

__all__ = ["READ", "ForgeAccessVerifier", "RepositoryScope", "current_scope", "repository_id"]

ACCESS_PATH = "/code-graph/access"


def current_scope() -> RepositoryScope:
    """
    The scope of the request being served.

    :raises PermissionError: No verified credential came with it, which the
        endpoint's auth never lets happen.
    """
    token = get_access_token()
    if token is None or READ not in token.scopes:
        raise PermissionError("no verified Forge credential")
    return RepositoryScope.from_claims(token.claims or {}, str(token.subject or token.client_id))


class AdminUnavailable(AuthenticationError):
    """The admin API couldn't say whose a credential is."""


def _unavailable(_conn: HTTPConnection, error: AuthenticationError) -> Response:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": str(error)},
        status_code=503,
        headers={"Retry-After": "5"},
    )


@dataclass
class _Kept:
    access: AccessToken
    until: float


class ForgeAccessVerifier(TokenVerifier):
    """
    Bearer Forge credentials, checked with the admin API; the answer is kept
    per credential (by its SHA-256) for ``cache_seconds``.

    :param settings: The endpoint's auth settings.
    :param transport: For tests: the HTTP transport to the admin API.
    """

    def __init__(
        self,
        settings: McpAuthSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(required_scopes=[READ])
        self._settings = settings
        headers = {"Accept": "application/json"}
        if settings.admin_api_key is not None:
            headers["X-API-Key"] = settings.admin_api_key.get_secret_value()
        self._http = httpx.AsyncClient(
            base_url=settings.admin_url.rstrip("/"),
            headers=headers,
            timeout=settings.timeout,
            transport=transport,
        )
        self._clock = clock
        self._kept: dict[str, _Kept] = {}
        self._asking: dict[str, asyncio.Future[AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        kept = self._kept.get(key)
        if kept is not None and kept.until > self._clock():
            return kept.access
        # Calls that arrive together with a new credential share one question.
        asking = self._asking.get(key)
        if asking is None:
            asking = asyncio.ensure_future(self._ask(key, token))
            self._asking[key] = asking
            asking.add_done_callback(lambda _: self._asking.pop(key, None))
        return await asyncio.shield(asking)

    async def _ask(self, key: str, token: str) -> AccessToken | None:
        try:
            response = await self._http.get(
                ACCESS_PATH, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as error:
            log.warning("mcp.auth.admin_unreachable", error=type(error).__name__)
            raise AdminUnavailable("The Forge admin API can't be reached") from None
        if response.status_code == 401:
            log.info("mcp.auth.refused", detail=_detail(response))
            return None
        if response.status_code in (403, 404):
            # Whose it is was established, but it reads nothing: kept like
            # an answer, so a client retrying doesn't ask again each time.
            log.info("mcp.auth.no_access", status=response.status_code, detail=_detail(response))
            access = AccessToken(token=token, client_id="forbidden", scopes=[])
        elif response.status_code == 200:
            try:
                access = self._granted(token, response.json())
            except (KeyError, TypeError, ValueError):
                log.warning("mcp.auth.admin_answer_unreadable")
                raise AdminUnavailable("The Forge admin API answered unexpectedly") from None
        else:
            log.warning("mcp.auth.admin_failed", status=response.status_code)
            raise AdminUnavailable("The Forge admin API couldn't check the credential")
        self._keep(key, access)
        return access

    def _granted(self, token: str, body: dict[str, Any]) -> AccessToken:
        scope = RepositoryScope.from_access(body)
        return AccessToken(
            token=token,
            client_id=scope.subject,
            subject=scope.subject,
            scopes=[READ],
            claims=scope.claims(),
        )

    def _keep(self, key: str, access: AccessToken) -> None:
        if self._settings.cache_seconds <= 0:
            return
        now = self._clock()
        if len(self._kept) >= self._settings.cache_size:
            self._kept = {k: v for k, v in self._kept.items() if v.until > now}
            while len(self._kept) >= self._settings.cache_size:
                self._kept.pop(next(iter(self._kept)))
        self._kept[key] = _Kept(access, now + self._settings.cache_seconds)

    def get_middleware(self) -> list[Any]:
        # FastMCP's, answering 503 when the admin API can't check a credential.
        return [
            Middleware(
                AuthenticationMiddleware,  # type: ignore[arg-type]
                backend=BearerAuthBackend(self),
                on_error=_unavailable,
            ),
            Middleware(AuthContextMiddleware),  # type: ignore[arg-type]
        ]

    async def aclose(self) -> None:
        await self._http.aclose()


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail") or "")[:300]
    except ValueError:
        return ""
