"""Settings, read from the environment and an optional .env file.

Precedence, lowest to highest: the defaults below, .env in the working
directory, the process environment. Every setting is FORGE_ADMIN_<NAME>,
e.g. mysql_host is FORGE_ADMIN_MYSQL_HOST.

Values shared with the other apps live in the repository's .env.common,
which .env references by name, e.g.
FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}; see
forge_admin.env_files.
"""

from functools import lru_cache
from pathlib import Path

from forge_common.logging import LoggingSettings
from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy import URL

from forge_admin.env_files import EnvFileSettingsSource


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FORGE_ADMIN_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Application.
    name: str = "forge-admin"
    # Every router passed to ApiServer is mounted below this prefix.
    api_prefix: str = "/api/v1"
    # Serve /docs and /openapi.json.
    docs_enabled: bool = True
    # Browser origins allowed to call the API, e.g. ["http://localhost:5190"].
    cors_origins: list[str] = []
    # When set, API routes require this value in the X-API-Key header.
    # Health probes stay open.
    api_key: SecretStr | None = None
    # Bearer tokens: API requests identify the user with an
    # "Authorization: Bearer <JWT>" header whose sub is their user ID, signed
    # HS256 with this secret (forge-admin-token mints them). Unset, bearer
    # tokens are refused. At least 32 characters.
    jwt_secret: SecretStr | None = Field(default=None, min_length=32)
    # When set, tokens must carry this iss and aud, and minted ones do.
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    # LOCAL DEVELOPMENT ONLY: treat API requests without a bearer token as
    # this user. The tests use it; never set it in production.
    local_user_id: str | None = Field(default=None, max_length=255, pattern=r"^\S+$")
    # The web console's address, for links to it: the assistant links to a
    # workflow's builder.
    web_url: str = "http://localhost:5190"
    # The async worker's background tasks API (apps/forge-async-worker,
    # forge-async-worker api), which shows each organization the jobs run for it and
    # resubmits, restarts or abandons one: http://127.0.0.1:8104 for make
    # async-worker-api, http://async-worker-api:8094 inside Compose. The token
    # is its HYBRID_API__TOKEN, FORGE_ASYNC_WORKER_TOKEN in .env.common, which
    # make env generates. Unset, background tasks answer that they aren't set up.
    async_worker_url: str | None = Field(default=None, pattern=r"^https?://[^/?#]+/?$")
    async_worker_token: SecretStr | None = None
    # Seconds a call to it may take.
    async_worker_timeout: float = Field(default=10.0, gt=0)
    # The async worker (apps/forge-async-worker): the Redis its SAQ job
    # queues run on, where workflow runs are submitted:
    # redis://127.0.0.1:16389/0 for make async-worker, redis://redis:6379/0
    # inside Compose. Unset, starting a run answers that it isn't set up.
    embedding_redis_url: str | None = Field(default=None, pattern=r"^rediss?://")
    # Where other systems reach this API, e.g. https://forge.example.com: the
    # base of the organization event endpoints the web console shows
    # (<public_url>/hooks/events/<endpoint>/<event type>). Unset, the address
    # each request came in on.
    public_url: str | None = Field(default=None, pattern=r"^https?://[^?#]+$")
    # The largest event body an organization's endpoint accepts, in bytes; kept as
    # MEDIUMTEXT, so at most 16 MiB.
    events_max_bytes: int = Field(default=256 * 1024, gt=0, le=16 * 1024 * 1024)
    # MongoDB, where organizations' workflows and agents are kept as the JSON
    # documents the web builders save: mongodb://…@127.0.0.1:27037/ for make
    # admin-deps, shared mongo inside Compose. Unset, workflows and agents answer
    # that they aren't set up. The URI can carry a password, so it's a secret.
    mongo_uri: SecretStr | None = None
    mongo_database: str = Field(default="forge_admin", pattern=r"^[A-Za-z0-9_-]{1,63}$")
    # Seconds to find a MongoDB server before a request answers 503.
    mongo_timeout: float = Field(default=5.0, gt=0)
    # The largest workflow document saved, in bytes of JSON; MongoDB's own
    # limit is 16 MiB a document.
    workflows_max_bytes: int = Field(default=1024 * 1024, gt=0, le=15 * 1024 * 1024)
    # The largest agent document saved, in bytes of JSON.
    agents_max_bytes: int = Field(default=1024 * 1024, gt=0, le=15 * 1024 * 1024)
    # Workflow runs: the async worker's workflows task calls this API's
    # service routes (/internal/workflows/...) with this token, to run other
    # workflows as the member a run acts as. It is FORGE_WORKFLOWS_TOKEN in
    # .env.common, which make env generates, and the worker's
    # HYBRID_WORKFLOWS__ADMIN_TOKEN. Unset, those routes answer 401 and such
    # steps fail saying so. Runs are submitted to the async worker's Redis
    # (embedding_redis_url).
    workflows_token: SecretStr | None = Field(default=None, min_length=32)
    # The site administrator forge-admin-seed adds to users and gives
    # site:admin: the first user, who can then add everyone else.
    site_admin_id: str | None = Field(default=None, max_length=255, pattern=r"^\S+$")
    site_admin_email: str | None = None
    site_admin_first_name: str | None = None
    site_admin_last_name: str | None = None
    site_admin_msid: str | None = None

    # The assistant: Google ADK agents (forge_admin.agents). The models it may
    # run on: a model_provider.yaml, the file workflows' agent steps read too, e.g.
    # ../../packages/python/common/src/forge_common/model_provider/model_provider.openai.yaml
    # (the image keeps the shared files at that path relative to /app). Its
    # ${NAME} references resolve from the process environment, then from
    # .env. Unset, the assistant runs on Gemini with google_api_key.
    model_provider_config: Path | None = None
    # Without model_provider_config: the Gemini API key, from Google AI
    # Studio; GOOGLE_API_KEY and GEMINI_API_KEY work too. Unset, the assistant
    # answers that it isn't set up.
    google_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "FORGE_ADMIN_GOOGLE_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"
        ),
    )
    # The model the assistant runs on unless a conversation chooses another.
    # With model_provider_config, one of its models (provider/model, or an id
    # only one provider has), by default its default; without, a Gemini
    # model, by default gemini-3.5-flash.
    agent_model: str | None = None
    # The models a conversation may switch to: the web console's model
    # section lists them (GET /agents/apps/forge/models) and sends its choice
    # as the session's "model" state. Anything else runs on agent_model. With
    # model_provider_config, which of its models (all when empty); without,
    # other Gemini models.
    agent_models: list[str] = []
    # What the assistant has on each screen: a YAML file in the shape of
    # agents/screens.yaml, which is used when this is unset.
    agent_screens: Path | None = None

    # HTTP listener. The container listens on 0.0.0.0:8091.
    host: str = "127.0.0.1"
    port: int = Field(default=8101, ge=1, le=65535)
    # Restart on source changes; local development only.
    reload: bool = False
    # Structured logs (forge_common.logging): FORGE_ADMIN_LOGGING__LEVEL,
    # __FORMAT (json or console; unset, console on a terminal and json
    # otherwise), __ACCESS_LOG, __ACCESS_LOG_EXCLUDE_PATHS and __LEVELS.
    logging: LoggingSettings = LoggingSettings()

    # MySQL. The password has no default, so a deployment never falls back
    # to a local credential.
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "forge_admin"
    mysql_user: str = "forge_admin"
    mysql_password: SecretStr
    mysql_pool_size: int = Field(default=5, ge=1)
    mysql_max_overflow: int = Field(default=10, ge=0)
    # Seconds before a pooled connection is replaced; keep it below the
    # server's wait_timeout.
    mysql_pool_recycle: int = Field(default=1800, ge=1)
    mysql_connect_timeout: int = Field(default=10, ge=1)

    # Apply pending Alembic migrations before serving.
    migrate_on_start: bool = True
    # Seconds MySQL has to answer /health/ready before the probe reports 503.
    ready_timeout: float = Field(default=2.0, gt=0)

    @field_validator(
        "agent_model",
        "agent_screens",
        "api_key",
        "async_worker_url",
        "async_worker_token",
        "embedding_redis_url",
        "google_api_key",
        "jwt_secret",
        "jwt_issuer",
        "jwt_audience",
        "local_user_id",
        "model_provider_config",
        "mongo_uri",
        "public_url",
        "site_admin_id",
        "site_admin_email",
        "site_admin_first_name",
        "site_admin_last_name",
        "site_admin_msid",
        "workflows_token",
        mode="before",
    )
    @classmethod
    def _blank_disables(cls, value: object) -> object:
        # FORGE_ADMIN_API_KEY= (empty) means no key, not an empty key.
        return None if value == "" else value

    @field_validator("mongo_uri")
    @classmethod
    def _mongo_uri(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("mongodb://", "mongodb+srv://")
        ):
            raise ValueError("mongo_uri must start with mongodb:// or mongodb+srv://")
        return value

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
    def database_url(self) -> URL:
        # URL.create escapes the credentials, so any password characters work.
        return URL.create(
            "mysql+aiomysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment, which type checkers can't see.
    return Settings()  # pyright: ignore[reportCallIssue]
