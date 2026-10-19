"""The ADK workflows task's settings, from ``HYBRID_ADK_WORKFLOWS__*`` (and ``.env``)."""

from __future__ import annotations

from typing import Literal

from forge_codegraph_query import CodeGraphSettings
from forge_common.sandbox import OpenShellSettings, SandboxMode
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

    # Where runs of workflows whose start allows a file system get a folder
    # of their own (forge_task_adk_workflows.workspaces):
    # <workspaces>/<organization>/<workflow>/<run>, which its steps and agents
    # read as state.workspace. A mounted volume every worker of the queue
    # reaches (a relative path is from the worker's folder); made when it
    # isn't there. Unset, such a run fails saying so.
    workspaces: str | None = None
    # Where Bash steps run their commands: off (a run with one fails before it
    # starts); local: bash on this worker's machine, as the worker's user,
    # starting in a folder of the run's own, with its network and whatever
    # its user may read and write (the command gets PATH, HOME, the locale and
    # the variables tool_environment lists, not the rest of the worker's
    # environment), not sandboxed, so only for workflows whose authors you
    # trust with this machine; or openshell: bash in a new sandbox for each
    # command on the OpenShell gateway in openshell (FORGE_OPENSHELL_*), the
    # run's folder copied in and its changes copied back
    # (forge_task_adk_workflows.support.sandbox_commands). What it writes to
    # stdout and stderr is kept up to this many bytes each (the end of it).
    commands: Literal["off", "local", "openshell"] = "off"
    command_max_output_bytes: int = Field(default=1024 * 1024, gt=0)
    # openshell's sandboxes for Bash steps: their image, which needs bash and
    # tar (empty: openshell's image, else the gateway's default); their policy
    # file (OpenShell's SandboxPolicy, YAML or JSON; empty: openshell's
    # policy, else no network, the system read-only, /sandbox and /tmp
    # writable); the hosts any program in them may reach besides (host or
    # host:port, 443 by default; *.example.com for a domain's), e.g.
    # ["github.com"] for git clone over HTTPS; and the most the run's folder
    # may hold, copied in and back out.
    command_image: str = ""
    command_policy: str = ""
    command_allowed_hosts: list[str] = Field(default_factory=list)
    command_max_folder_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)

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
    # read, and Bash steps' commands get: none by default, so a workflow can
    # never send the worker's own secrets anywhere. Put API keys and tokens
    # in the organization's credentials instead (the same key decrypts them).
    tool_environment: list[str] = Field(default_factory=list)
    # Where LLM agents' Code execution tools (and skills' scripts) run the
    # code they send, as .env.common's FORGE_AGENT_SANDBOX_MODE says: off (a
    # step whose agent has one fails); local: ADK's UnsafeLocalCodeExecutor, a
    # new Python process on this worker's machine for each call, with the
    # worker's environment, files and network (local development only); or
    # openshell: a new sandbox for each call on the OpenShell gateway in
    # openshell (FORGE_OPENSHELL_*), confined by its policy, then deleted.
    agent_sandbox_mode: SandboxMode = "off"
    openshell: OpenShellSettings = OpenShellSettings()
    # Pi agent steps: pi, the coding agent (pi.dev, forge_pi_agent), run on
    # this worker's machine in the run's folder, with the built-in tools each
    # step allows, on the steps' models: off (such a step fails) or local. Its
    # bash tool runs any command as the worker's user. How pi is started (a
    # command line: pi on PATH, or e.g. "npx -y @earendil-works/pi-coding-agent"),
    # and the most seconds a step may take, whatever its own timeout.
    pi_agent: Literal["off", "local"] = "off"
    pi_command: str = Field(default="pi", min_length=1)
    pi_max_seconds: float = Field(default=3600.0, gt=0)
    # Seconds a workflow an LLM agent calls as a tool is waited for before
    # the tool answers where it got to, when its Workflow tool doesn't say
    # ("Wait up to"); and the most it may say: .env.common's
    # FORGE_AGENT_WORKFLOW_WAIT_MAX, which chat agents (the admin API) share.
    workflow_tool_wait: float = Field(default=120.0, gt=0)
    workflow_tool_wait_max: float = Field(default=900.0, gt=0)
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
        "workspaces",
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
