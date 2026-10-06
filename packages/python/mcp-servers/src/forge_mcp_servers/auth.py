"""How Forge authenticates to an MCP server: the protocol every auth method
follows, and the registry the API and the web console read them from.

An auth method is one way of proving who's calling: no auth, an API key in a
header, a bearer token, OAuth. Each has a ``kind`` (what a server's
``auth.kind`` names), its **settings** (kept in the clear and shown, such as
a header's name or a client ID) and its **secrets** (encrypted, never
answered: an API key, a client secret), both Pydantic models whose fields
the web console draws as a form (:meth:`AuthMethod.describe`). What a method
keeps between calls (an OAuth client it registered, its tokens) is its
**grant**, encrypted like the secrets.

Before each connection, :meth:`AuthMethod.credentials` turns them into the
headers to send, refreshing what it must and handing back a new grant to
keep. A method a person has to connect first (an OAuth sign-in) is an
:class:`InteractiveAuthMethod`: :meth:`~InteractiveAuthMethod.begin` makes the
address to send them to, :meth:`~InteractiveAuthMethod.complete` the grant
from where they come back.

To support another method, subclass one of them and register an instance in
:data:`forge_mcp_servers.registry.AUTH_METHODS`; the API and the web
console's form pick it up from there.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, ValidationError

#: A header name, as RFC 9110 allows one.
HEADER_NAME_PATTERN = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$"


class AuthError(Exception):
    """A method couldn't get what it sends: a token endpoint refused, say."""


class NotConnected(AuthError):
    """A person has to connect the server first (sign in), or again."""


class NoFields(BaseModel):
    """Settings or secrets of a method that has none."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class AuthContext[S: BaseModel, K: BaseModel]:
    """What a method has to work with for one server."""

    #: The server's MCP endpoint.
    server_url: str
    settings: S
    secrets: K
    #: What the method kept last time; empty when it has kept nothing.
    grant: dict[str, Any]
    #: For the method's own requests (token endpoints, discovery).
    http: httpx.AsyncClient


@dataclass(frozen=True)
class Credentials:
    """What to send the server."""

    headers: dict[str, str] = field(default_factory=dict)
    #: A grant to keep in place of the old one (a refreshed token); None
    #: when it's unchanged.
    grant: dict[str, Any] | None = None


@dataclass(frozen=True)
class Connection:
    """What a server's grant says about who connected it, for people to read."""

    connected: bool
    connected_by: str | None = None
    connected_at: float | None = None
    #: When its access runs out (epoch seconds); it may be refreshed then.
    expires_at: float | None = None
    scope: str | None = None


@dataclass(frozen=True)
class Authorization:
    """Where to send a person to connect, and what to keep until they're back."""

    url: str
    #: Kept encrypted with the flow, and handed to ``complete``.
    pending: dict[str, Any]


class AuthField(BaseModel):
    """One field of a method's form, as the web console draws it."""

    name: str
    label: str
    description: str = ""
    #: Write-only: kept encrypted and never answered.
    secret: bool = False
    required: bool = False
    default: str = ""
    placeholder: str = ""


class AuthMethodInfo(BaseModel):
    """A method, as ``GET /mcp-auth-methods`` lists it."""

    kind: str
    label: str
    description: str
    #: A person connects the server (signs in) before it's used.
    interactive: bool
    fields: list[AuthField]


