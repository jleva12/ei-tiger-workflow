"""OAuth for MCP servers, as the MCP authorization spec describes it.

Finding a server's authorization server (:func:`discover`): the server's
``401`` names its protected resource metadata (RFC 9728) in
``WWW-Authenticate``, or it's at the well-known address; that metadata
names the authorization server, whose own metadata (RFC 8414, or OpenID
Connect discovery) gives its endpoints. A server from before that spec has
its authorization server at its own origin, at ``/authorize``, ``/token``
and ``/register``.

Two methods use it:

- :class:`OAuthAuthorizationCode` (``oauth``): a person signs in. Without a
  client ID, Forge registers itself with the authorization server (dynamic
  client registration, RFC 7591). The code is exchanged with PKCE (S256),
  every request names the server as its ``resource`` (RFC 8707), and the
  access token is refreshed as it runs out.
- :class:`OAuthClientCredentials` (``oauth_client_credentials``): Forge signs
  in as itself with a client ID and secret, nobody involved.

Errors never carry a token endpoint's response beyond its ``error`` and
``error_description``: the rest can hold credentials.
"""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field

from forge_mcp_servers.auth import (
    AuthContext,
    AuthError,
    AuthMethod,
    Authorization,
    Connection,
    Credentials,
    InteractiveAuthMethod,
    NotConnected,
)

#: The name Forge registers itself with.
CLIENT_NAME = "Forge"
#: The MCP protocol version the discovery probe names.
PROTOCOL_VERSION = "2025-06-18"
# A token this close to running out is refreshed first.
EXPIRY_MARGIN = 60.0
SCOPES_PATTERN = r"^[\x21\x23-\x5b\x5d-\x7e ]*$"


@dataclass(frozen=True)
class Discovery:
    """Where a server's authorization is, as :func:`discover` found it."""

    #: What tokens are asked for (RFC 8707): the server, as it names itself.
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    #: The scopes the server asks for, space-separated; empty when it doesn't say.
    scope: str


def canonical(url: str) -> str:
    """:return: A URL as a resource is named: lowercase scheme and host, no fragment."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _well_known(url: str, name: str) -> list[str]:
    """The addresses of a well-known document for a URL: with its path
    after the well-known name, then at the root."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    root = f"{_origin(url)}/.well-known/{name}"
    return [f"{root}{path}", root] if path else [root]


def _challenge(header: str) -> dict[str, str]:
    """The parameters of a ``WWW-Authenticate: Bearer …`` challenge."""
    found = re.findall(
        r'([A-Za-z_][A-Za-z0-9_-]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', header
    )
    return {name.lower(): quoted or bare for name, quoted, bare in found}


