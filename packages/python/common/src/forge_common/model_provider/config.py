"""model_provider.yaml: the model providers an app may use, their models and how
to reach them.

The one loader for the shared file: the admin API's assistant and workflows'
agent steps both read it through here, so every app reads one configuration
with the same schema and rules (pi's model provider format). Keys are
camelCase, as in the YAML; the Python attributes are their snake_case names.

``${NAME}`` in any value is the environment variable NAME, and ``$${NAME}`` a
literal ``${NAME}``. A missing variable fails, naming the variable and where it
is, never a value: a file that declares several providers needs every
provider's variables. Errors never include values from the file either, which
may be credentials.
"""

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

__all__ = [
    "SHARED_CONFIG_DIR",
    "ApiKeyAuth",
    "ConfiguredModel",
    "Cost",
    "DefaultModel",
    "ModelApi",
    "ModelProviderAuthError",
    "ModelProviderConfig",
    "ModelProviderConfigError",
    "OAuth2Auth",
    "Provider",
    "ResolvedModel",
    "ThinkingLevelMap",
    "default_model_provider_config_path",
    "load_model_provider_config",
    "parse_model_provider_yaml",
    "resolve_model_provider_environment",
]

# The shared files: model_provider.yaml (an OAuth gateway and OpenAI) and
# model_provider.openai.yaml (OpenAI with an API key).
SHARED_CONFIG_DIR = Path(__file__).parent

ModelApi = Literal[
    "openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"
]


class ModelProviderConfigError(ValueError):
    """The configuration can't be read, or isn't valid. Its message holds no values."""


class ModelProviderAuthError(Exception):
    """A provider's credentials couldn't be obtained. Its message holds no values."""


def _http_url(value: str) -> str:
    try:
        url = urlsplit(value)
    except ValueError:
        raise ValueError("must be a URL") from None
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("must use http:// or https://")
    if url.username is not None or url.password is not None:
        raise ValueError("must not contain embedded credentials")
    return value


def _header_name(value: str) -> str:
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
        raise ValueError("must be a valid HTTP header name")
    return value


