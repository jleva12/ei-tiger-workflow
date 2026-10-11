"""API credentials: how Forge signs in to an organization's HTTP APIs, for its
workflows' HTTP steps and its agents' HTTP and OpenAPI tools.

A credential is an organization's: an auth method with its settings (kept in
the clear and shown), its secrets (encrypted, write-only) and what it keeps
between requests (an OAuth grant, encrypted), as MCP servers have them
(:mod:`.auth`). Unlike a server, it has no address of its own: it's sent to
whatever a step or tool calls, so it names the URLs it may be sent to
(:func:`sends_to`), and a request anywhere else is refused. A workflow can't
hand the organization's key to another host.

The methods (:data:`CREDENTIAL_METHODS`): an API key in a header or in the
query, a bearer token, basic auth, OAuth client credentials and OAuth
sign-in. They're the MCP servers' protocol with an API's settings: OAuth here
names its endpoints instead of discovering them from an MCP server.

:class:`ApiCredentials` works out what a request sends through a credential's
method; :func:`request_credentials` finds the credential in a
:class:`CredentialStore` (the admin's models, or :class:`SqlCredentialStore`
in the async worker), checks it's the organization's and may go where the
request goes, and keeps a renewed grant on its row.
"""

import base64
import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx2 as httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from forge_mcp_servers.auth import (
    AuthContext,
    AuthError,
    AuthMethod,
    AuthMethods,
    Authorization,
    Connection,
    Credentials,
    InteractiveAuthMethod,
    NotConnected,
)
from forge_mcp_servers.methods import ApiKeyAuth, ApiKeySecrets, BearerAuth, Secret
from forge_mcp_servers.oauth import (
    SCOPES_PATTERN,
    _bearer,
    _fresh,
    _pkce,
    _token_request,
    _tokens,
)
from forge_mcp_servers.secrets import SecretBox, SecretsError

#: What a refused grant tells the person.
SIGN_IN_AGAIN = "sign in again on the Credentials page"
#: An http(s) URL, as a setting.
HTTP_URL = r"^https?://\S+$"

# ----------------------------------------------------------------- where


def sends_to(url: str, prefixes: Sequence[str]) -> bool:
    """
    Whether a credential that may be sent to ``prefixes`` goes with a request
    to ``url``: one of them has its scheme, host and port, and its path is
    the prefix's or under it (``https://api.example.com/v1`` takes
    ``…/v1`` and ``…/v1/orders``, not ``…/v10``).

    :param url: Where the request goes.
    :param prefixes: Where the credential may be sent: URLs, each its origin
        and optionally a path.
    """
    target = _parts(url)
    if target is None:
        return False
    for prefix in prefixes:
        allowed = _parts(prefix)
        if allowed is None or allowed[:3] != target[:3]:
            continue
        base = allowed[3].rstrip("/")
        path = target[3] or "/"
        if not base or path == base or path.startswith(f"{base}/"):
            return True
    return False


def _parts(url: str) -> tuple[str, str, int, str] | None:
    # A URL's scheme, host, port and path; None for one that isn't http(s).
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return (
        scheme,
        parts.hostname.lower(),
        port or (443 if scheme == "https" else 80),
        _resolved(parts.path),
    )


def _resolved(path: str) -> str:
    # A path as the request reaches it: its dot segments resolved, as httpx
    # does before sending, with %2E read as the dot servers take it for. So
    # …/v1/../admin and …/v1/%2e%2e/admin aren't under …/v1.
    segments = re.sub("%2e", ".", path, flags=re.IGNORECASE).split("/")
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment in (".", ".."):
            if segment == ".." and len(out) > 1:
                out.pop()
            if last:
                out.append("")
            continue
        out.append(segment)
    return "/".join(out)


# --------------------------------------------------------------- methods


class HeaderKeyAuth(ApiKeyAuth):
    label = "API key in a header"
    description = "A key the API issued, sent in a header of your choosing."


class QueryKeySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter: str = Field(
        default="api_key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.~-]+$",
        title="Query parameter",
        description="The parameter of the URL the key goes in.",
        json_schema_extra={"placeholder": "api_key"},
    )


