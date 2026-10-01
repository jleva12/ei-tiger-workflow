"""The workflows task's settings, from ``HYBRID_WORKFLOWS__*`` (and ``.env``)."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator


class WorkflowsSettings(BaseModel):
    # The admin API, which acts for the member a run acts as: it starts the
    # runs of other workflows (it keeps the workflows). Its service token is
    # FORGE_WORKFLOWS_TOKEN, shared through .env.common. Unset, those steps
    # fail saying so.
    admin_url: str | None = Field(default=None, pattern=r"^https?://[^?#]+$")
    admin_token: SecretStr | None = None
    admin_timeout: float = Field(default=60.0, gt=0)

    # Agent steps' models: the shared model provider configuration's
    # (model_provider.yaml, the file the admin API's assistant reads too),
    # when this names it; its ${NAME} references resolve from the process
    # environment, then from .env. Without it, Gemini, with this API key
    # (FORGE_GOOGLE_API_KEY in .env.common). Neither: agent steps fail saying
    # so. A step's own model wins over the default: agent_model, else the
    # configuration's (Gemini's: gemini-3.5-flash).
    model_provider_config: str | None = None
    google_api_key: SecretStr | None = None
    agent_model: str | None = None
    # The most model calls a step with no limit of its own makes.
    agent_turn_cap: int = Field(default=50, ge=1)
    agent_timeout: float = Field(default=180.0, gt=0)

    # HTTP steps never reach private, loopback or link-local addresses (the
    # worker's own network) unless allowed here; hosts listed are always allowed.
    http_allow_private: bool = False
    http_allowed_hosts: list[str] = Field(default_factory=list)
    http_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # JSONata: how long one evaluation may take, and how deep it may go.
    expression_timeout_ms: int = Field(default=2000, gt=0)
    expression_depth: int = Field(default=300, gt=0)

    # What one run may do: steps run in all, visits of one step (a loop
    # that comes back through it), items of one loop, nested workflows.
    max_steps: int = Field(default=2000, ge=1)
    max_visits_per_step: int = Field(default=500, ge=1)
    max_loop_items: int = Field(default=10_000, ge=1)
    max_depth: int = Field(default=5, ge=1)
    # The most a step's output may be, as JSON, to be kept with the run.
    max_output_bytes: int = Field(default=1024 * 1024, gt=0)

    # How often a run waiting on another workflow looks again.
    poll_seconds: int = Field(default=60, ge=5)
    # Delays shorter than this wait in place; longer ones let the worker go.
    inline_delay_seconds: float = Field(default=20.0, ge=0)

    @field_validator(
        "admin_url", "admin_token", "google_api_key", "model_provider_config", "agent_model", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value
