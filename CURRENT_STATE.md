# Current state, pending work and known bugs

As of 2026-10-19, `main` at `470810f`. This page replaces the old review
documents (`CODE_REVIEW_2026-10-16.md`, the integration suite's
`FINDINGS.md` and the code graph's `gap-closure.md`). Keep it current: fix
a bug, cross it off here.

## At a glance

Forge is an organization-scoped platform for building and running AI
agents and workflows:

- Chat agents are built on the Agents canvas and served by a hosted runtime
  (REST/SSE, A2A 1.0/0.3, chat pages, the desktop app).
- ADK workflows are built on the workflow canvas and run by the async worker
  against a MySQL run store.
- Knowledge bases cover documents (vector and text search) and code graphs.
- An admin API and web console sit on top, with an in-app assistant and
  co-pilots.

**Health on 2026-10-19:**

| Suite | Result |
|---|---|
| admin API | 1121 passed, 64 skipped (MySQL/Mongo-only) |
| adk-workflows | 631 passed |
| agent-runtime | 426 passed |
| forge_common | 365 passed |
| mcp-servers | 57 passed |
| async worker | 104 passed |
| pi-agent / task-sdk | 18 / 17 passed |
| web | `tsc` clean; node tests all passing |
| Integration, real MySQL 8 and Redis (`make async-worker-test-integration`) | **33/33 passed** |

No Python suite runs in CI (see [Engineering](#engineering)).

| Where | What |
|---|---|
| `apps/forge-admin-api` | Admin API: organizations, permissions, agents, workflows, the hosted chat-agent runtime, the workflow runtime, the assistant, MCP servers, credentials, knowledge bases. |
| `apps/forge-async-worker` | SAQ worker. Two queues: `adk_workflows` runs workflows (run store in admin MySQL), and `documents` ingests knowledge-base documents. |
| `apps/forge-web` | React console: builders, runs, chats, overview. |
| `apps/forge-desktop` | Forge Agents desktop app (Tauri 2). |
| `apps/forge-codegraph-worker` | Go code-graph ingestion into Spanner. |
| `apps/forge-codegraph-mcp` | Python MCP server exposing the code-graph tools. |
| `packages/python/agent-runtime` | Chat agents on Google ADK: build from canvas JSON, executor, run API, A2A, intake. |
| `packages/python/adk-workflows` | Workflow graph, steps, run store, runs. |
| `packages/python/common` | ADK plugins, bounded patterns, sandbox, process ties, model provider. |
| `packages/python/{task-sdk,mcp-servers,documents,embeddings,codegraph,codegraph-query,jsonata,pi-agent}` | Worker contract, MCP registry, document ingestion and search, embeddings, code-graph tools and query, JSONata, Pi agent. |
| `packages/forge-ui`, `packages/go/code-graph` | Design system and registry; shared Go code-graph modules. |

## Known bugs

All of these were checked against the code on 2026-10-18/19.

| ID | Sev | Area | Bug | Where |
|---|---|---|---|---|
| B-1 | High | Chat runtime | **Clients receive the raw ADK session.** Tool arguments and results, model thoughts, internal `forge:*` state and raw error strings reach chat-page visitors and A2A callers. | `forge_agent_runtime/wire.py`, `server.py`, the A2A event converter |
| B-2 | High | Chat runtime | **No spend limits.** Callers pick any configured model and thinking level. Follow-up and compaction calls aren't metered. There are no request or attachment size caps and no rate limits, including on chat-page visitor-token issuance. | `executor.py` (`FIXED_STATE`), `forge_common/adk/follow_ups.py`, `context_window.py` |
| B-3 | Med–High | Credentials | **Token requests for org API credentials skip the egress guard.** Client-credentials, refresh and code-exchange requests go out on a plain `httpx.AsyncClient`, and `token_url` is only pattern-checked. Client secrets can be POSTed to private or metadata addresses. | `forge_mcp_servers/api_credentials.py` (`http()`), built in `forge_admin/api/server.py` and `forge_task_adk_workflows/task.py` |
| B-4 | Medium | Workflow runs | **Bash steps in OpenShell read output with no budget.** The SDK keeps every chunk until timeout. Code execution has the 1 MiB budget; Bash steps don't. | `support/sandbox_commands.py` (`_command`) |
| B-5 | Low–Med | Workflow runs | **The worker's MCP OAuth refreshes are unguarded.** The worker builds `McpServers` without `client_factory`; the admin API passes one. | `forge_task_adk_workflows/task.py` |
| B-6 | Low–Med | Workflow runs | **Git steps check the host once.** git then resolves DNS and follows redirects itself, so the rebinding and redirect-to-private hole closed for HTTP is still open here. | `graph/factories/github.py` |
| B-7 | Medium | Workflow runs | **No per-organization fairness or admission control.** One FIFO queue; `claim` has no per-org limit; starts aren't rate-limited. | `run_store.py` (`claim`) |
| B-8 | Medium | Chat runtime | **The agent build cache is coarse.** Its key ignores workflow versions and schemas. Every draft autosave builds a new runner. Agents are re-resolved on every request, and the permission policy is reloaded on every call. | `forge_admin/chat_agents/source.py`, `chat_agents/access.py` |
| B-9 | Medium | Chat runtime | **MCP sessions are pooled across all users of an agent.** Stateful MCP servers leak state between users. | ADK `mcp_session_manager`; headers are org-level |
| B-10 | Medium | Chat runtime | **Hosted storage gaps.** Artifacts are kept in memory (lost on restart, not shared across processes). Memory nodes never save, since nothing calls `add_session_to_memory`. | `chat_agents/source.py` (`InMemoryArtifactService`) |
| B-11 | Medium | Chat runtime | **Compaction uses the wrong model.** It summarizes on the default model and measures the window before the agent's model is chosen. | `forge_common/adk/context_window.py` |
| B-12 | Medium | Workflow runs | **Lease and liveness gaps.** Leases use each process's own clock, not the database's. The watchdog clock pauses on suspend. Nothing quarantines a run that keeps killing workers. Lease renewals share the general connection pool. | `run_store.py` (`_now`, `_lease`), `watchdog.py`, `saq_worker.py` |
| B-13 | Medium | Workflow runs | **Nothing is ever deleted.** No retention for runs, events, usage, ADK sessions, run folders, S3 artifacts or sandboxes. `overdue()` scans every paused run every minute. Reloading the session for each answered pause is O(n²). SQL parameters and URLs are stored in run errors. | `run_store.py`, `runs.py`, `workspaces.py` |
| B-14 | Medium* | Chat runtime | **A2A cancel can be overwritten across replicas.** The task store is unversioned under `DefaultRequestHandlerV2`. *Only matters with more than one replica.* | `forge_agent_runtime/a2a.py` |
| B-15 | Low–Med | Chat runtime | **The SSE stream buffer is unbounded per turn.** Fix it by dropping partial events for slow clients, not by back-pressuring the run. | `server.py` (`stream_events`) |
| B-16 | Medium | Workflow runs | **A model 503 on one branch can fail the whole run.** If another branch was mid-POST, the fail-closed rule stops it. LLM nodes don't retry hiccups themselves. | `runs.py`, LLM node factory |
| B-17 | Low–Med | Workflow runs | **A workflow tool inside a sub-agent tool still waits in place.** It holds its worker slot, because only top-level calls pause at a `child_run` gate. | `graph/agent_tools.py` |
| B-18 | Medium | Knowledge | **The search visibility filter grows with document count.** One `$or` clause per document of the tenant, unbenchmarked. | `documents/storage/mongo/store.py`, `pipelines.py` |
| B-19 | Low | Admin API | **Deleting an org leaves data behind.** SQL is committed first, Mongo is cleaned best-effort with no sweeper, and knowledge-base documents, chunks and S3 files are never removed. | `api/routes/organizations.py` |
| B-20 | Low | Chat runtime | **One saved agent placed twice under different rules requires both rules** for its inner agents. It refuses wrongly, never allows wrongly. | `forge_agent_runtime/build.py` (`guarded`) |
| B-21 | Low | Chat runtime | **The standalone server without API keys keys anonymous A2A conversations by `contextId`.** Documented; the hosted runtime has no anonymous A2A. | `a2a.py` |
| B-22 | Design | Chat runtime | **Intake confirmations are the model's claim,** not a verifiable action by the person. | `intake.py` (`confirmed`, `summary_confirmed`) |
| B-23 | Cleanup | Admin API | **A dead anonymous-caller branch** is left in the workflow runtime since public mode was removed. | `adk_workflows/runtime.py` |
| B-24 | Test | Integration | **The frozen-worker scenario errored once under load,** then passed alone. The harness reads the lease deadline just before the worker renews it. | `tests/integration/test_c_lease_fencing.py` |
| B-25 | Low | Infra | **The documents worker isn't set up for Spanner in Compose.** `async-worker-documents` gets neither `SPANNER_EMULATOR_HOST` nor the cloud overlay's credentials. That only matters if the vector store is switched to Spanner under Compose. | `apps/forge-async-worker/compose.yaml` |

## Pending work

### Hardening, grouped into tasks

1. **Client view model** (B-1, B-14, B-15): a projection per channel. Strip thoughts and internal state; show tool results only when a tool opts in. Bound the SSE buffer, and use a versioned A2A task store.
2. **Spend limits and fairness** (B-2, B-7, B-11): a model allowlist per agent and org; meter every LLM call through one sink; body and attachment caps; rate limits; a per-org concurrency cap in `claim`; compaction on the selected model.
3. **Agent builds and hosted persistence** (B-8, B-9, B-10): a compiled build manifest with pinned dependency versions; close a draft's old runner on a new revision; MCP sessions per conversation; a persistent artifact service; save sessions to memory.
4. **Egress gaps** (B-3, B-5, B-6, B-4): guarded clients for credential token requests and the worker's MCP registry; host pinning for git; an output budget for OpenShell Bash.
5. **Leases and liveness** (B-12): the database clock for leases, `CLOCK_BOOTTIME` for the watchdog, quarantine after repeated kills, and a small dedicated engine for claim and renew.
6. **Retention** (B-13, B-19): retention jobs for every growing store, a bounded `overdue()`, a sweeper for org deletion, and `hide_parameters=True` on engines.
7. **LLM node retries** (B-16): retry model hiccups inside LLM nodes, so one branch's 503 can't cut off a sibling's effect.

### Features

**Team generative UI**, Phases 4–5 ([design brief](docs/design/team-generative-ui-components.md)). Phases 1–3 are shipped. Still to build:
- the UI Components settings page, library, JSON editor, preview, version history and publish dialog;
- the `ui_component_writer` helper;
- the rich-answers picker with Base and Team groups;
- the pilot deployment setting;
- general availability.

**Code graph** (the old `gap-closure.md`, G1, G2, G8 and G9 are done):
- G3: callers with override and implements expansion; only impact has it.
- G4: framework and topology edges (routes, handlers, HTTP calls, injection).
- G5: Go, C# and Groovy. Only Java, TypeScript and Python are registered.
- G6: repository, module, package, folder and dependency nodes.
- G7: non-code artifact extractors.
- G10: a local or hash embedding provider; only OpenAI exists today.
- G11: scale evidence (a benchmark run and report).
- The remaining Python resolution phases ([design](apps/forge-codegraph-worker/docs/python-resolution-design.md)): a distribution-to-module map, a deployment-fidelity mode, runtime/test contexts, a 3.14 profile, structured binding evidence, possible-calls queries, framework enrichment, module-level invalidation.
- Storage compaction ([design](apps/forge-codegraph-worker/docs/storage-compaction.md)): not started.

### Engineering

- **CI.** No Python suite runs in CI; `.github/workflows` only checks the forge-ui registry. The integration suite is manual (`make async-worker-test-integration`).
- **Never exercised against real services:** MongoDB Atlas (vector index), Spanner, OAuth providers, the Pi agent, the OpenShell gateway, and model providers. Also never run: the 64 MySQL- and Mongo-only admin tests on the current code.
- **Never reviewed:** the Go ingestion worker (`apps/forge-codegraph-worker`, `packages/go`).
- **Never deployed:** there has been no production deployment or verification yet.
- **Docs debt.** The admin API README doesn't document the newer routes: skills, UI components, API credentials, dashboards, shared widgets, agent access, permission groups, overview, node kinds.
- **Lint debt.** Already present before the review:
  - `adk-workflows/tests/test_agent_tools.py` isn't formatted.
  - `forge_common/adk/toolset.py` has 4 long lines.
  - Agent-runtime tests have 6 mypy errors.
  - Some web tests aren't prettier-formatted.

## Operations

**Migrations.** The head is `0035adk_run_interruptions`. Recent ones:
- `0033exactly_once_starts`: `chat_turn_leases`, `workflow_start_intents`, `intake_requests.version`.
- `0034ui_components`.
- `0035adk_run_interruptions`: `adk_runs.interruptions`.

Run `make admin-migrate` before starting new admin API or worker code; the run store reads the new column.

**Settings added or changed 2026-10-18/19:**

| Setting | Effect |
|---|---|
| `FORGE_ADMIN_AGENT_TURN_WAIT` (default 5 s, max 60) | How long a second message in a conversation waits for the running turn before it's refused (409, or a rejected A2A task). |
| `ASYNC_WORKER_STOP_GRACE_PERIOD` (`.env.compose`, default now `2m`) | Docker stop grace for worker containers. It must cover the 30 s drain plus stopping and writing back a cut-off run. |
| `FORGE_IDEMPOTENCY_KEY` (set by the worker) | Stable for every attempt of one Bash or Pi step execution. Reserved: steps can't define it. |
| `FORGE_ADMIN_AGENT_TOOLS_ALLOW_PRIVATE` / `…_ALLOWED_HOSTS` | Now also govern MCP servers (registration, checks, connections) and the stricter address rule: only globally routable addresses unless allowed. |
| `HYBRID_ADK_WORKFLOWS__HTTP_ALLOW_PRIVATE` / `…_HTTP_ALLOWED_HOSTS` | The same for workflow HTTP and git steps and MCP tools in workflow LLM steps. |
| `FORGE_ADMIN_JWT_SECRET` (else `FORGE_ADMIN_SECRETS_KEY`) | Also derives the key that signs chat-page visitor tokens. With neither set, each process makes its own key, and visitors lose their conversations on restart or across replicas. |
| `FORGE_ADMIN_AGENT_RUNTIME_PUBLIC` | **Removed.** There's no public runtime. Every call needs an org API key or a Forge sign-in; chat pages use signed visitor tokens (`X-Forge-Visitor`). |
| `IT_MYSQL_PORT` / `IT_REDIS_PORT` / `IT_ARGS`, plus `COMPOSE_PROJECT_NAME` | Ports and pytest arguments for the integration suite. Set a project name to run two stacks side by side. |

New HTTP headers:
- `Idempotency-Key`: accepted on workflow starts, console starts and resubmits. Workflow POST and PATCH steps send it too.
- `X-Forge-Visitor`: a chat page's visitor token.

**Invariants to keep:**
- **All outbound traffic goes through the guard.** Tools, workflow HTTP, OpenAPI and MCP use `http_client()`/`GuardedClient` or `mcp_guard`. Configured proxies make HTTP tools fail closed. Across origins, redirects keep only representation headers.
- **Chat-page links are the only anonymous path in,** and every runtime entry point must keep the "organization still exists" check.
- **Hosted chat-agent saves strip carried copies.** They drop `version`, `dependencies` and `ui_components` and force `organization_id`.
- **`run_adk` liveness comes from the run-store lease alone** (SAQ heartbeat 0). `run_job` uses heartbeat 0, a 1800 s timeout and 3 retries. The worker applies its own job settings whatever the enqueuer sent.

**Rolling out document generations:**
1. Add the Atlas vector-index filter fields `run_seq` and `live` before deploying the readers, using the one-off `ensure_schema(update_existing=True, wait=True)`. A normal `ensure-schema` only warns about drift.
2. Drain ingestion. Never mix old and new readers or writers.
3. Re-ingest previously corrupted generations from source.

Spanner needs no DDL for this.

**Local development:**
- The Code explorer's MCP host is allowed in the local `.env` files (`localhost`, `codegraph-mcp`). The `.env.example` files don't have it, so a fresh `make env` blocks the Code explorer until you set `FORGE_ADMIN_AGENT_TOOLS_ALLOWED_HOSTS` and `HYBRID_ADK_WORKFLOWS__HTTP_ALLOWED_HOSTS`.
- Old `make up-all-local` workers keep old code and take SAQ jobs. Check `ps` when a change doesn't seem to take effect.

## Fixed (record)

**Integration findings** (code and tests cite these IDs; all fixed and passing):

| ID | Finding | Fix |
|---|---|---|
| IT-1 | A new member's simultaneous first runs failed on ADK's state-row insert race (REPEATABLE READ) | Retried as transient; ADK sessions at READ COMMITTED |
| IT-2 | An HTTP step cut off mid-request was skipped on resume, and the run reported success | Effectful steps record their start, re-run on resume and fail closed with `may_have_run`; runs never default to success |
| IT-3 | A killed worker's Bash command kept running beside its rerun | `forge_common.processes.Tie` kills the group when the worker dies; leftovers are found by tag on resume |
| IT-4 | An attempt whose lease ran out still wrote notes and usage rows | Writes fenced on the lease |
| IT-5 | That attempt's session append made the new attempt fail | Session appends fenced; a lost lease isn't a failure |
| IT-6 | Parents waiting on child runs held every worker slot | Parents pause at a `child_run` gate; the child's end wakes them |
| IT-7 | A worker draining for shutdown kept taking new runs | `stop_taking` before the drain; `_let_go` queues a job; interruptions are counted apart |
| IT-8 | Workers spun a CPU core while Redis was away | Dequeues back off |

**2026-10-18 review:**
- Runtime identity: callers are bound to their conversations, drafts need `agents:manage`, the entry rule is checked every turn, and build errors are generic.
- Exactly-once workflow starts (an outbox and `Idempotency-Key`) and one turn per conversation.
- Chat-runtime hardening: bounded intake regex, a linear cycle check with size caps, YAML alias and spec-size limits, an egress guard that allows only global addresses, and MCP through the guard.
- Shared agent graphs: toolsets owned by the cached graph, saved agents named by placement, spill files cleaned up, and steps that fail when their tools won't list.
- Workflow cancellation: sandbox create and discard coordinated, recovery fenced on `lease_epoch`, child runs carry `agent_access`, and resubmit recomputes grants.

**2026-10-16 review:** 16 of 17 findings fixed. They covered:
- cached dependency permissions;
- the conversation-audit state leak;
- redirect credential leaks;
- DNS rebinding;
- MCP endpoint changes;
- template regex;
- document jobs' SAQ heartbeat;
- `user:` artifact deletion;
- deleted documents coming back;
- mixed publication;
- Mongo write-concern errors;
- runner cache eviction;
- child skill snapshots;
- org-deletion access;
- autosave on unload;
- refused-save warnings.

The 17th, sandbox output, is still open for Bash steps (B-4).
