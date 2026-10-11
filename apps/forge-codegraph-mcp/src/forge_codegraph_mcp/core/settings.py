"""Settings, from the environment and the app's ``.env``.

Every field maps to an environment variable with the ``CODEGRAPH_`` prefix;
nested groups join with ``__``:

    CODEGRAPH_SERVER__PORT=8103
    CODEGRAPH_MCP__AUTH__ADMIN_URL=http://localhost:8101/api/v1
    CODEGRAPH_SPANNER__DATABASE=projects/p/instances/i/databases/d

Precedence, lowest to highest: built-in defaults, ``.env`` in the working
directory, the process environment. ``${NAME}`` in ``.env`` names a value
the app shares with the others, from the repository's ``.env.common``
(core/env_files.py). See ``.env.example`` for every setting.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from forge_codegraph import INSTRUCTIONS
from forge_codegraph_query.settings import (
    LOCAL_DATABASE,
    CodeGraphSettings,
    EmbeddingSettings,
    SearchSettings,
    SpannerSettings,
)
from forge_common.logging import LoggingSettings, LogLevel
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from forge_codegraph_mcp.core.env_files import EnvFileSettingsSource

# How to use the tools (forge_codegraph.tools), the server instructions.


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ServerSettings(BaseModel):
    """Uvicorn process settings."""

    host: str = "127.0.0.1"
    port: int = Field(default=8103, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    reload: bool = False
    # Trust X-Forwarded-* headers from these proxy IPs ("*" = any; only behind a trusted LB).
    proxy_headers: bool = True
    forwarded_allow_ips: str = "127.0.0.1"
    # Mount prefix when served behind a path-rewriting proxy (e.g. "/api").
    root_path: str = ""
    timeout_keep_alive: int = Field(default=5, ge=1)
    # How long SIGTERM waits for in-flight requests.
    timeout_graceful_shutdown: int = Field(default=10, ge=1)
    limit_concurrency: int | None = Field(default=None, ge=1)


class AppLoggingSettings(LoggingSettings):
    levels: dict[str, LogLevel] = {
        **LoggingSettings.model_fields["levels"].default,
        "mcp.server.lowlevel.server": "WARNING",
        "mcp.server.streamable_http": "WARNING",
        "mcp.server.streamable_http_manager": "WARNING",
        "sse_starlette": "WARNING",
        "openai": "WARNING",
    }


class CorsSettings(BaseModel):
    """CORS is only installed when ``allow_origins`` is non-empty."""

    allow_origins: list[str] = []
    allow_credentials: bool = False
    allow_methods: list[str] = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers: list[str] = [
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "Mcp-Session-Id",
        "Mcp-Protocol-Version",
        "Last-Event-ID",
    ]
    # Browser MCP clients must be able to read the session id header.
    expose_headers: list[str] = ["X-Request-ID", "Mcp-Session-Id"]
    max_age: int = 600


class McpAuthSettings(BaseModel):
    """Who may call the MCP endpoint: Forge credentials, which the admin API
    checks, each reading its organization's repositories (access.py). Callers
    send an organization's API key (fk_…) or a Forge token minted for an
    organization as their bearer token."""

    # The admin API, with its prefix: GET <admin_url>/code-graph/access.
    admin_url: str = Field(default="http://localhost:8101/api/v1", pattern=r"^https?://\S+$")
    # The admin API's deployment key (its FORGE_ADMIN_API_KEY), when it has
    # one: sent as X-API-Key beside the caller's credential.
    admin_api_key: SecretStr | None = None
    # How long the admin API's answer for a credential is reused: a deleted
    # key, a removed member or a new repository shows within this long. Zero
    # asks on every request.
    cache_seconds: float = Field(default=60, ge=0)
    # Credentials whose answers are kept at once.
    cache_size: int = Field(default=1024, ge=1)
    # How long the admin API has to answer, in seconds.
    timeout: float = Field(default=10, gt=0)

    @field_validator("admin_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class McpRateLimitSettings(BaseModel):
    enabled: bool = False
    requests_per_second: float = Field(default=10.0, gt=0)
    burst_capacity: int | None = Field(default=None, ge=1)


class McpSettings(BaseModel):
    enabled: bool = True
    name: str = "codegraph"
    # Sent to clients on connect; tells the model how to use this server's tools.
    instructions: str | None = INSTRUCTIONS
    path: str = "/mcp"
    # Stateless mode: no server-side sessions, so any worker or replica can
    # serve any request behind a load balancer. Every tool is a read.
    stateless_http: bool = True
    # Plain JSON instead of SSE streams (only meaningful in stateless mode).
    json_response: bool = True
    # Hide internal exception details from clients; only ToolError messages are sent.
    mask_error_details: bool = True
    strict_input_validation: bool = False
    # Idle stateful sessions are reaped after this many seconds (ignored when stateless).
    session_idle_timeout: float | None = Field(default=3600, gt=0)
    # DNS-rebinding protection: validate Host/Origin headers.
    host_origin_protection: bool | Literal["auto"] = "auto"
    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    auth: McpAuthSettings = McpAuthSettings()
    rate_limit: McpRateLimitSettings = McpRateLimitSettings()


class LocalSpannerSettings(SpannerSettings):
    """The worker's database, on the local emulator unless set."""

    database: str | None = LOCAL_DATABASE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODEGRAPH_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = "forge-codegraph-mcp"
    version: str = "0.1.0"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    # None → enabled everywhere except production.
    docs_enabled: bool | None = None

    server: ServerSettings = ServerSettings()
    logging: AppLoggingSettings = AppLoggingSettings()
    cors: CorsSettings = CorsSettings()
    mcp: McpSettings = McpSettings()
    # The code graph as every app reads it (forge_codegraph_query.settings),
    # its groups at the top level here: CODEGRAPH_SPANNER__DATABASE, ...
    spanner: LocalSpannerSettings = LocalSpannerSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    search: SearchSettings = SearchSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The usual sources and precedence, with .env's references to
        # .env.common resolved.
        return (
            init_settings,
            env_settings,
            EnvFileSettingsSource.replacing(dotenv_settings),
            file_secret_settings,
        )

    @property
    def codegraph(self) -> CodeGraphSettings:
        return CodeGraphSettings(spanner=self.spanner, embedding=self.embedding, search=self.search)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def show_docs(self) -> bool:
        return self.docs_enabled if self.docs_enabled is not None else not self.is_production

    @model_validator(mode="after")
    def _guardrails(self) -> Self:
        if self.spanner.database is None:
            raise ValueError("spanner.database is required: the code graph worker's database")
        if self.spanner.cursor_signing_key is None:
            raise ValueError(
                "spanner.cursor_signing_key is required: the worker's cursor key "
                "(FORGE_CODEGRAPH_CURSOR_SIGNING_KEY in .env.common, which make env generates)"
            )
        if self.is_production:
            if self.debug:
                raise ValueError("debug must be disabled in production")
            if self.server.reload:
                raise ValueError("server.reload must be disabled in production")
            if "*" in self.cors.allow_origins and self.cors.allow_credentials:
                raise ValueError("cors: wildcard origins cannot be combined with credentials")
        if self.server.reload and self.server.workers > 1:
            raise ValueError("server.reload and server.workers > 1 are mutually exclusive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
