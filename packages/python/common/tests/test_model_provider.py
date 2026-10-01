import base64
import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from forge_common.model_provider import (
    SHARED_CONFIG_DIR,
    ApiKeyAuth,
    ModelProviderAuthError,
    ModelProviderConfig,
    ModelProviderConfigError,
    OAuth2Auth,
    OAuth2TokenSource,
    clamp_thinking_level,
    default_model_provider_config_path,
    load_model_provider_config,
    parse_model_provider_yaml,
    supported_thinking_levels,
    thinking_value,
)


def oauth_yaml(provider_id: str = "corp-test") -> str:
    return f"""
version: 1
default:
  provider: {provider_id}
  model: general
providers:
  {provider_id}:
    name: Corporate Gateway
    baseUrl: ${{GATEWAY_BASE_URL}}
    api: openai-responses
    headers:
      x-tenant: prefix-${{TENANT}}
      x-template: $${{NOT_AN_ENV_REFERENCE}}
      x-literal: "!must-not-execute-$${{LITERAL}}"
    auth:
      type: oauth2
      tokenUrl: ${{TOKEN_URL}}
      clientId: ${{CLIENT_ID}}
      clientSecret: ${{CLIENT_SECRET}}
      clientAuthentication: body
      scopes: [models.read, models.invoke]
      audience: model-gateway
      tokenHeaders:
        x-token-tenant: ${{TOKEN_TENANT}}
    models:
      - id: general
        reasoning: true
        headers:
          X-Tenant: model-${{TENANT}}
          x-model-route: ${{MODEL_ROUTE}}
"""


ENVIRONMENT = {
    "GATEWAY_BASE_URL": "https://gateway.example.test/v1",
    "TOKEN_URL": "https://identity.example.test/oauth/token",
    "CLIENT_ID": "worker-client",
    "CLIENT_SECRET": "resolved-client-secret",
    "TENANT": "engineering",
    "TOKEN_TENANT": "identity-platform",
    "MODEL_ROUTE": "workflows",
}


def model_yaml(model: str, api: str = "openai-responses") -> str:
    return f"""
version: 1
default: {{provider: p, model: m}}
providers:
  p:
    baseUrl: https://example.test/v1
    api: {api}
    auth: {{type: apiKey, key: k}}
    models:
      - {model}
"""


def error_of(parse: Callable[[], object]) -> ModelProviderConfigError:
    with pytest.raises(ModelProviderConfigError) as raised:
        parse()
    return raised.value


# Parsing.


def test_it_resolves_environment_references_and_keeps_escaped_literals() -> None:
    config = parse_model_provider_yaml(oauth_yaml(), ENVIRONMENT)
    provider = config.providers["corp-test"]

    assert provider.base_url == "https://gateway.example.test/v1"
    assert provider.headers == {
        "x-tenant": "prefix-engineering",
        "x-template": "${NOT_AN_ENV_REFERENCE}",
        "x-literal": "!must-not-execute-${LITERAL}",
    }
    assert isinstance(provider.auth, OAuth2Auth)
    assert provider.auth.client_id == "worker-client"
    assert provider.auth.client_secret.get_secret_value() == "resolved-client-secret"
    assert provider.auth.scopes == ["models.read", "models.invoke"]
    [model] = provider.models
    assert (model.id, model.display_name, model.input) == ("general", "general", ["text"])
    assert (model.context_window, model.max_tokens) == (128_000, 16_384)
    assert model.headers == {"X-Tenant": "model-engineering", "x-model-route": "workflows"}


def test_a_models_headers_replace_its_providers_whatever_their_case() -> None:
    config = parse_model_provider_yaml(oauth_yaml(), ENVIRONMENT)

    assert config.default_model.headers == {
        "X-Tenant": "model-engineering",
        "x-template": "${NOT_AN_ENV_REFERENCE}",
        "x-literal": "!must-not-execute-${LITERAL}",
        "x-model-route": "workflows",
    }


