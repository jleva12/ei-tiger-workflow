# Packages

Shared Python libraries used by the applications in `apps/`: the admin API
(`apps/forge-admin-api`) and the async worker (`apps/forge-async-worker`).
Each is a uv project with a `src/` layout. An app depends on one with a path
source in its `pyproject.toml`
(`[tool.uv.sources] name = { path = "../../packages/python/<name>", editable = true }`)
and its Dockerfile copies the package directory before `uv sync`.

- [`python/common/`](python/common/README.md) (`forge-common`,
  `forge_common`) holds code any Python codebase may share, each subpackage
  behind an extra: `forge_common.adk` (`forge-common[adk]`) has
  `ForgeBaseToolset`, a Google ADK toolset whose tools never raise and cut
  oversized results down, which the admin API's assistant toolsets build on.
  `forge_common.model_provider` (`forge-common[model-provider]`) holds the
  shared `model_provider.yaml` files and their loader, and
  `forge_common.adk.models` (`forge-common[adk-models]`) runs an ADK agent on
  their models, a conversation choosing one per turn: the admin API's
  assistant and ADK workflows' LLM nodes both run on them
  (`make common-check`).
- [`python/jsonata/`](python/jsonata/README.md) (`forge-jsonata`,
  `forge_jsonata`) is the locally maintained JSONata transformation engine
  every ADK workflow expression runs on, with no runtime dependencies and the
  pinned upstream compatibility suite (`make jsonata-check`). The ADK
  workflows task evaluates expressions with it.
- [`python/enhanced-task-framework/`](python/enhanced-task-framework) (`etf`)
  tracks, audits, pauses and recovers async job runs: every job the async
  worker runs, including each ADK workflow run, is one of its runs, which the
  worker's background tasks API reads back for the admin API
  (`make etf-check`).
- `python/tasks/<name>/`: the task packages `apps/forge-async-worker` runs.
  [`task-sdk`](python/tasks/task-sdk/README.md) (`forge-tasks`,
  `forge_tasks`) is the contract between the worker and a task: a task
  package registers its factory under the `forge_async_worker.tasks` entry
  point group and keeps its business logic to itself.
  [`adk-workflows`](python/tasks/adk-workflows/README.md)
  (`forge-task-adk-workflows`, `forge_task_adk_workflows`) is the ADK
  workflows task: it builds an organization's ADK workflows
  (`forge.agent/v1`) into Google ADK graphs and runs them on ADK's graph
  engine, through approvals, human input, waits and restarts, their state in
  ADK sessions. The admin API depends on it too, to build a document before
  a run and to read a run's steps from its session. Their tests run in the
  worker's environment (`make async-worker-check`).

Packages never import application code. Code used by only one application
stays in that application.
