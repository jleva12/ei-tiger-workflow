# forge-async-worker

A generic async task worker for this monorepo: app `apps/forge-async-worker`,
package `forge-async-worker`, module `forge_async_worker`, on Python 3.13 with uv.
It runs the jobs of every installed **task package** off SAQ queues on Redis, and
every job it runs is a run tracked by the
[enhanced task framework](../../packages/python/enhanced-task-framework) (ETF):
its status, failures, result and audit trail are stored, a run cut off by a
crash or a shutdown is restarted, and an operator can stop, restart or decide
it. The worker holds no business logic: that lives in the task packages.

| Task package | Task | What it does |
|---|---|---|
| [`packages/python/tasks/workflows`](../../packages/python/tasks/workflows/README.md) | `workflows` | Runs organizations' workflows from the web console's builder: agents, approvals, HTTP calls, transforms, delays, other workflows, logic and loops, through waits and restarts |
| [`packages/python/tasks/adk-workflows`](../../packages/python/tasks/adk-workflows/README.md) | `adk_workflows` | Runs organizations' ADK workflows (the ADK workflows page) on Google ADK's graph engine, their state in ADK sessions in the admin MySQL, through approvals, human input and delays |

It builds on [`forge-tasks`](../../packages/python/tasks/task-sdk/README.md)
(the contract between the worker and a task). More task types plug in the
same way ([Adding a task type](#adding-a-task-type)).

```
 admin API / CLI /    ┌──────── forge-async-worker (generic) ────────┐
 webhook / cron  ───► │ SAQ queue per task type ─► run_job           │
 enqueue("run_job",   │   ─► ETF run "<task>.<kind>" (tracked,       │ ───► forge_task_workflows
   spec=JobSpec)      │      leased, audited, re-driven) ─► the job  │      <your task package>
                      │                                              │
                      │ retries · locks · heartbeat · schedules ·    │      (entry point
                      │ upkeep (recover orphaned runs, expire        │       forge_async_worker.tasks)
                      │ approvals) · health check · CLI              │
                      └──────────────────────────────────────────────┘
```

## Layout

```
src/forge_async_worker/
  saq_worker.py   run_job (every job), redrive_run, resume_run and decide_run (a waiting run
                  carried on), maintain (the ETF upkeep cron), schedules as cron jobs, one SAQ
                  worker per queue, graceful stop, health check
  job_control.py  EtfJobControl: a job's kept state, approvals and waits, over its ETF run
  etf_jobs.py     each (task, job kind) as an ETF job; a SAQ delivery as an ETF launch, labelled with its
                  task type, kind and tenant (the organization); retryable failures
  api.py          the background tasks API: an organization's tasks, one task's attempts and audit trail, and
                  resubmit / restart / abandon / decide (views.py shapes what it answers)
  queue.py        SaqJobQueue: JobSpecs to the generic run_job, on the queue of their task type
  runtime.py      build_runtime(): the installed task packages, built (entry points)
  config.py       WorkerSettings: the shared settings (forge_tasks.CoreSettings), HYBRID_ETF__*, HYBRID_API__*
                  and HYBRID_LOGGING__*
  cli.py          forge-async-worker: worker, tasks, ensure-schema, prepare, run, and each task package's commands
tests/                    the SAQ job, retries, re-drives and upkeep, with toy tasks on in-memory stores; the
                          worker on a real Redis. The task packages test their own jobs
Dockerfile                uv-built image with the bundled task packages and what their prepare() downloads
compose.yaml              async-worker-workflows, async-worker-adk-workflows and async-worker-api
../../compose.infrastructure.yaml  Shared Atlas MongoDB and Redis (SAQ database 0)
.env.example              settings template; make env copies it to .env
pyproject.toml, uv.lock   dependencies (the bundled task packages are the workflows and adk-workflows extras) and the lockfile
```

## How a job runs

Other services submit jobs by name, with no code from this package: on the queue
named after the task type, `enqueue("run_job", spec={"task_type", "kind",
"payload"}, key=...)`, where a key already queued or running isn't queued again.
The worker runs every job under its own timeout (none), heartbeat and retries,
whatever it was enqueued with, so a submitter can't give a backfill SAQ's
10-second default.

`run_job` takes the job's Redis lock (from its `lock_key`, if any), then launches
the delivery as an ETF run of the job `<task>.<kind>` (`workflows.run`), whose
one step runs the task's job. A SAQ delivery is one ETF job instance, identified
by its queue, key and enqueue time: SAQ's retries of a delivery continue that
instance's history, while every cron tick and every re-submission is an instance
of its own. A duplicate delivery of a completed run returns its stored result
without running the job again.

| Outcome | What happens |
|---|---|
| the job returns `JobResult` | the run completes with the result stored on its step; the SAQ job's result carries `run_id`. `followups` are enqueued before the run completes |
| raises `TransientError` (network, 429/5xx, Mongo failover) | the run fails *retryable*; SAQ retries the delivery with our backoff (30 s → 30 min cap, ±25% jitter, 10 retries), freeing the slot meanwhile, and the retry restarts the run as its next attempt |
| raises any other `TaskError`, or a bad payload | `failed`: the run fails permanently with the reason, no retry |
| raises anything else (a bug) | the run and the SAQ job fail with the error, no retry; an operator can restart the run |
| unknown task type or job kind | `failed`, no run, no retry |
| the job's lock is held | re-queued as a new job 30 s out, without spending a retry or starting a run |
| the worker is stopped mid-job | SAQ re-queues it after the grace period; its redelivery restarts the cut-off run |
| the worker dies | SAQ's sweeper retries the job once its heartbeat (touched every 30 s) is 2 min old, and that restarts the run; should the SAQ job itself be lost, the upkeep sweep finds the run (untouched for `HYBRID_ETF__STALE_AFTER_SECONDS`, its lease free), recovers it and queues its re-drive (`redrive_run`) |
| a run waits for a person, or is paused or stopped | the delivery is acknowledged (`skipped`, with the run's status); the run moves on through the operator |

A spec may also carry `tenant_id` (the organization it's for), `labels` (names the run
carries, to find it by: a workflow run's `workflow`) and `requested_by`
(`{"id", "display_name"}`, the person who asked for it, recorded as the run's
requester; the worker itself otherwise).

### Waits and approvals

A job can outlast its worker. While it runs, `forge_tasks.control`
(`current_control()`) gives it the task framework's run (`job_control.py`):

- **State:** `keep(key, value)` and `checkpoint()` keep what it has done with
  the run, and `load(key)` reads it back in any later attempt (a restart, a
  resume, a redelivery).
- **Approvals:** `approval(key=…, reason=…)` pauses the run at a gate
  (`AWAITING_VALIDATION`) and lets the worker go. A decision through the API
  (below) queues `decide_run` on the task's queue; the worker that runs it
  resumes the run with the decision, which the job reads from the same call.
  Approving is the gate's APPROVE; rejecting is an OVERRIDE that changes
  nothing, so the run carries on down its rejected way rather than stopping.
  Each approval keeps its decision, however many gates a run passes. A
  timeout queues a rejection for its deadline.
- **Waiting for a time:** `wait_until(when, reason=…)` stops the run
  (`STOPPED`, marked as waiting, checkpoint kept) and schedules `resume_run`
  for then; a stopped run that isn't waiting (someone stopped it) isn't
  resumed.
- **Other runs:** `find_run(labels)` reads another run by its labels, e.g.
  the child a workflow step started.

Outside the worker (tests, the CLI) the same calls work in memory
(`LocalJobControl`), and a wait ends the job.

Every queue's worker also runs the ETF upkeep as a cron job (`maintain`,
`HYBRID_ETF__MAINTENANCE_CRON`, every 5 minutes by default): recover orphaned
runs and queue their re-drive, and expire approvals past their deadline. The
runs, steps and audit trail live in `HYBRID_ETF__DATABASE` (`forge_tasks`) on
the worker's MongoDB; `HYBRID_ETF__STORE=memory` keeps them in the process.

Jobs are coroutines, so each queue runs `--concurrency` of them at once (default
4) on the worker's event loop; the blocking steps of the task packages run in
threads. The per-process limits of the tasks are shared by every job in the
process.

## Background tasks API

`forge-async-worker api` (`make async-worker-api`, the `async-worker-api`
service in Compose) serves what the task framework recorded, for the admin
API, which shows each organization its background tasks in the web console and checks
who may act ([its README](../forge-admin-api/README.md#background-tasks)).
Every request needs `HYBRID_API__TOKEN` as its bearer token
(`FORGE_ASYNC_WORKER_TOKEN` in `.env.common`, which `make env` generates); the
API won't start without one.

| Route | What |
|---|---|
| `GET /v1/tasks?tenant=&task_type=&exclude_task_type=&status=&label=&limit=&offset=` | Tasks, newest first, with the `total`: each one's job, description, status, attempts, times, latest failure, and whether it waits (`waiting_until`, `waiting_reason`, `awaiting_approval`). `label=name:value`, repeated, keeps tasks with all of those labels; `exclude_task_type`, repeated, leaves those types out |
| `GET /v1/tasks/{id}` | One task: its input (`payload`), labels, SAQ delivery, requester, result, every attempt with its failures (type, message, stack trace, category) and steps, the audit trail, the `actions` its state allows, and the `approval` it waits for (its `id`, `reason` and `details`) |
| `POST /v1/tasks/{id}/resubmit` | The same job again, as a new task (`run_job` with a `resubmit:` key); not while it's still running |
| `POST /v1/tasks/{id}/restart` | A failed or stopped task's next attempt: queues `restart_run` on its queue, which a worker runs, skipping what it finished |
| `POST /v1/tasks/{id}/abandon` | Gives up on a failed or stopped task: `ABANDONED`, no more attempts |
| `POST /v1/tasks/{id}/decisions` | Decides the approval a task waits at (`{"request_id", "approved", "comment", "actor"}`): queues `decide_run`, which a worker runs; 409 when that approval isn't open |

Actions take `{"actor": {"id", "display_name"}}`, the person acting, which the
audit trail records. The API runs no job itself: the task framework runs a
restarted job in whichever process restarts it, so that happens on a worker.

A task belongs to an organization through its `tenant` label: a job's
`tenant_id` (`JobSpec.tenant_id`, or its payload's `tenant_id`), which its
follow-ups inherit; a fan-out across organizations names each follow-up's. A job's optional
`describe(payload)` gives the task its `description` (a workflow's name). Failure categories: `transient`
(retried with backoff), `interrupted` (its worker died or stopped; restarted
by its redelivery), `permanent` (the input can't succeed), `error` (a bug; not
retried).

## Deployments per queue

Each task type has its own SAQ queue, so each can have its own deployment,
concurrency and replicas. Compose runs one service per queue from one image:

| Service | Serves | Knobs (`.env.compose`) |
|---|---|---|
| `async-worker-workflows` | `workflows`: organizations' workflow runs, which reach the admin API at `http://admin:8091` | `ASYNC_WORKER_WORKFLOWS_CONCURRENCY`, `ASYNC_WORKER_WORKFLOWS_REPLICAS` |
| `async-worker-adk-workflows` | `adk_workflows`: organizations' ADK workflow runs, their sessions in `admin-mysql` | `ASYNC_WORKER_ADK_WORKFLOWS_CONCURRENCY`, `ASYNC_WORKER_ADK_WORKFLOWS_REPLICAS` |
| `async-worker-api` | the background tasks API, published on 18204 | `ASYNC_WORKER_API_PORT` |

Every deployment builds all of `HYBRID_ENABLED_TASKS` (cheap: their clients
connect lazily) so follow-ups reach any queue, but serves and sets up only its
own queues (`--queues`, `--ensure-schema`). A cron tick runs once however many
replicas serve its queue. Elsewhere:

```sh
forge-async-worker worker --queues workflows --concurrency 8
```

## Adding a task type

No task-specific code lives here. Write a task package (see [forge-tasks](../../packages/python/tasks/task-sdk/README.md#adding-a-task-type)),
register its factory under the `forge_async_worker.tasks` entry point group, add
it to this app's `pyproject.toml` (a path source under `[tool.uv.sources]` and an
extra, and the dev group so its tests run here), add it to
`HYBRID_ENABLED_TASKS`, and give its queue a deployment. Nothing in the worker
changes: `forge-async-worker tasks` lists it, its jobs become ETF jobs, its
schedules cron jobs, its CLI commands (`cli_name`, `add_cli`, `run_cli` on
its factory) appear under `forge-async-worker <name>`, and its `prepare()`
(whatever it would download at runtime) runs when the image is built.

## Configuration

Env vars prefixed `HYBRID_`, nested with `__`, read from the environment and from
`.env` in the working directory (`apps/forge-async-worker/.env`, from
`.env.example`). The worker's own: `HYBRID_ENABLED_TASKS`, `HYBRID_REDIS_URL`,
`HYBRID_MONGO__*` (`forge_tasks.CoreSettings`), `HYBRID_ETF__*`, `HYBRID_API__*`
and `HYBRID_LOGGING__*` (`config.py`). Logs go through structlog
(`forge_common.logging`): JSON lines, or key=value with
`HYBRID_LOGGING__FORMAT=console` (the default on a terminal), at
`HYBRID_LOGGING__LEVEL`; the background tasks API adds an access line per
request, and every line of a request carries its `request_id`. Each task package reads its own section (the workflows task,
`HYBRID_WORKFLOWS__*`; the ADK workflows task, `HYBRID_ADK_WORKFLOWS__*`). Quote JSON values in single quotes so every `.env` reader (pydantic-settings,
Compose, uv) keeps them intact.

Values the worker shares with other apps live once in the repository's
`.env.common`: the MongoDB and Redis connections, the service tokens, and
the model keys. `.env` names each with a `${NAME}` reference, e.g.
`HYBRID_WORKFLOWS__ADMIN_TOKEN=${FORGE_WORKFLOWS_TOKEN}` (`forge_tasks.env_files`). A
reference resolves to an earlier line of `.env`, otherwise to `.env.common`; a
name defined in neither stops startup unless the process environment sets that
variable. `.env.common` is read from `../../.env.common`, or
`FORGE_ENV_COMMON_FILE` (empty disables it), and none of it reaches the settings
unless `.env` references it. Compose resolves the references itself from
`.env.common`.

## Running

From the repository root:

```sh
make async-worker-install     # uv sync --locked: the app, the task packages and the dev tools
make async-worker-check       # ruff, format check, mypy and pytest: the app, then each task package; no services needed
make async-worker-deps        # mongo (127.0.0.1:27037), redis (127.0.0.1:16389)
make async-worker             # the worker natively, every queue, after ensure-schema
make async-worker-api         # the background tasks API natively, on http://localhost:8104
make up                       # the whole stack, async-worker-workflows and async-worker-api included
make logs-async-worker
make async-worker-test-redis  # the SAQ worker on a real Redis (database 15 of redis, cleared)
```

```sh
forge-async-worker worker [--queues workflows] [--concurrency 4] [--grace-period 30] [--ensure-schema]
forge-async-worker worker --check      # exit 1 unless this host serves each queue (the container healthcheck)
forge-async-worker prepare             # each installed task's prepare(): what it would download at runtime (image builds)
```

On SIGINT or SIGTERM the worker waits up to `--grace-period` seconds for running
jobs, and SAQ re-queues the ones still running.

The CLI runs jobs inline, without a worker (and without ETF). Run it from
`apps/forge-async-worker` so it reads `.env`:

```sh
uv run forge-async-worker tasks            # installed task types, their queues and schedules
uv run forge-async-worker ensure-schema
```

The admin API submits `workflows.run` for each run a member starts (the
builder's Run button or the assistant), and for each run another workflow's
Run workflow step starts, under the organization as tenant
([apps/forge-admin-api](../forge-admin-api/README.md)).

The image runs `forge-async-worker prepare` at build, so a bundled task never
downloads at runtime.

## What is and isn't verified

**Verified by tests**, with toy tasks: the SAQ job through ETF: a run per delivery with its
result and `run_id`, lock re-queue, a transient error retried with our backoff
and its retry restarting the run as attempt 2, bugs and permanent failures
without retry (and a redelivered bug not re-run), a duplicate delivery served
from the completed run, a run cut off by a shutdown restarted by its
redelivery, heartbeat; the upkeep recovering an orphaned run and its
`redrive_run`; schedules as uniquely named cron jobs, each tick a run of its
own; queue routing; `prepare`. `mypy` and `ruff` are clean. The task packages'
own tests cover their jobs, their registration and their example APIs.

**Verified on a real Redis** (`make async-worker-test-redis`): jobs run
concurrently up to the limit, a transient error is rescheduled with our
backoff, other errors fail without retry, each schedule and each queue's upkeep
gets its own cron key, the health check sees this host, a stop waits for
running jobs but not for idle workers, and bare enqueues run under the worker's
policy.

**Not yet run** as the Compose stack or against a live MongoDB for the ETF
store (the tests use its in-memory store; ETF's own suite covers the Mongo
store).