def test_a_missing_variable_fails_closed_without_leaking_values() -> None:
    environment = {k: v for k, v in ENVIRONMENT.items() if k != "TOKEN_URL"}

    error = error_of(lambda: parse_model_provider_yaml(oauth_yaml(), environment))

    assert (
        str(error) == "Missing environment variable TOKEN_URL at providers.corp-test.auth.tokenUrl"
    )
    assert "resolved-client-secret" not in str(error)


def test_a_dollar_brace_that_isnt_a_reference_is_refused() -> None:
    error = error_of(lambda: parse_model_provider_yaml(model_yaml("id: ${lower-case}"), {}))

    assert str(error) == ("Invalid environment placeholder at providers.p.models.0.id; use ${NAME}")


def test_the_default_model_must_be_declared() -> None:
    text = oauth_yaml().replace("model: general", "model: unavailable")

    error = error_of(lambda: parse_model_provider_yaml(text, ENVIRONMENT))

    assert "must name a model declared by the default provider" in str(error)


def test_malformed_yaml_isnt_echoed() -> None:
    error = error_of(
        lambda: parse_model_provider_yaml("clientSecret: literal-secret\nproviders: [", {})
    )

    assert str(error).startswith("Invalid model provider YAML")
    assert "literal-secret" not in str(error)


def test_invalid_values_are_located_but_never_quoted() -> None:
    text = """
version: 1
default: {provider: p, model: m}
providers:
  p:
    baseUrl: https://user:hunter2@example.test
    api: openai-chat
    auth: {type: apiKey, key: "  "}
    models:
      - {id: m, contextWindow: "128000"}
  bad id:
    baseUrl: ftp://example.test
"""
    error = str(error_of(lambda: parse_model_provider_yaml(text, {})))

    assert "hunter2" not in error
    assert "providers.p.baseUrl: must not contain embedded credentials" in error
    assert "providers.p.api: Input should be" in error
    assert "providers.p.auth.key: String should have at least 1 character" in error
    assert "providers.p.models.0.contextWindow: Input should be a valid integer" in error
    assert "providers.bad id.[key]: must contain only letters" in error
    assert "providers.bad id.baseUrl: must use http:// or https://" in error


def test_model_ids_are_unique_within_a_provider() -> None:
    text = model_yaml("{id: m}\n      - {id: m}")

    error = error_of(lambda: parse_model_provider_yaml(text, {}))

    assert "providers.p.models: model ids must be unique within a provider" in str(error)


def test_it_reads_yaml_1_2() -> None:
    # YAML 1.2: off is a string (a thinkingLevelMap key), dates stay text.
    text = model_yaml(
        "{id: m, reasoning: true, thinkingLevelMap: {off: none, minimal: null, xhigh: xhigh}}"
    ).replace(
        "{type: apiKey, key: k}",
        "{type: oauth2, tokenUrl: 'https://t.test', "
        "clientId: c, clientSecret: s, expiresAt: 2026-08-08T00:00:00Z}",
    )

    config = parse_model_provider_yaml(text, {})

    model = config.default_model.model
    assert model.thinking_level_map is not None
    assert model.thinking_level_map.off == "none"
    auth = config.providers["p"].auth
    assert isinstance(auth, OAuth2Auth)
    assert auth.expires_at == "2026-08-08T00:00:00Z"


def test_a_repeated_key_is_refused() -> None:
    error = error_of(lambda: parse_model_provider_yaml("version: 1\nversion: 1\n", {}))

    assert str(error) == "Invalid model provider YAML (line 2, column 1)"


