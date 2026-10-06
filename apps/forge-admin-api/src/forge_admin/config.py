"""Settings, read from the environment and an optional .env file.

Precedence, lowest to highest: the defaults below, .env in the working
directory, the process environment. Every setting is FORGE_ADMIN_<NAME>,
e.g. mysql_host is FORGE_ADMIN_MYSQL_HOST.

Values shared with the other apps live in the repository's .env.common,
which .env references by name, e.g.
FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}; see
forge_admin.env_files.
"""

from functools import lru_cache
from pathlib import Path

from forge_common.logging import LoggingSettings
from forge_embeddings.config import EmbeddingSettings, RerankSettings
from forge_task_documents.retrieval.service import SearchConfig
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
    # The token claim listing the company groups (from its directory) the
    # user is in: a list of names, e.g. ["Forge Admins"]. Permissions linked
    # to a group (Permissions > Link permissions) are theirs on the whole site.
    jwt_groups_claim: str = Field(default="groups", min_length=1)
    # LOCAL DEVELOPMENT ONLY: treat API requests without a bearer token as
    # this user. The tests use it; never set it in production.
    local_user_id: str | None = Field(default=None, max_length=255, pattern=r"^\S+$")
    # LOCAL DEVELOPMENT ONLY: the company groups that user is in.
    local_user_groups: list[str] = []
    # The web console's address, for links to its pages.
    web_url: str = "http://localhost:5190"
    # The async worker (apps/forge-async-worker): the Redis its SAQ job
    # queues run on, where the jobs that take ADK workflow runs are queued
    # (the runs themselves are kept in MySQL, below):
    # redis://127.0.0.1:16389/0 for make async-worker, redis://redis:6379/0
    # inside Compose. Unset, starting or carrying on a run answers that it
    # isn't set up.
    embedding_redis_url: str | None = Field(default=None, pattern=r"^rediss?://")
    # MongoDB, where organizations' agents (their ADK workflows) are kept as
    # the JSON documents the web builder saves: mongodb://…@127.0.0.1:27037/
    # for make admin-deps, shared mongo inside Compose. Unset, agents answer
    # that they aren't set up. The URI can carry a password, so it's a secret.
    mongo_uri: SecretStr | None = None
    mongo_database: str = Field(default="forge_admin", pattern=r"^[A-Za-z0-9_-]{1,63}$")
    # Seconds to find a MongoDB server before a request answers 503.
    mongo_timeout: float = Field(default=5.0, gt=0)
    # The largest agent document saved, in bytes of JSON; MongoDB's own
    # limit is 16 MiB a document.
    agents_max_bytes: int = Field(default=1024 * 1024, gt=0, le=15 * 1024 * 1024)
    # The site administrator forge-admin-seed adds to users and gives
    # site:admin: the first user, who can then add everyone else.
    site_admin_id: str | None = Field(default=None, max_length=255, pattern=r"^\S+$")
    site_admin_email: str | None = None
    site_admin_first_name: str | None = None
    site_admin_last_name: str | None = None
    site_admin_msid: str | None = None
    # The key MCP servers' credentials (API keys, tokens, OAuth clients and
    # grants) are encrypted with in MySQL: at least 32 characters, e.g.
    # openssl rand -hex 32, which make env generates into .env. Changing it
    # loses every saved credential. Unset, MCP servers answer that they
    # aren't set up.
    secrets_key: SecretStr | None = Field(default=None, min_length=32)
    # Seconds the admin API waits for an MCP server, or its OAuth
    # authorization server, to answer.
    mcp_timeout: float = Field(default=15.0, gt=0, le=120)

    # Knowledge bases (forge_admin.knowledge): an organization's named sets of
    # documents its chat agents search. Uploads are stored in this S3 bucket,
    # where the async worker's documents task reads them back, parses, chunks
    # and embeds them into MongoDB Atlas (the documents queue, on
    # embedding_redis_url). Unset, uploads answer that they aren't set up.
    documents_bucket: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
    )
    # The largest upload accepted, in bytes; the worker's limit is 100 MiB.
    documents_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # The S3 service: an S3-compatible endpoint such as the local RustFS
    # (knowledge-s3), addressed path-style; unset for AWS. Unset credentials
    # fall back to boto3's own (AWS_* variables, an instance role).
    s3_endpoint_url: str | None = Field(default=None, pattern=r"^https?://")
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    # Create the bucket on first use when it's missing: for local
    # development; provision it in production.
    s3_create_bucket: bool = False
    # Where the worker keeps the knowledge bases' chunks and their vectors:
    # this database of the MongoDB Atlas at mongo_uri, which must be the
    # worker's HYBRID_MONGO__URI / HYBRID_MONGO__DATABASE. Searching a
    # knowledge base (its search route, chat agents' knowledge base tools)
    # reads it; without mongo_uri, searching answers that it isn't set up.
    # FORGE_VECTOR_STORE=spanner (with FORGE_VECTOR_SPANNER_DATABASE) reads
    # the worker's Spanner database instead, as every reader and writer must.
    knowledge_database: str = Field(
        default="forge_knowledge", pattern=r"^[A-Za-z0-9_-]{1,63}$"
    )
    # How a search's question is embedded: the same provider, model and
    # dimensions as the worker's HYBRID_EMBEDDING__*, or nothing matches.
    # FORGE_ADMIN_KNOWLEDGE_EMBEDDING__API_KEY (else OPENAI_API_KEY),
    # __DOCUMENT_MODEL (text-embedding-3-large), __DIMENSIONS (1024).
    knowledge_embedding: EmbeddingSettings = EmbeddingSettings()
    # How a search's candidates are reordered: the worker's HYBRID_RERANK__*,
    # so chat agents and workflows find the same passages. "none" (the
    # default) keeps the fused order; FORGE_ADMIN_KNOWLEDGE_RERANK__PROVIDER=
    # voyage with __API_KEY (else a Voyage embedder's key) reranks them.
    knowledge_rerank: RerankSettings = RerankSettings()
    # How a search ranks and filters: the worker's HYBRID_DOCUMENTS__SEARCH__*,
    # e.g. FORGE_ADMIN_KNOWLEDGE_SEARCH__MIN_SIMILARITY (0.2), the least
    # relevance a passage needs (none: a question the documents don't answer
    # still finds the nearest passages), or __MIN_RERANK_SCORE with a reranker.
    knowledge_search: SearchConfig = SearchConfig()

    # The assistant: Google ADK agents (forge_admin.assistant). The models it may
    # run on: a model_provider.yaml, the file ADK workflows' LLM nodes read too, e.g.
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
    # assistant/screens.yaml, which is used when this is unset.
    agent_screens: Path | None = None

    # Organizations' chat agents, run by ID at {api_prefix}/runtime
    # (forge_agent_runtime's run API: run_sse and sessions). Public: anyone
    # with an agent's or workflow's ID can call it, spending its model and
    # calling its tools; set false to ask for an organization's API key or a
    # Forge sign-in, holding agents:run where it is.
    agent_runtime_public: bool = True
    # How many built chat agents the runtime keeps ready.
    agent_runtime_cache: int = Field(default=64, ge=1, le=4096)
    # Where callers reach this API (https://forge.example.com), for the A2A
    # cards of chat agents ({api_prefix}/runtime/a2a/{agent}), when that isn't
    # where requests come to (behind a proxy). Unset, each request's own.
    agent_runtime_url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    # The most seconds a caller of the workflow runtime may wait for a run to
    # pause or end in one request ({api_prefix}/runtime/workflows, "wait"),
    # and an A2A task follows its run before answering it's still working.
    workflow_runtime_wait: float = Field(default=60.0, ge=0, le=300)
    # What chat agents' HTTP tools may reach besides the internet: private
    # networks (local development only), or these hosts.
    agent_tools_allow_private: bool = False
    agent_tools_allowed_hosts: list[str] = []
    # Standalone agent projects (the builder's Generate standalone agent): where
    # their runtime comes from. wheels: the wheels in starter_wheels are copied
    # into each project's vendor/, until the runtime is on PyPI; pypi:<version>
    # pins that version instead.
    starter_runtime: str = Field(
        default="wheels", pattern=r"^(wheels|pypi:[0-9][0-9A-Za-z.+-]*)$"
    )
    # The wheels (forge_agent_runtime, forge_common, forge_jsonata). Unset, the
    # runtime package's dist folder, which make starter-wheels fills; the image
    # builds its own.
    starter_wheels: Path | None = None

    # HTTP listener. The container listens on 0.0.0.0:8091.
    host: str = "127.0.0.1"
    port: int = Field(default=8101, ge=1, le=65535)
    # Restart on source changes; local development only.
    reload: bool = False
    # Structured logs (forge_common.logging): FORGE_ADMIN_LOGGING__LEVEL,
    # __FORMAT (json or console; unset, console on a terminal and json
    # otherwise), __ACCESS_LOG, __ACCESS_LOG_EXCLUDE_PATHS and __LEVELS.
    logging: LoggingSettings = LoggingSettings()

    # MySQL, which keeps the organizations, people and access, the ADK
    # workflow runs and their ADK sessions, and the assistant's conversations.
    # The password has no default, so a deployment never falls back to a
    # local credential.
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
        "starter_wheels",
        "api_key",
        "documents_bucket",
        "embedding_redis_url",
        "google_api_key",
        "jwt_secret",
        "jwt_issuer",
        "jwt_audience",
        "local_user_id",
        "model_provider_config",
        "mongo_uri",
        "s3_access_key_id",
        "s3_endpoint_url",
        "s3_secret_access_key",
        "secrets_key",
        "site_admin_id",
        "site_admin_email",
        "site_admin_first_name",
        "site_admin_last_name",
        "site_admin_msid",
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
