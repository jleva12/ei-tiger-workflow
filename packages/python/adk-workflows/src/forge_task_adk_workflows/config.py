"""The ADK workflows task's settings, from ``HYBRID_ADK_WORKFLOWS__*`` (and ``.env``)."""

from __future__ import annotations

from typing import Literal

from forge_codegraph_query import CodeGraphSettings
from pydantic import BaseModel, Field, SecretStr, field_validator


class AdkWorkflowsSettings(BaseModel):
    # Where runs keep their ADK sessions (ADK's DatabaseSessionService, which
    # creates its tables on first use): the admin MySQL, whose sessions the
    # admin API reads for a run's steps, e.g.
    # mysql+aiomysql://user:password@127.0.0.1:13326/forge_admin. Any async
    # SQLAlchemy URL works (sqlite+aiosqlite:///sessions.db for a trial).
    # Unset, runs fail saying so.
    session_database_url: SecretStr | None = None

    # LLM nodes' models: the shared model provider configuration's
    # (model_provider.yaml, the file the admin API's assistant reads too),
    # when this names it; its ${NAME} references resolve from the process
    # environment, then from .env. Without it, Gemini, with
    # this API key (FORGE_GOOGLE_API_KEY in .env.common). Neither: LLM nodes
    # fail the run saying so. A node's own model wins over the default:
    # default_model, else the configuration's (Gemini's: gemini-3.5-flash).
    model_provider_config: str | None = None
    google_api_key: SecretStr | None = None
    default_model: str | None = None
    # Seconds one model call may take.
    model_timeout: float = Field(default=180.0, gt=0)
    # What each LLM agent's next request holds, and what its model may read
    # (model_provider.yaml's contextWindow less maxTokens), is kept in the
    # run's state as forge:context:<agent>. An agent whose request reaches this
    # share of it is compacted in the middle of its step: its earlier steps
    # summarized, its task and last step kept. Off: counted, compacted only
    # when a tool asks (forge_common.adk.context_window.request_compaction).
    context_compaction: bool = True
    context_compaction_threshold: float = Field(default=0.8, gt=0, le=1)

    # HTTP nodes never reach private, loopback or link-local addresses (the
    # worker's own network) unless allowed here; hosts listed are always allowed.
    http_allow_private: bool = False
    http_allowed_hosts: list[str] = Field(default_factory=list)
    http_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # JSONata: how long one evaluation may take, and how deep it may go; and
    # how long a condition read for each item of a list (a filter's) may take
    # in all.
    expression_timeout_ms: int = Field(default=2000, gt=0)
    expression_depth: int = Field(default=300, gt=0)
    expression_list_timeout_ms: int = Field(default=30_000, gt=0)

    # The most items one loop goes through, whatever it allows.
    max_loop_items: int = Field(default=10_000, ge=1)
    # Delays up to this wait in place; longer ones pause the run, and let the
    # worker go until the time comes.
    inline_delay_seconds: float = Field(default=20.0, ge=0)

    # The most seconds a run may go on at a time (from its start, or from an
    # answer, to its end or its next pause) before it fails.
    run_timeout: float = Field(default=3600.0, gt=0)
    # The most seconds one attempt of a run may take in all: its steps, the
    # answers it carries on with, opening its session and closing what it
    # opened. Past it the worker stops the attempt, and the run is tried again
    # later (it fails once out of attempts). Unset, twice run_timeout.
    attempt_timeout: float | None = Field(default=None, gt=0)

    # Where runs' ADK artifacts are kept, among them the files a run starts
    # with, which the admin API saves there (its FORGE_ADMIN_WORKFLOW_ARTIFACTS,
    # the same place): s3://bucket/prefix on the S3 service below, or a folder
    # both reach. Unset, a run started with files fails saying so.
    artifacts: str | None = None
    # The S3 service: an S3-compatible endpoint such as the local RustFS
    # (knowledge-s3); unset for AWS. Unset credentials fall back to boto3's
    # own (AWS_* variables, an instance role).
    s3_endpoint_url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None

    # LLM agents' tools (the Agents builder's: MCP servers, knowledge bases,
    # HTTP tools, OpenAPI specs, agents, workflows). The key the organization's
    # MCP servers' credentials are encrypted with: the admin API's
    # FORGE_ADMIN_SECRETS_KEY (make env copies it). Unset, a tool using one of
    # them fails its step saying so.
    secrets_key: SecretStr | None = None
    # Seconds an MCP server's (or a credential's) authorization server has to
    # answer.
    mcp_timeout: float = Field(default=15.0, gt=0)
    # The variables of this worker's environment that ${NAME} in LLM agents'
    # HTTP and MCP tools (their URLs and headers) and OpenAPI spec URLs may
    # read: none by default, so a workflow can never send the worker's own
    # secrets anywhere. Put API keys and tokens in the organization's
    # credentials instead (the same key decrypts them).
    tool_environment: list[str] = Field(default_factory=list)
    # Where LLM agents' Code execution tools run the code they send: off (a
    # step whose agent has one fails), or local: ADK's UnsafeLocalCodeExecutor,
    # a new Python process on this worker's machine for each call, with the
    # worker's environment, files and network. Local development only.
    code_execution: Literal["off", "local"] = "off"
    # Seconds a workflow an LLM agent calls as a tool is waited for before
    # the tool answers where it got to.
    workflow_tool_wait: float = Field(default=120.0, gt=0)
    # System design knowledge bases' search: the code graph the worker
    # (apps/forge-codegraph-worker) writes to Spanner, read as every app reads
    # it (forge_codegraph_query): HYBRID_ADK_WORKFLOWS__CODEGRAPH__SPANNER__
    # DATABASE and __CURSOR_SIGNING_KEY (.env.common's FORGE_CODEGRAPH_*),
    # __EMBEDDING__* (FORGE_EMBEDDING_*), __SEARCH__ENHANCE_QUERY. Unset, a
    # knowledge base tool searching one fails its step saying so. The Spanner
    # client finds the emulator in SPANNER_EMULATOR_HOST.
    codegraph: CodeGraphSettings = CodeGraphSettings()

    @field_validator(
        "session_database_url",
        "google_api_key",
        "model_provider_config",
        "default_model",
        "secrets_key",
        "artifacts",
        "s3_endpoint_url",
        "s3_region",
        "s3_access_key_id",
        "s3_secret_access_key",
        "attempt_timeout",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def attempt_seconds(self) -> float:
        """:return: The most seconds one attempt of a run may take (``attempt_timeout``)."""
        return self.attempt_timeout or 2 * self.run_timeout