def test_the_shared_openai_file_uses_api_keys_for_openai_and_claude() -> None:
    path = SHARED_CONFIG_DIR / "model_provider.openai.yaml"
    environment = {
        "OPENAI_API_KEY": " test-openai-key ",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "OPENAI_MODEL": "gpt-5.2",
    }

    config = load_model_provider_config(path, environment)

    assert config.default_model.ref == "openai/gpt-5.2"
    assert [model.ref for model in config.models()] == [
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.2",
        "anthropic/claude-opus-5",
    ]
    openai = config.providers["openai"].auth
    assert isinstance(openai, ApiKeyAuth)
    assert openai.key.get_secret_value() == "test-openai-key"
    claude = config.find("claude-opus-5")
    assert claude is not None
    assert (claude.api, claude.base_url) == ("anthropic-messages", "https://api.anthropic.com")
    # Anthropic keys go in x-api-key, not a Bearer header.
    assert claude.provider.auth_header is False
    # Its thinking is always on.
    assert supported_thinking_levels(claude.model) == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert "test-openai-key" not in repr(config)
    for missing in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        without = {k: v for k, v in environment.items() if k != missing}
        with pytest.raises(ModelProviderConfigError, match=missing):
            load_model_provider_config(path, without)
    with pytest.raises(ModelProviderConfigError, match="declared by the default provider"):
        load_model_provider_config(path, {**environment, "OPENAI_MODEL": "gpt-4o"})
    for key in ("", "   ", "line\nbreak"):
        with pytest.raises(ModelProviderConfigError):
            load_model_provider_config(path, {**environment, "OPENAI_API_KEY": key})


def test_the_shared_default_file_is_the_gateway_example() -> None:
    config = load_model_provider_config(
        None,
        {
            "OPENAI_API_KEY": "k",
            "MODEL_GATEWAY_BASE_URL": "https://gateway.example.test/v1",
            "MODEL_GATEWAY_TOKEN_URL": "https://identity.example.test/token",
            "MODEL_GATEWAY_CLIENT_ID": "id",
            "MODEL_GATEWAY_CLIENT_SECRET": "secret",
        },
    )

    assert default_model_provider_config_path().parent == SHARED_CONFIG_DIR
    assert [model.ref for model in config.models()] == [
        "openai/gpt-5.2",
        "company-gateway/company-model",
    ]
    assert config.default_model.ref == "company-gateway/company-model"


def test_an_unreadable_file_is_named() -> None:
    error = error_of(lambda: load_model_provider_config("missing/model_provider.yaml", {}))

    assert str(error) == "Cannot read model provider configuration: missing/model_provider.yaml"


def test_models_are_found_by_reference_or_unambiguous_id() -> None:
    config = ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": "a", "model": "shared"},
            "providers": {
                name: {
                    "baseUrl": "https://example.test",
                    "api": "openai-completions",
                    "auth": {"type": "apiKey", "key": "k"},
                    "models": [{"id": "shared"}, {"id": f"{name}-only"}, {"id": "org/model"}],
                }
                for name in ("a", "b")
            },
        }
    )

    assert config.find("b/shared").ref == "b/shared"  # type: ignore[union-attr]
    assert config.find("a/org/model").ref == "a/org/model"  # type: ignore[union-attr]
    assert config.find("b-only").ref == "b/b-only"  # type: ignore[union-attr]
    # In both providers, so ambiguous.
    assert config.find("shared") is None
    assert config.find("c/shared") is None


# Thinking levels, by pi's rules.


def reasoning(thinking_level_map: str = "") -> ModelProviderConfig:
    mapping = f", thinkingLevelMap: {thinking_level_map}" if thinking_level_map else ""
    return parse_model_provider_yaml(model_yaml(f"{{id: m, reasoning: true{mapping}}}"), {})


def test_a_model_without_reasoning_only_has_off() -> None:
    model = parse_model_provider_yaml(model_yaml("{id: m}"), {}).default_model.model

    assert supported_thinking_levels(model) == ["off"]
    assert clamp_thinking_level(model, "high") == "off"


def test_a_reasoning_model_has_xhigh_only_when_mapped_and_drops_null_levels() -> None:
    plain = reasoning().default_model.model
    mapped = reasoning("{minimal: null, xhigh: max}").default_model.model

    assert supported_thinking_levels(plain) == ["off", "minimal", "low", "medium", "high"]
    assert supported_thinking_levels(mapped) == ["off", "low", "medium", "high", "xhigh"]
    assert clamp_thinking_level(plain, "xhigh") == "high"
    assert clamp_thinking_level(mapped, "minimal") == "low"
    assert clamp_thinking_level(mapped, "unknown") == "off"
    assert thinking_value(mapped, "xhigh") == "max"
    assert thinking_value(mapped, "high") == "high"
    assert thinking_value(plain, "off") is None


