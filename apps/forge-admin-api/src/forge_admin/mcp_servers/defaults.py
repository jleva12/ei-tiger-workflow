"""
The application's default MCP servers: ready-made configurations the web
console offers when someone adds an MCP server to an organization.

Each is what connecting to a known server takes (its name, URL, timeout, how
Forge authenticates to it, and the headers it needs), so the person only
fills in what's theirs: a token, a header's value. Picking one opens the
usual form, filled in. They live in ``default_servers.yaml`` beside this
module, or in the file ``FORGE_ADMIN_MCP_SERVER_DEFAULTS`` names, and are
checked when the API starts: an auth method there isn't, or settings it
doesn't take, stop it.

A string may name a value from the environment, ``${NAME}``, or with one to
fall back on, ``${NAME:-default}``: the process environment over the admin's
``.env`` (``env_files.environment``), so one file serves a native run and
Compose, whose services reach each other by other addresses.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from forge_mcp_servers.auth import HEADER_NAME_PATTERN, AuthMethods
from forge_mcp_servers.registry import AUTH_METHODS
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

DEFAULTS_FILE = Path(__file__).with_name("default_servers.yaml")

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldHelp(_Strict):
    """How this server words one of its auth method's fields."""

    label: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=1000)
    placeholder: str | None = Field(default=None, max_length=200)


class DefaultAuth(_Strict):
    """How Forge authenticates to the server."""

    #: An auth method's kind (``GET /mcp-auth-methods``).
    kind: str = "none"
    #: The method's settings; its defaults for those left out.
    settings: dict[str, Any] = {}
    #: Help for the method's fields that's particular to this server, by
    #: field name: where to get its token, say.
    fields: dict[str, FieldHelp] = {}


class DefaultHeader(_Strict):
    """A header the server takes with every request."""

    name: str = Field(pattern=HEADER_NAME_PATTERN)
    #: Filled in; the person may change it.
    value: str = Field(default="", max_length=8192)
    description: Text = ""
    #: The server needs it: it can't be removed, and needs a value.
    required: bool = False


class DefaultServer(_Strict):
    """A server the web console offers to add, filled in."""

    #: Stable, for the web console: lowercase letters, digits and -.
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    #: What the added server is called, until the person renames it.
    name: str = Field(min_length=1, max_length=200)
    description: Text = ""
    #: Its MCP endpoint.
    url: str = Field(pattern=r"^https?://[^\s/?#]+[^\s]*$", max_length=2048)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: What the person has to do before adding it, in a sentence or two.
    instructions: Text = ""
    auth: DefaultAuth = DefaultAuth()
    headers: list[DefaultHeader] = Field(default=[], max_length=50)

    @model_validator(mode="after")
    def _unique_headers(self) -> "DefaultServer":
        names = [header.name.lower() for header in self.headers]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.id}: a header is listed twice")
        return self


class DefaultServers(_Strict):
    """The whole file."""

    servers: list[DefaultServer] = []

    @model_validator(mode="after")
    def _unique(self) -> "DefaultServers":
        for what in ("id", "name"):
            values = [getattr(server, what) for server in self.servers]
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise ValueError(
                    f"server {what}s must be unique: {', '.join(repeated)}"
                )
        return self

    def check(self, methods: AuthMethods) -> "DefaultServers":
        """
        :raises ValueError: A server names an auth method there isn't, gives
            it settings it doesn't take, or helps with a field it hasn't.
        """
        for server in self.servers:
            try:
                method = methods.get(server.auth.kind)
                method.settings(server.auth.settings)
            except ValueError as error:
                raise ValueError(f"{server.id}: {error}") from None
            fields = {field.name for field in method.describe().fields}
            unknown = sorted(set(server.auth.fields) - fields)
            if unknown:
                raise ValueError(
                    f"{server.id}: {method.label} has no field {', '.join(unknown)}"
                )
        return self


def resolve(value: Any, environment: Mapping[str, str], where: str = "") -> Any:
    """
    ``value`` with every string's ``${NAME}`` and ``${NAME:-default}``
    replaced from ``environment``.

    :raises ValueError: A string names a value nothing defines, without one
        to fall back on.
    """
    if isinstance(value, str):

        def lookup(match: re.Match[str]) -> str:
            name, fallback = match[1], match[2]
            found = environment.get(name)
            if found:
                return found
            if fallback is not None:
                return fallback
            raise ValueError(f"{where or 'a value'} names ${{{name}}}, which isn't set")

        return _REFERENCE.sub(lookup, value)
    if isinstance(value, list):
        return [resolve(item, environment, where) for item in value]
    if isinstance(value, dict):
        return {
            key: resolve(item, environment, f"{where}.{key}" if where else str(key))
            for key, item in value.items()
        }
    return value


def load(
    path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    methods: AuthMethods = AUTH_METHODS,
) -> list[DefaultServer]:
    """
    The default servers in ``path`` (the bundled file by default), their
    references resolved from ``environment``.

    :raises ValueError: The file isn't valid YAML in this shape, names a value
        nothing defines, or an auth method, setting or field there isn't.
    """
    path = path or DEFAULTS_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        resolved = resolve(data, environment or {})
        return DefaultServers.model_validate(resolved).check(methods).servers
    except (yaml.YAMLError, ValidationError, ValueError) as error:
        raise ValueError(
            f"{path} isn't a valid MCP server defaults file: {error}"
        ) from None