async def _json(http: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _probe(http: httpx.AsyncClient, server_url: str) -> dict[str, str]:
    """What the server's 401 challenge names: its resource metadata, scope."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    try:
        # Streamed, and never read: an open server may answer with an SSE
        # stream that doesn't end.
        async with http.stream(
            "POST", server_url, json=body, headers=headers
        ) as response:
            if response.status_code != 401:
                return {}
            return _challenge(response.headers.get("WWW-Authenticate", ""))
    except httpx.HTTPError:
        return {}


async def discover(
    http: httpx.AsyncClient, server_url: str, *, authorization_server: str = ""
) -> Discovery:
    """
    Find a server's authorization server and its endpoints.

    :param http: For the requests.
    :param server_url: The server's MCP endpoint.
    :param authorization_server: The authorization server to use instead of
        the one the server names; empty to find it.
    :return: What was found.
    :raises AuthError: The authorization server has no metadata and the
        server names none.
    """
    challenge = await _probe(http, server_url)
    resource_metadata = None
    for url in filter(
        None,
        [
            challenge.get("resource_metadata"),
            *_well_known(server_url, "oauth-protected-resource"),
        ],
    ):
        resource_metadata = await _json(http, url)
        if resource_metadata:
            break
    resource_metadata = resource_metadata or {}

    servers = resource_metadata.get("authorization_servers")
    issuer = authorization_server or (
        servers[0]
        if isinstance(servers, list) and servers and isinstance(servers[0], str)
        else ""
    )
    # Before protected resource metadata, the server's origin was its
    # authorization server.
    legacy = not issuer
    issuer = issuer or _origin(server_url)

    metadata: dict[str, Any] | None = None
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    candidates = (
        [
            f"{_origin(issuer)}/.well-known/oauth-authorization-server{path}",
            f"{_origin(issuer)}/.well-known/openid-configuration{path}",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ]
        if path
        else [
            f"{_origin(issuer)}/.well-known/oauth-authorization-server",
            f"{_origin(issuer)}/.well-known/openid-configuration",
        ]
    )
    for url in candidates:
        metadata = await _json(http, url)
        if metadata:
            break

    if metadata:
        authorize = metadata.get("authorization_endpoint")
        token = metadata.get("token_endpoint")
        if not isinstance(token, str) or not token:
            raise AuthError(
                f"The authorization server {issuer} names no token endpoint"
            )
        registration = metadata.get("registration_endpoint")
    elif legacy:
        base = _origin(server_url)
        authorize, token, registration = (
            f"{base}/authorize",
            f"{base}/token",
            f"{base}/register",
        )
    else:
        raise AuthError(
            f"The authorization server {issuer} publishes no OAuth metadata"
        )

    scopes = resource_metadata.get("scopes_supported")
    scope = challenge.get("scope") or (
        " ".join(s for s in scopes if isinstance(s, str))
        if isinstance(scopes, list)
        else ""
    )
    resource = resource_metadata.get("resource")
    return Discovery(
        resource=resource
        if isinstance(resource, str) and resource
        else canonical(server_url),
        issuer=issuer,
        authorization_endpoint=authorize if isinstance(authorize, str) else "",
        token_endpoint=token,
        registration_endpoint=registration
        if isinstance(registration, str) and registration
        else None,
        scope=scope,
    )


def _failure(what: str, response: httpx.Response) -> AuthError:
    """An error naming why an endpoint refused, without what else it said."""
    reason = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        reason = body["error"]
        if isinstance(body.get("error_description"), str):
            reason += f": {body['error_description']}"
    return AuthError(
        f"{what} answered {response.status_code}{f' ({reason})' if reason else ''}"
    )


async def _token_request(
    http: httpx.AsyncClient,
    token_endpoint: str,
    client: dict[str, Any],
    data: dict[str, str],
) -> dict[str, Any]:
    """Ask a token endpoint for tokens, as the client authenticates there.

    :raises NotConnected: It refused the grant (``invalid_grant``).
    :raises AuthError: It refused otherwise, or didn't answer.
    """
    data = dict(data)
    auth = None
    secret = client.get("client_secret") or ""
    if secret and client.get("auth_method") == "client_secret_post":
        data |= {"client_id": client["client_id"], "client_secret": secret}
    elif secret:
        # Form-encoded first (RFC 6749, section 2.3.1).
        auth = httpx.BasicAuth(
            quote(client["client_id"], safe=""), quote(secret, safe="")
        )
    else:
        data["client_id"] = client["client_id"]
    try:
        response = await http.post(
            token_endpoint,
            data=data,
            auth=auth,  # type: ignore[arg-type]  # None: no client authentication
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise AuthError(
            f"The token endpoint didn't answer: {type(error).__name__}"
        ) from None
    if response.status_code != 200:
        failure = _failure("The token endpoint", response)
        try:
            refused = response.json().get("error") == "invalid_grant"
        except (ValueError, AttributeError):
            refused = False
        if refused:
            raise NotConnected(f"{failure}; connect the server again") from None
        raise failure
    try:
        body = response.json()
    except ValueError:
        raise AuthError("The token endpoint didn't answer with JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise AuthError("The token endpoint's answer has no access token")
    return body


def _tokens(
    body: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    expires_in = body.get("expires_in")
    return {
        "access_token": body["access_token"],
        # A refresh that doesn't rotate the refresh token keeps the old one.
        "refresh_token": body.get("refresh_token")
        or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + float(expires_in)
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else None,
        "scope": body.get("scope")
        if isinstance(body.get("scope"), str)
        else (previous or {}).get("scope"),
    }


def _fresh(tokens: dict[str, Any]) -> bool:
    expires_at = tokens.get("expires_at")
    return bool(tokens.get("access_token")) and (
        expires_at is None or expires_at - EXPIRY_MARGIN > time.time()
    )


def _bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ------------------------------------------------------- authorization code


class OAuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        default="",
        max_length=512,
        title="Client ID",
        description=(
            "A client registered for Forge. Empty: Forge registers itself, "
            "if the server allows it."
        ),
        json_schema_extra={"placeholder": "Registered automatically"},
    )
    scopes: str = Field(
        default="",
        max_length=2048,
        pattern=SCOPES_PATTERN,
        title="Scopes",
        description="Space-separated. Empty: the ones the server asks for.",
        json_schema_extra={"placeholder": "As the server asks"},
    )
    authorization_server: str = Field(
        default="",
        max_length=2048,
        pattern=r"^(https?://\S+)?$",
        title="Authorization server",
        description=(
            "Its issuer URL, when the server doesn't name it. "
            "Empty: found from the server."
        ),
        json_schema_extra={"placeholder": "Found from the server"},
    )


class OAuthSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret: str = Field(
        default="",
        max_length=8192,
        title="Client secret",
        description="With a client ID, when the client has one. Kept encrypted.",
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthAuthorizationCode(InteractiveAuthMethod[OAuthSettings, OAuthSecrets]):
    kind = "oauth"
    label = "OAuth sign-in"
    description = (
        "Someone signs in to the server's authorization server once; Forge keeps "
        "the tokens, refreshes them and uses them for every agent."
    )
    settings_model = OAuthSettings
    secrets_model = OAuthSecrets

    async def begin(
        self,
        context: AuthContext[OAuthSettings, OAuthSecrets],
        *,
        redirect_uri: str,
        state: str,
    ) -> Authorization:
        settings = context.settings
        found = await discover(
            context.http,
            context.server_url,
            authorization_server=settings.authorization_server,
        )
        if not found.authorization_endpoint:
            raise AuthError(
                f"The authorization server {found.issuer} names no authorization endpoint"
            )
        scope = settings.scopes or found.scope
        client = await self._client(context, found, redirect_uri, scope)
        verifier, challenge = _pkce()
        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": found.resource,
        }
        if scope:
            query["scope"] = scope
        separator = "&" if urlsplit(found.authorization_endpoint).query else "?"
        return Authorization(
            url=f"{found.authorization_endpoint}{separator}{urlencode(query)}",
            pending={
                "verifier": verifier,
                "client": client,
                "token_endpoint": found.token_endpoint,
                "issuer": found.issuer,
                "resource": found.resource,
                "scope": scope,
            },
        )

    async def _client(
        self,
        context: AuthContext[OAuthSettings, OAuthSecrets],
        found: Discovery,
        redirect_uri: str,
        scope: str,
    ) -> dict[str, Any]:
        """The client Forge signs in as: the one set up, the one it registered
        before (for the same server and address), or a new registration."""
        if context.settings.client_id:
            return {
                "client_id": context.settings.client_id,
                "client_secret": context.secrets.client_secret,
                "auth_method": "client_secret_basic",
            }
        kept = context.grant.get("client") or {}
        if (
            kept.get("registered")
            and kept.get("issuer") == found.issuer
            and kept.get("redirect_uri") == redirect_uri
        ):
            return kept
        if not found.registration_endpoint:
            raise AuthError(
                f"The authorization server {found.issuer} doesn't let clients "
                "register themselves: enter a client ID registered for Forge"
            )
        registration: dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if scope:
            registration["scope"] = scope
        try:
            response = await context.http.post(
                found.registration_endpoint, json=registration
            )
        except httpx.HTTPError as error:
            raise AuthError(
                f"The registration endpoint didn't answer: {type(error).__name__}"
            ) from None
        if response.status_code not in (200, 201):
            raise _failure("The registration endpoint", response)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("client_id"), str):
            raise AuthError("The registration endpoint's answer has no client ID")
        return {
            "client_id": body["client_id"],
            "client_secret": body.get("client_secret") or "",
            "auth_method": body.get("token_endpoint_auth_method") or "none",
            "registered": True,
            "issuer": found.issuer,
            "redirect_uri": redirect_uri,
        }

    async def complete(
        self,
        context: AuthContext[OAuthSettings, OAuthSecrets],
        *,
        pending: dict[str, Any],
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        body = await _token_request(
            context.http,
            pending["token_endpoint"],
            pending["client"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": pending["verifier"],
                "resource": pending["resource"],
            },
        )
        return {
            "client": pending["client"],
            "token_endpoint": pending["token_endpoint"],
            "issuer": pending["issuer"],
            "resource": pending["resource"],
            "tokens": _tokens(body)
            | {"scope": body.get("scope") or pending.get("scope") or None},
            "connected_at": time.time(),
        }

    async def credentials(
        self, context: AuthContext[OAuthSettings, OAuthSecrets]
    ) -> Credentials:
        grant = context.grant
        tokens = grant.get("tokens") or {}
        if not tokens.get("access_token"):
            raise NotConnected("Nobody has signed in to the server yet: connect it")
        if _fresh(tokens):
            return Credentials(_bearer(tokens))
        if not tokens.get("refresh_token"):
            raise NotConnected("The server's sign-in ran out: connect it again")
        body = await _token_request(
            context.http,
            grant["token_endpoint"],
            grant["client"],
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "resource": grant["resource"],
            },
        )
        refreshed = _tokens(body, tokens)
        return Credentials(_bearer(refreshed), grant | {"tokens": refreshed})

    def connection(self, grant: dict[str, Any]) -> Connection:
        tokens = grant.get("tokens") or {}
        return Connection(
            connected=bool(tokens.get("access_token")),
            connected_by=grant.get("connected_by"),
            connected_at=grant.get("connected_at"),
            expires_at=None
            if tokens.get("refresh_token")
            else tokens.get("expires_at"),
            scope=tokens.get("scope"),
        )


# ------------------------------------------------------- client credentials


class ClientCredentialsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        min_length=1,
        max_length=512,
        title="Client ID",
        description="The client Forge signs in as.",
    )
    scopes: str = Field(
        default="",
        max_length=2048,
        pattern=SCOPES_PATTERN,
        title="Scopes",
        description="Space-separated. Empty: the ones the server asks for.",
        json_schema_extra={"placeholder": "As the server asks"},
    )
    token_url: str = Field(
        default="",
        max_length=2048,
        pattern=r"^(https?://\S+)?$",
        title="Token URL",
        description="The authorization server's token endpoint. Empty: found from the server.",
        json_schema_extra={"placeholder": "Found from the server"},
    )


class ClientCredentialsSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret: str = Field(
        min_length=1,
        max_length=8192,
        title="Client secret",
        description="Kept encrypted; nobody can read it back.",
    )


class OAuthClientCredentials(
    AuthMethod[ClientCredentialsSettings, ClientCredentialsSecrets]
):
    kind = "oauth_client_credentials"
    label = "OAuth client credentials"
    description = (
        "Forge signs in as itself with a client ID and secret (machine to "
        "machine); the token is fetched and renewed as it runs out."
    )
    settings_model = ClientCredentialsSettings
    secrets_model = ClientCredentialsSecrets

    async def credentials(
        self, context: AuthContext[ClientCredentialsSettings, ClientCredentialsSecrets]
    ) -> Credentials:
        tokens = context.grant.get("tokens") or {}
        if _fresh(tokens):
            return Credentials(_bearer(tokens))
        settings = context.settings
        token_url, resource, scope = (
            settings.token_url,
            canonical(context.server_url),
            settings.scopes,
        )
        if not token_url:
            found = await discover(context.http, context.server_url)
            token_url, resource, scope = (
                found.token_endpoint,
                found.resource,
                scope or found.scope,
            )
        data = {"grant_type": "client_credentials", "resource": resource}
        if scope:
            data["scope"] = scope
        body = await _token_request(
            context.http,
            token_url,
            {
                "client_id": settings.client_id,
                "client_secret": context.secrets.client_secret,
            },
            data,
        )
        fetched = _tokens(body)
        # Never refreshed: a new one is asked for.
        fetched["refresh_token"] = None
        return Credentials(_bearer(fetched), {"tokens": fetched})