class QueryKeyAuth(AuthMethod[QueryKeySettings, ApiKeySecrets]):
    kind = "api_key_query"
    label = "API key in the query"
    description = "A key the API issued, sent as a query parameter of your choosing."
    settings_model = QueryKeySettings
    secrets_model = ApiKeySecrets

    async def credentials(
        self, context: AuthContext[QueryKeySettings, ApiKeySecrets]
    ) -> Credentials:
        return Credentials(params={context.settings.parameter: context.secrets.key})


class BasicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=1,
        max_length=512,
        title="Username",
        description="Who Forge signs in as.",
    )


class BasicSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(
        default="",
        max_length=8192,
        title="Password",
        description=(
            "Kept encrypted; nobody can read it back. Some APIs take a key as "
            "the username and no password."
        ),
    )


class BasicAuth(AuthMethod[BasicSettings, BasicSecrets]):
    kind = "basic"
    label = "Basic auth"
    description = "A username and password, sent in the Authorization header."
    settings_model = BasicSettings
    secrets_model = BasicSecrets

    async def credentials(
        self, context: AuthContext[BasicSettings, BasicSecrets]
    ) -> Credentials:
        pair = f"{context.settings.username}:{context.secrets.password}".encode()
        return Credentials(
            {"Authorization": f"Basic {base64.b64encode(pair).decode('ascii')}"}
        )


ClientAuthentication = Literal["basic", "body"]
CLIENT_AUTHENTICATION = (
    "basic: the client ID and secret go in the Authorization header; body: in "
    "the request's form."
)


def _client(
    client_id: str, client_secret: str, how: ClientAuthentication
) -> dict[str, Any]:
    """The client as the token endpoint is told who's asking."""
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_method": "client_secret_post" if how == "body" else "client_secret_basic",
    }


class ClientCredentialsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        min_length=1,
        max_length=512,
        title="Client ID",
        description="The client Forge signs in as.",
    )
    token_url: str = Field(
        min_length=1,
        max_length=2048,
        pattern=HTTP_URL,
        title="Token URL",
        description="The authorization server's token endpoint.",
        json_schema_extra={"placeholder": "https://auth.example.com/oauth/token"},
    )
    scopes: str = Field(
        default="",
        max_length=2048,
        pattern=SCOPES_PATTERN,
        title="Scopes",
        description="Space-separated. Empty: the client's own.",
    )
    audience: str = Field(
        default="",
        max_length=2048,
        title="Audience",
        description=(
            "The API the token is for, for authorization servers that ask for "
            "one (Auth0). Empty: none is sent."
        ),
    )
    client_authentication: ClientAuthentication = Field(
        default="basic",
        title="Client authentication",
        description=CLIENT_AUTHENTICATION,
    )


class ClientCredentialsSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret: Secret = Field(
        title="Client secret", description="Kept encrypted; nobody can read it back."
    )


