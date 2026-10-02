# Packages

Shared Python libraries used by the applications in `apps/`: the admin API
(`apps/forge-admin-api`) and the async worker (`apps/forge-async-worker`);
and the Forge UI design system the web console's components come from
(`forge-ui/`, below them). Each Python library is a `src/`-layout member of the
uv workspace in the root `pyproject.toml`, which also lists it in
`[tool.uv.sources]` (`name = { workspace = true }`), so an app depends on it by
name. Every member shares the root's `uv.lock` and `.venv`, and an app's
Dockerfile copies the package directory before `uv sync --package <app>`.

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
- [`python/task-sdk/`](python/task-sdk/README.md) (`forge-tasks`,
  `forge_tasks`) is the contract between `apps/forge-async-worker` and the
  task packages it runs: a task package registers its factory under the
  `forge_async_worker.tasks` entry point group and keeps its business logic
  to itself.
- [`python/adk-workflows/`](python/adk-workflows/README.md)
  (`forge-task-adk-workflows`, `forge_task_adk_workflows`) is the ADK
  workflows task: it builds an organization's ADK workflows
  (`forge.agent/v1`) into Google ADK graphs and runs them on ADK's graph
  engine, through approvals, human input, waits and retries, their state in
  ADK sessions. Each run is kept in its run store (`run_store`: two tables
  in the admin MySQL), which the admin API starts, lists and acts on and the
  worker runs. The admin API depends on it too, for the run store, to build
  a document before a run and to read a run's steps from its session. Its
  tests, and task-sdk's, run with the worker's checks
  (`make async-worker-check`).

[`forge-ui/`](forge-ui/README.md) is the Forge UI design system: shadcn
primitives on Base UI, Forge composites, the theme and the data and state
libraries, with a demo app (`make forge-ui`, port 5185). Apps don't import
it; they copy its items in with the shadcn CLI
(`npx shadcn@latest add @forge-ui/<item>`) from the shadcn registry it
builds into `forge-ui/registry.json` and `forge-ui/public/r`, read from
this repo on GitHub with a token (`FORGE_UI_TOKEN`), which works for any
project, inside this repo or not. The root `registry.json` only includes
`forge-ui/registry.json`, so `npx shadcn@latest init
jleva12/ei-tiger-workflow/base#main` can start a new app.
Rebuild the registry with a change and commit both (`make
forge-ui-registry`); `make forge-ui-check` and the `Forge UI registry`
workflow fail when they differ. It came from `jleva12/forge-ui` with its
history.

Packages never import application code. Code used by only one application
stays in that application.