# OAuth2 tokens.


class TokenEndpoint:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def form(self, index: int) -> dict[str, list[str]]:
        return parse_qs(self.requests[index].content.decode())

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def oauth2(**fields: object) -> OAuth2Auth:
    return OAuth2Auth.model_validate(
        {
            "type": "oauth2",
            "tokenUrl": "https://identity.example.test/oauth/token",
            "clientId": "worker client",
            "clientSecret": "s3cr*t~",
            **fields,
        }
    )


class Clock:
    def __init__(self) -> None:
        self.ms = 1_000_000.0

    def __call__(self) -> float:
        return self.ms


async def test_a_client_credentials_token_is_reused_until_it_nearly_expires() -> None:
    endpoint = TokenEndpoint(
        httpx.Response(200, json={"access_token": "one", "expires_in": 100, "refresh_token": "r1"}),
        httpx.Response(200, json={"access_token": "two", "token_type": "Bearer"}),
    )
    clock = Clock()
    source = OAuth2TokenSource(
        oauth2(
            scopes=["models.invoke"],
            audience="gateway",
            tokenParameters={"resource": "x"},
            tokenHeaders={"x-tenant": "t"},
        ),
        transport=endpoint.transport,
        now=clock,
    )

    assert await source.token() == "one"
    clock.ms += 80_000
    assert await source.token() == "one"
    # 100 s less a tenth of it, less the margin.
    clock.ms += 6_000
    assert await source.token() == "two"

    first, second = endpoint.form(0), endpoint.form(1)
    assert first == {
        "resource": ["x"],
        "grant_type": ["client_credentials"],
        "scope": ["models.invoke"],
        "audience": ["gateway"],
        "client_id": ["worker client"],
        "client_secret": ["s3cr*t~"],
    }
    assert second["grant_type"] == ["refresh_token"]
    assert second["refresh_token"] == ["r1"]
    assert endpoint.requests[0].headers["x-tenant"] == "t"
    assert endpoint.requests[0].headers["accept"] == "application/json"


async def test_basic_client_authentication_form_encodes_the_credentials() -> None:
    endpoint = TokenEndpoint(httpx.Response(200, json={"access_token": "token"}))
    source = OAuth2TokenSource(oauth2(clientAuthentication="basic"), transport=endpoint.transport)

    await source.token()

    [request] = endpoint.requests
    encoded = request.headers["authorization"].removeprefix("Basic ")
    assert base64.b64decode(encoded).decode() == "worker+client:s3cr*t%7E"
    assert "client_secret" not in endpoint.form(0)


async def test_a_configured_access_token_is_used_until_it_expires() -> None:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 2_000}).encode()).decode().rstrip("=")
    jwt = f"header.{claims}.signature"
    endpoint = TokenEndpoint(httpx.Response(200, json={"access_token": "fresh"}))
    clock = Clock()
    source = OAuth2TokenSource(oauth2(accessToken=jwt), transport=endpoint.transport, now=clock)

    assert await source.token() == jwt
    clock.ms = 2_000_000
    assert await source.token() == "fresh"
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, text="secret body"), "OAuth token endpoint returned HTTP 401"),
        (httpx.Response(200, text="not json"), "OAuth token endpoint returned invalid JSON"),
        (httpx.Response(200, json={"token": "x"}), "invalid token response"),
        (
            httpx.Response(200, json={"access_token": "x", "token_type": "mac"}),
            "OAuth token endpoint returned a non-Bearer token",
        ),
    ],
)
async def test_token_endpoint_failures_say_what_failed_without_its_body(
    response: httpx.Response, message: str
) -> None:
    source = OAuth2TokenSource(oauth2(), transport=TokenEndpoint(response).transport)

    with pytest.raises(ModelProviderAuthError, match=message) as raised:
        await source.token()

    assert "secret body" not in str(raised.value)