class ClientCredentials(
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
        data = {"grant_type": "client_credentials"}
        if settings.scopes:
            data["scope"] = settings.scopes
        if settings.audience:
            data["audience"] = settings.audience
        body = await _token_request(
            context.http,
            settings.token_url,
            _client(
                settings.client_id,
                context.secrets.client_secret,
                settings.client_authentication,
            ),
            data,
            again="check the client ID and secret",
        )
        fetched = _tokens(body)
        # Never refreshed: a new one is asked for.
        fetched["refresh_token"] = None
        return Credentials(_bearer(fetched), {"tokens": fetched})


class SignInSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str = Field(
        min_length=1,
        max_length=2048,
        pattern=HTTP_URL,
        title="Authorization URL",
        description="Where a person signs in: the authorization server's authorization endpoint.",
        json_schema_extra={"placeholder": "https://auth.example.com/authorize"},
    )
    token_url: str = Field(
        min_length=1,
        max_length=2048,
        pattern=HTTP_URL,
        title="Token URL",
        description="The authorization server's token endpoint.",
        json_schema_extra={"placeholder": "https://auth.example.com/oauth/token"},
    )
    client_id: str = Field(
        min_length=1,
        max_length=512,
        title="Client ID",
        description="A client registered for Forge, with Forge's callback as a redirect URI.",
    )
    scopes: str = Field(
        default="",
        max_length=2048,
        pattern=SCOPES_PATTERN,
        title="Scopes",
        description="Space-separated. Empty: the client's own.",
    )
    authorization_params: str = Field(
        default="",
        max_length=2048,
        pattern=r"^[^\s#]*$",
        title="More sign-in parameters",
        description=(
            "Added to the sign-in address, as a query string: "
            "access_type=offline&prompt=consent asks Google for a refresh token."
        ),
        json_schema_extra={"placeholder": "None"},
    )
    client_authentication: ClientAuthentication = Field(
        default="basic",
        title="Client authentication",
        description=CLIENT_AUTHENTICATION,
    )


class SignInSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret: str = Field(
        default="",
        max_length=8192,
        title="Client secret",
        description="When the client has one. Kept encrypted.",
    )


class SignIn(InteractiveAuthMethod[SignInSettings, SignInSecrets]):
    kind = "oauth"
    label = "OAuth sign-in"
    description = (
        "Someone signs in to the API's authorization server once; Forge keeps "
        "the tokens, refreshes them and uses them for every run."
    )
    settings_model = SignInSettings
    secrets_model = SignInSecrets

    @staticmethod
    def _client(context: AuthContext[SignInSettings, SignInSecrets]) -> dict[str, Any]:
        settings = context.settings
        return _client(
            settings.client_id,
            context.secrets.client_secret,
            settings.client_authentication,
        )

    async def begin(
        self,
        context: AuthContext[SignInSettings, SignInSecrets],
        *,
        redirect_uri: str,
        state: str,
    ) -> Authorization:
        settings = context.settings
        verifier, challenge = _pkce()
        query: dict[str, str] = dict(parse_qsl(settings.authorization_params))
        query |= {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if settings.scopes:
            query["scope"] = settings.scopes
        separator = "&" if urlsplit(settings.authorization_url).query else "?"
        return Authorization(
            url=f"{settings.authorization_url}{separator}{urlencode(query)}",
            pending={"verifier": verifier, "scope": settings.scopes},
        )

    async def complete(
        self,
        context: AuthContext[SignInSettings, SignInSecrets],
        *,
        pending: dict[str, Any],
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        body = await _token_request(
            context.http,
            context.settings.token_url,
            self._client(context),
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": pending["verifier"],
            },
            again="sign in again",
        )
        return {
            "tokens": _tokens(body)
            | {"scope": body.get("scope") or pending.get("scope") or None},
            "connected_at": time.time(),
        }

    async def credentials(
        self, context: AuthContext[SignInSettings, SignInSecrets]
    ) -> Credentials:
        grant = context.grant
        tokens = grant.get("tokens") or {}
        if not tokens.get("access_token"):
            raise NotConnected(
                "Nobody has signed in with it yet: sign in on the Credentials page"
            )
        if _fresh(tokens):
            return Credentials(_bearer(tokens))
        if not tokens.get("refresh_token"):
            raise NotConnected(f"Its sign-in ran out: {SIGN_IN_AGAIN}")
        body = await _token_request(
            context.http,
            context.settings.token_url,
            self._client(context),
            {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            again=SIGN_IN_AGAIN,
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


#: The methods a credential may use. Register a new one here.
CREDENTIAL_METHODS = AuthMethods(
    [
        HeaderKeyAuth(),
        QueryKeyAuth(),
        BearerAuth(),
        BasicAuth(),
        ClientCredentials(),
        SignIn(),
    ]
)

# --------------------------------------------------------------- service


class CredentialRow(Protocol):
    """A credential as it's kept: the admin API's ``ApiCredential``, or
    :class:`StoredCredential`."""

    id: str
    organization_id: str
    name: str
    #: Where it may be sent: URLs, each its origin and optionally a path.
    urls: list[str]
    auth_kind: str
    auth_settings: dict[str, Any]
    #: Ciphertext, or None for none.
    auth_secrets: str | None
    auth_grant: str | None


class CredentialUnavailable(LookupError):
    """A request can't have a credential: it's gone, may not go there, isn't
    signed in to, or its secrets can't be read. Says why."""


class ApiCredentials:
    """
    What's done with credentials through their methods: what a request
    sends, and signing in. Methods change a credential's row (its grant) and
    leave committing to the caller.

    :param box: Encrypts the credentials' secrets and grants.
    :param timeout: Seconds an authorization server has to answer.
    :param methods: The auth methods there are.
    :param transport: For the methods' own requests; tests stand one in.
    """

    def __init__(
        self,
        box: SecretBox,
        *,
        timeout: float = 15.0,
        methods: AuthMethods = CREDENTIAL_METHODS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.box = box
        self.timeout = timeout
        self.methods = methods
        self.transport = transport

    def http(self) -> httpx.AsyncClient:
        """:return: A client for a method's own requests; close it after."""
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    def method(self, row: CredentialRow) -> AuthMethod[Any, Any]:
        """:raises ValueError: Its method is one there isn't (any more)."""
        return self.methods.get(row.auth_kind)

    def secrets(self, row: CredentialRow) -> dict[str, Any]:
        """:raises SecretsError: They can't be decrypted."""
        return self.box.open(row.auth_secrets)

    def grant(self, row: CredentialRow) -> dict[str, Any]:
        """:return: The grant; empty when it can't be decrypted."""
        try:
            return self.box.open(row.auth_grant)
        except SecretsError:
            return {}

    def set_grant(self, row: CredentialRow, grant: dict[str, Any] | None) -> None:
        row.auth_grant = self.box.seal(grant)

    def context(
        self, row: CredentialRow, http: httpx.AsyncClient
    ) -> AuthContext[Any, Any]:
        """
        :raises SecretsError: Its secrets can't be decrypted.
        :raises ValueError: Its settings or secrets no longer fit its method.
        """
        method = self.method(row)
        return AuthContext(
            server_url=row.urls[0] if row.urls else "",
            settings=method.settings(row.auth_settings),
            secrets=method.secrets(self.secrets(row)),
            grant=self.grant(row),
            http=http,
        )

    def connection(self, row: CredentialRow) -> Connection | None:
        try:
            return self.method(row).connection(self.grant(row))
        except ValueError:
            return None

    async def credentials(self, row: CredentialRow) -> Credentials:
        """
        What a request sends: its method's headers and query parameters. A
        grant the method renewed is kept on the row.

        :raises NotConnected: A person has to sign in first.
        :raises AuthError: Its credentials couldn't be had.
        :raises SecretsError: Its secrets can't be decrypted.
        """
        async with self.http() as http:
            try:
                context = self.context(row, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            found = await self.method(row).credentials(context)
        if found.grant is not None:
            self.set_grant(row, found.grant)
        return found

    async def for_request(self, row: CredentialRow, url: str) -> Credentials:
        """
        :return: What a request to ``url`` sends.
        :raises CredentialUnavailable: It may not be sent there, or can't be had.
        """
        if not sends_to(url, row.urls):
            raise CredentialUnavailable(
                f"The credential {row.name} is only sent to {', '.join(row.urls) or 'nowhere'}, "
                f"not to {_origin(url)}"
            )
        try:
            return await self.credentials(row)
        except (AuthError, SecretsError) as error:
            raise CredentialUnavailable(f"The credential {row.name}: {error}") from None

    def interactive(self, row: CredentialRow) -> InteractiveAuthMethod[Any, Any]:
        """:raises ValueError: Its method isn't one a person signs in with."""
        method = self.method(row)
        if not isinstance(method, InteractiveAuthMethod):
            raise ValueError(f"{method.label} isn't signed in with")
        return method

    async def begin(
        self, row: CredentialRow, *, redirect_uri: str, state: str
    ) -> Authorization:
        """
        :raises ValueError: Its method isn't one a person signs in with.
        :raises AuthError: Its settings can't be used.
        """
        method = self.interactive(row)
        async with self.http() as http:
            try:
                context = self.context(row, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            return await method.begin(context, redirect_uri=redirect_uri, state=state)

    async def complete(
        self,
        row: CredentialRow,
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
        method = self.interactive(row)
        async with self.http() as http:
            try:
                context = self.context(row, http)
            except ValueError as error:
                raise AuthError(str(error)) from None
            grant = await method.complete(
                context, pending=pending, code=code, redirect_uri=redirect_uri
            )
        self.set_grant(row, grant | {"connected_by": user})


def _origin(url: str) -> str:
    parts = _parts(url)
    return f"{parts[0]}://{parts[1]}" if parts else url[:80]


class CredentialStore(Protocol):
    """Where credentials are kept: the admin's models, or :class:`SqlCredentialStore`."""

    def open(
        self, credential_id: str
    ) -> AbstractAsyncContextManager[CredentialRow | None]:
        """
        :return: The credential's row (None when there's none), locked against
            other processes until the block ends; what the block changed on
            it is kept then.
        """
        ...


async def request_credentials(
    credentials: ApiCredentials,
    store: CredentialStore,
    *,
    credential_id: str,
    organization_id: str | None,
    url: str,
) -> Credentials:
    """
    What a request of an organization's to ``url`` sends through one of its
    credentials, a renewed grant kept on the credential's row.

    :raises CredentialUnavailable: The organization has no such credential, it
        may not be sent to ``url``, nobody has signed in with it, or what it
        sends can't be had.
    """
    if organization_id is None:
        raise CredentialUnavailable("Only an organization's runs use its credentials")
    async with store.open(credential_id) as row:
        if row is None or row.organization_id != organization_id:
            raise CredentialUnavailable(
                f"The organization has no credential {credential_id!r}: it was deleted"
            )
        return await credentials.for_request(row, url)


# ----------------------------------------------------------------- store

#: The columns of the admin's table a request reads, and the grant it keeps.
CREDENTIALS = sa.Table(
    "api_credentials",
    sa.MetaData(),
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("organization_id", sa.String(36)),
    sa.Column("name", sa.String(200)),
    sa.Column("urls", sa.Text),
    sa.Column("auth_kind", sa.String(64)),
    sa.Column("auth_settings", sa.Text),
    sa.Column("auth_secrets", sa.Text),
    sa.Column("auth_grant", sa.Text),
)


@dataclass
class StoredCredential:
    """A credential as :class:`SqlCredentialStore` reads it: a :class:`CredentialRow`."""

    id: str
    organization_id: str
    name: str
    urls: list[str] = field(default_factory=list)
    auth_kind: str = "bearer"
    auth_settings: dict[str, Any] = field(default_factory=dict)
    auth_secrets: str | None = None
    auth_grant: str | None = None


def _json(text: str | None, empty: Any) -> Any:
    if not text:
        return empty
    try:
        return json.loads(text)
    except ValueError:
        return empty


class SqlCredentialStore:
    """
    Credentials in the admin's database, a row locked (``FOR UPDATE``) while
    it's open, so a grant is renewed by one process at a time.

    :param engine: The admin MySQL (or a test's SQLite, which doesn't lock).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def open(self, credential_id: str) -> AsyncIterator[StoredCredential | None]:
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(CREDENTIALS)
                        .where(CREDENTIALS.c.id == credential_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                yield None
                return
            credential = StoredCredential(
                id=row["id"],
                organization_id=row["organization_id"],
                name=row["name"],
                urls=_json(row["urls"], []),
                auth_kind=row["auth_kind"],
                auth_settings=_json(row["auth_settings"], {}),
                auth_secrets=row["auth_secrets"],
                auth_grant=row["auth_grant"],
            )
            kept = credential.auth_grant
            yield credential
            if credential.auth_grant != kept:
                await conn.execute(
                    sa.update(CREDENTIALS)
                    .where(CREDENTIALS.c.id == credential_id)
                    .values(auth_grant=credential.auth_grant)
                )