class AuthMethod[S: BaseModel, K: BaseModel](ABC):
    """
    One way of authenticating to an MCP server.

    Subclasses set the class attributes and :meth:`credentials`; settings and
    secrets are Pydantic models whose fields have a ``title`` (the label), a
    ``description``, a default when optional, and optionally a
    ``json_schema_extra={"placeholder": ...}``.
    """

    #: What a server's ``auth.kind`` names: lowercase, digits and _.
    kind: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    settings_model: ClassVar[type[BaseModel]] = NoFields
    secrets_model: ClassVar[type[BaseModel]] = NoFields

    @property
    def interactive(self) -> bool:
        return False

    @abstractmethod
    async def credentials(self, context: AuthContext[S, K]) -> Credentials:
        """
        :param context: The server, its settings, secrets and grant.
        :return: The headers to send, and a grant to keep when it changed.
        :raises NotConnected: A person has to connect it first.
        :raises AuthError: What it needs couldn't be had.
        """

    def connection(self, grant: dict[str, Any]) -> Connection | None:
        """
        :param grant: What the method kept.
        :return: Who connected the server, for methods a person connects;
            None for the others.
        """
        return None

    def settings(self, values: dict[str, Any]) -> S:
        """
        :raises ValueError: They aren't this method's settings.
        """
        return _validated(self.settings_model, values, "settings")  # type: ignore[return-value]

    def secrets(self, values: dict[str, Any]) -> K:
        """
        :raises ValueError: They aren't this method's secrets, or one it
            needs is missing.
        """
        return _validated(self.secrets_model, values, "secrets")  # type: ignore[return-value]

    def secret_names(self) -> list[str]:
        return list(self.secrets_model.model_fields)

    def describe(self) -> AuthMethodInfo:
        """:return: The method and its form's fields."""
        return AuthMethodInfo(
            kind=self.kind,
            label=self.label,
            description=self.description,
            interactive=self.interactive,
            fields=[
                *_fields(self.settings_model, secret=False),
                *_fields(self.secrets_model, secret=True),
            ],
        )


class InteractiveAuthMethod[S: BaseModel, K: BaseModel](AuthMethod[S, K]):
    """A method a person connects: they're sent to sign in, and come back."""

    @property
    def interactive(self) -> Literal[True]:
        return True

    @abstractmethod
    async def begin(
        self, context: AuthContext[S, K], *, redirect_uri: str, state: str
    ) -> Authorization:
        """
        :param context: The server, its settings and secrets.
        :param redirect_uri: Where the person comes back to.
        :param state: What comes back with them, naming the flow.
        :return: Where to send them, and what to keep until they're back.
        :raises AuthError: The server's authorization can't be found or used.
        """

    @abstractmethod
    async def complete(
        self,
        context: AuthContext[S, K],
        *,
        pending: dict[str, Any],
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """
        :param context: The server, its settings and secrets.
        :param pending: What :meth:`begin` kept.
        :param code: What the person came back with.
        :param redirect_uri: Where they came back to, as ``begin`` had it.
        :return: The grant to keep.
        :raises AuthError: The code wasn't exchanged.
        """


class AuthMethods:
    """The auth methods there are, by kind."""

    def __init__(self, methods: Iterable[AuthMethod[Any, Any]]) -> None:
        self._methods: dict[str, AuthMethod[Any, Any]] = {}
        for method in methods:
            self.register(method)

    def register(self, method: AuthMethod[Any, Any]) -> None:
        if method.kind in self._methods:
            raise ValueError(f"Two auth methods are called {method.kind!r}")
        self._methods[method.kind] = method

    def get(self, kind: str) -> AuthMethod[Any, Any]:
        """:raises ValueError: There's no such method."""
        try:
            return self._methods[kind]
        except KeyError:
            raise ValueError(f"There's no auth method {kind!r}") from None

    def __contains__(self, kind: object) -> bool:
        return kind in self._methods

    def __iter__(self) -> Iterator[AuthMethod[Any, Any]]:
        return iter(self._methods.values())


def _validated(model: type[BaseModel], values: dict[str, Any], what: str) -> BaseModel:
    try:
        return model.model_validate(values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in problem['loc']) or what}: {problem['msg']}"
            for problem in error.errors()
        )
        raise ValueError(f"The {what} aren't right: {problems}") from None


def _fields(model: type[BaseModel], *, secret: bool) -> Iterator[AuthField]:
    for name, info in model.model_fields.items():
        extra = (
            info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        )
        default = info.get_default() if not info.is_required() else ""
        yield AuthField(
            name=name,
            label=info.title or name.replace("_", " ").capitalize(),
            description=info.description or "",
            secret=secret,
            required=info.is_required(),
            default=str(default) if default is not None else "",
            placeholder=str(extra.get("placeholder", "")),
        )
