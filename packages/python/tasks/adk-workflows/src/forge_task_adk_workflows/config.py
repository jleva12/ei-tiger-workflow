"""The ADK workflows task's settings, from ``HYBRID_ADK_WORKFLOWS__*`` (and ``.env``).

Its own section, apart from Forge workflows' ``HYBRID_WORKFLOWS__*``: the two
tasks share nothing but the worker.
"""

from __future__ import annotations

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
    # (model_provider.yaml, the file the admin API's assistant and Forge
    # workflows read too), when this names it; its ${NAME} references resolve
    # from the process environment, then from .env. Without it, Gemini, with
    # this API key (FORGE_GOOGLE_API_KEY in .env.common). Neither: LLM nodes
    # fail the run saying so. A node's own model wins over the default:
    # default_model, else the configuration's (Gemini's: gemini-3.5-flash).
    model_provider_config: str | None = None
    google_api_key: SecretStr | None = None
    default_model: str | None = None
    # Seconds one model call may take.
    model_timeout: float = Field(default=180.0, gt=0)

    # HTTP nodes never reach private, loopback or link-local addresses (the
    # worker's own network) unless allowed here; hosts listed are always allowed.
    http_allow_private: bool = False
    http_allowed_hosts: list[str] = Field(default_factory=list)
    http_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # JSONata: how long one evaluation may take, and how deep it may go.
    expression_timeout_ms: int = Field(default=2000, gt=0)
    expression_depth: int = Field(default=300, gt=0)

    # The most items one loop goes through, whatever it allows.
    max_loop_items: int = Field(default=10_000, ge=1)
    # Delays up to this wait in place; longer ones pause the run, and let the
    # worker go until the time comes.
    inline_delay_seconds: float = Field(default=20.0, ge=0)

    # The most seconds a run may go on at a time (from its start, or from an
    # answer, to its end or its next pause) before it fails.
    run_timeout: float = Field(default=3600.0, gt=0)

    @field_validator("session_database_url", "google_api_key", "model_provider_config", "default_model", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value