def _one_line(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("must not contain a line break")
    return value


HttpUrl = Annotated[str, AfterValidator(_http_url)]
Headers = dict[
    Annotated[str, AfterValidator(_header_name)], Annotated[str, AfterValidator(_one_line)]
]
Trimmed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class _Schema(BaseModel):
    # As strict as the TypeScript schema: its keys only, camelCase, no coercion
    # (``contextWindow: "128000"`` is refused there too).
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", strict=True, frozen=True)


class Cost(_Schema):
    """Price per million tokens; zero unless set."""

    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cache_read: float = Field(default=0, ge=0)
    cache_write: float = Field(default=0, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def _number(cls, value: object) -> object:
        # Strict floats refuse ints, which YAML gives for whole numbers.
        return float(value) if isinstance(value, int) and not isinstance(value, bool) else value


class ThinkingLevelMap(_Schema):
    """
    The provider's value for each thinking level, e.g. ``xhigh: high``. A level
    mapped to null is one the model doesn't support; ``xhigh`` is only offered
    when it's mapped (see :mod:`forge_common.model_provider.thinking`).
    """

    off: str | None = None
    minimal: str | None = None
    low: str | None = None
    medium: str | None = None
    high: str | None = None
    xhigh: str | None = None


class ConfiguredModel(_Schema):
    """One of a provider's models."""

    id: Trimmed
    # The model's display name; its id when unset.
    name: Trimmed | None = None
    # Overrides the provider's.
    api: ModelApi | None = None
    base_url: HttpUrl | None = None
    reasoning: bool = False
    thinking_level_map: ThinkingLevelMap | None = None
    input: list[Literal["text", "image"]] = Field(default=["text"], min_length=1)
    cost: Cost = Cost()
    context_window: int = Field(default=128_000, gt=0)
    # The most tokens it writes in one reply.
    max_tokens: int = Field(default=16_384, gt=0)
    # Merged over the provider's.
    headers: Headers | None = None
    # Pi's per-model compatibility switches; the Python adapters don't use them.
    compat: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        """Its name, or else its id."""
        return self.name or self.id


class OAuth2Auth(_Schema):
    """
    OAuth2 client credentials: a bearer token from ``token_url``, refreshed
    before it expires (:class:`~forge_common.model_provider.OAuth2TokenSource`).
    """

    type: Literal["oauth2"]
    token_url: HttpUrl
    client_id: NonEmpty
    client_secret: SecretStr = Field(min_length=1)
    client_authentication: Literal["body", "basic"] = "body"
    scopes: list[NonEmpty] = []
    audience: NonEmpty | None = None
    # Sent only to the token endpoint.
    token_headers: Headers = {}
    token_parameters: dict[str, str] = {}
    token_request_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    # When the token response doesn't say how long it lasts.
    default_expires_in_seconds: int = Field(default=3_600, ge=60, le=86_400)
    # A token to start with, and when it expires: epoch milliseconds or an ISO
    # date; a JWT's own exp when unset.
    access_token: SecretStr | None = Field(default=None, min_length=1)
    refresh_token: SecretStr | None = Field(default=None, min_length=1)
    expires_at: Annotated[int, Field(gt=0)] | NonEmpty | datetime | None = None


class ApiKeyAuth(_Schema):
    """A static API key."""

    type: Literal["apiKey"]
    key: SecretStr

    @field_validator("key", mode="before")
    @classmethod
    def _key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("String should have at least 1 character")
        return _one_line(value)


class Provider(_Schema):
    """A model provider: where it is, how to sign in and the models it serves."""

    name: Trimmed | None = None
    base_url: HttpUrl
    api: ModelApi
    # Sent with every model request.
    headers: Headers = {}
    # Send the key as an Authorization bearer header (pi's switch; the Python
    # adapters authenticate each API its own standard way).
    auth_header: bool = True
    auth: OAuth2Auth | ApiKeyAuth = Field(discriminator="type")
    models: list[ConfiguredModel] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _unique_ids(cls, models: list[ConfiguredModel]) -> list[ConfiguredModel]:
        ids = [model.id for model in models]
        if len(set(ids)) != len(ids):
            raise ValueError("model ids must be unique within a provider")
        return models


class DefaultModel(_Schema):
    provider: Trimmed
    model: Trimmed


def _provider_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("must contain only letters, numbers, dot, underscore, or hyphen")
    return value


@dataclass(frozen=True)
class ResolvedModel:
    """A model with its provider, and what the provider settles for it."""

    provider_id: str
    provider: Provider
    model: ConfiguredModel

    @property
    def ref(self) -> str:
        """``provider/model``: how apps name it, e.g. ``openai/gpt-5.2``."""
        return f"{self.provider_id}/{self.model.id}"

    @property
    def api(self) -> ModelApi:
        return self.model.api or self.provider.api

    @property
    def base_url(self) -> str:
        return self.model.base_url or self.provider.base_url

    @property
    def provider_name(self) -> str:
        return self.provider.name or self.provider_id

    @property
    def headers(self) -> dict[str, str]:
        """
        The provider's headers with the model's merged over them. HTTP header
        names are case-insensitive, so a model's ``x-tenant`` replaces its
        provider's ``X-Tenant`` rather than sending both.
        """
        merged: dict[str, tuple[str, str]] = {}
        for headers in (self.provider.headers, self.model.headers or {}):
            for name, value in headers.items():
                merged[name.lower()] = (name, value)
        return dict(merged.values())


class ModelProviderConfig(_Schema):
    """A model_provider.yaml, validated, with its environment references resolved."""

    version: Literal[1]
    default: DefaultModel
    providers: dict[Annotated[str, AfterValidator(_provider_id)], Provider]

    @model_validator(mode="after")
    def _default_exists(self) -> "ModelProviderConfig":
        provider = self.providers.get(self.default.provider)
        if provider is None:
            raise ValueError("default.provider must name a provider declared in providers")
        if not any(model.id == self.default.model for model in provider.models):
            raise ValueError("default.model must name a model declared by the default provider")
        return self

    def models(self) -> Iterator[ResolvedModel]:
        """Every model, in the file's order."""
        for provider_id, provider in self.providers.items():
            for model in provider.models:
                yield ResolvedModel(provider_id, provider, model)

    @property
    def default_model(self) -> ResolvedModel:
        found = self.find(f"{self.default.provider}/{self.default.model}")
        assert found is not None  # _default_exists
        return found

    def find(self, name: str) -> ResolvedModel | None:
        """
        A model by its ``provider/model`` reference or, when only one provider
        has it, its bare id.

        :param name: e.g. ``openai/gpt-5.2`` or ``gpt-5.2``.
        :return: The model; None when there's none, or the bare id is ambiguous.
        """
        provider_id, _, model_id = name.partition("/")
        provider = self.providers.get(provider_id)
        if provider is not None and model_id:
            model = next((m for m in provider.models if m.id == model_id), None)
            if model is not None:
                return ResolvedModel(provider_id, provider, model)
        matches = [found for found in self.models() if found.model.id == name]
        return matches[0] if len(matches) == 1 else None


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ESCAPED_OPEN = "\0MODEL_PROVIDER_ESCAPED_ENV_OPEN\0"

ConfigPath = Sequence[str | int]


def _display_path(path: ConfigPath) -> str:
    return ".".join(str(part) for part in path) if path else "(root)"


def resolve_model_provider_environment(
    value: object,
    environment: Mapping[str, str] | None = None,
    path: ConfigPath = (),
) -> object:
    """
    Resolves ``${NAME}`` in every string of a parsed YAML document, keys
    excepted. ``$${NAME}`` is a literal ``${NAME}``.

    :param value: The document, or a part of it.
    :param environment: The variables; the process environment by default.
    :param path: Where ``value`` is, for errors.
    :return: The document with its references resolved.
    :raises ModelProviderConfigError: A variable is missing, or a ``${`` isn't
        a reference.
    """
    env = os.environ if environment is None else environment
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            replacement = env.get(match[1])
            if replacement is None:
                raise ModelProviderConfigError(
                    f"Missing environment variable {match[1]} at {_display_path(path)}"
                )
            return replacement

        resolved = _PLACEHOLDER.sub(replace, value.replace("$${", _ESCAPED_OPEN))
        if "${" in resolved:
            raise ModelProviderConfigError(
                f"Invalid environment placeholder at {_display_path(path)}; use ${{NAME}}"
            )
        return resolved.replace(_ESCAPED_OPEN, "${")
    if isinstance(value, list):
        return [
            resolve_model_provider_environment(item, env, [*path, index])
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: resolve_model_provider_environment(item, env, [*path, key])
            for key, item in value.items()
        }
    return value


class _Yaml12Loader(yaml.SafeLoader):
    """
    PyYAML's safe loader with YAML 1.2's core schema: ``off``, ``yes`` and
    ``no`` stay strings (``thinkingLevelMap`` has an ``off`` key), dates stay
    strings, and a mapping may not repeat a key.
    """


_Yaml12Loader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first in (
    ("tag:yaml.org,2002:null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("tag:yaml.org,2002:bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("tag:yaml.org,2002:int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    (
        "tag:yaml.org,2002:float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
):
    _Yaml12Loader.add_implicit_resolver(_tag, re.compile(_pattern), _first)


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    value = str(loader.construct_scalar(node))
    if value.startswith("0o"):
        return int(value[2:], 8)
    if value.startswith("0x"):
        return int(value[2:], 16)
    return int(value)


def _construct_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> float:
    value = str(loader.construct_scalar(node)).lower()
    if value.endswith(".inf"):
        return float("-inf") if value.startswith("-") else float("inf")
    if value == ".nan":
        return float("nan")
    return float(value)


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate key", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_Yaml12Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_Yaml12Loader.add_constructor("tag:yaml.org,2002:float", _construct_float)
_Yaml12Loader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)


def _issue(error: Mapping[str, Any]) -> str:
    # The location and the message only: pydantic's input value could be a
    # credential.
    parts: list[str] = []
    for part in error["loc"]:
        # auth's location names the type pydantic tried (auth.apiKey.key),
        # which isn't a key of the file.
        if parts and parts[-1] == "auth" and part in ("apiKey", "oauth2"):
            continue
        parts.append(str(part))
    where = ".".join(parts) or "(root)"
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"  {where}: {message}"


def parse_model_provider_yaml(
    text: str, environment: Mapping[str, str] | None = None
) -> ModelProviderConfig:
    """
    Parses a model_provider.yaml, resolves its environment references and
    validates it.

    :param text: The file's contents.
    :param environment: The variables ``${NAME}`` resolves from; the process
        environment by default.
    :return: The configuration.
    :raises ModelProviderConfigError: The YAML, a reference or the
        configuration is invalid.
    """
    try:
        document = yaml.load(text, Loader=_Yaml12Loader)
    except yaml.YAMLError as error:
        # The parser's message may quote the source line, which can hold a
        # literal credential: say where, never what.
        mark = getattr(error, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ModelProviderConfigError(f"Invalid model provider YAML{where}") from None
    expanded = resolve_model_provider_environment(document, environment)
    try:
        return ModelProviderConfig.model_validate(expanded)
    except ValidationError as error:
        issues = "\n".join(_issue(issue) for issue in error.errors(include_input=False))
        raise ModelProviderConfigError(f"Invalid model provider configuration:\n{issues}") from None


def default_model_provider_config_path() -> Path:
    """The shared model_provider.yaml."""
    return SHARED_CONFIG_DIR / "model_provider.yaml"


def load_model_provider_config(
    path: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> ModelProviderConfig:
    """
    Reads and parses a model_provider.yaml (:func:`parse_model_provider_yaml`).

    :param path: The file; the shared model_provider.yaml by default. A
        relative path is relative to the working directory.
    :param environment: The variables ``${NAME}`` resolves from; the process
        environment by default.
    :return: The configuration.
    :raises ModelProviderConfigError: It can't be read, or isn't valid.
    """
    file = Path(path) if path is not None else default_model_provider_config_path()
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ModelProviderConfigError(
            f"Cannot read model provider configuration: {file}"
        ) from error
    return parse_model_provider_yaml(text, environment)
