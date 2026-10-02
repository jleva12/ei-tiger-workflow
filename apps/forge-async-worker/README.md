# forge-async-worker

The async worker for this monorepo: app `apps/forge-async-worker`, package
`forge-async-worker`, module `forge_async_worker`, on Python 3.13 with uv. It
runs organizations' **ADK workflow runs** off the `adk_workflows` SAQ queue on
Redis. Each run lives in the **run store**
([`run_store.py`](../../packages/python/adk-workflows/src/forge_task_adk_workflows/run_store.py):
the `adk_runs` and `adk_run_events` tables in the admin MySQL, beside the
runs' ADK sessions), where the admin API starts, lists and acts on it, and
the worker takes it, runs it, and records how it went. The ADK logic lives in
the [ADK workflows task](../../packages/python/adk-workflows/README.md);
the worker holds none.

```
 admin API ── create the run ──► run store (admin MySQL) ◄── claim, checkpoint, pause, wait, finish ──┐
     │                                                                                                 │
     └── enqueue("run_adk", run_id=…) ──► Redis: adk_workflows ──► forge-async-worker ─────────────────┘
                                                                     run_adk · expire_pause · maintain (cron)
                                                                     └─► the ADK workflows task's run job
```

## Layout

```
src/forge_async_worker/
  saq_worker.py   the SAQ job functions (run_adk, expire_pause, maintain), the settings every job runs under,
                  the worker and its cron, graceful stop, the health check, opening the run store
  jobs.py         AdkRunJobs: what each job does to a run (below)
  control.py      RunControl: a run's JobControl on the run store (kept state, decisions, pauses, waits, notes)
  queue.py        RunQueue: run_adk and expire_pause on the adk_workflows queue, and each job's SAQ settings
  runtime.py      build_runtime(): the installed task packages, built (entry points)
  config.py       WorkerSettings: HYBRID_ENABLED_TASKS, HYBRID_REDIS_URL and HYBRID_LOGGING__*
  cli.py          forge-async-worker: worker, tasks, ensure-schema, prepare, run
tests/                    the jobs on a SQLite run store with a stand-in task (toy_tasks.py), and with the real
                          ADK workflows task (test_adk_runs.py); the worker on a real Redis (test_saq_redis.py)
Dockerfile                uv-built image with the ADK workflows task and what its prepare() downloads
compose.yaml              async-worker-adk-workflows
.env.example              settings template; make env copies it to .env
pyproject.toml, uv.lock   dependencies and the lockfile
```

## The jobs

Other services enqueue by name, with no code from this package:
`enqueue("run_adk", run_id=..., key=...)` on the `adk_workflows` queue (the
admin API does after it creates, decides, answers, retries or resubmits a
run). A job whose key is queued or running already isn't queued again, and a
duplicate is harmless: taking a run is atomic. The worker runs every job
under its own settings (`queue.JOB_OPTIONS`: `run_adk` has no timeout, a
2-minute heartbeat and no SAQ retries), whatever it was enqueued with.

**`run_adk(run_id)`** takes the run (`claim`). A run that isn't to be run now
(another worker's, paused, finished, or waiting for a time that hasn't come)
is left alone. Otherwise the worker holds it for a 2-minute lease, renewed
(and its SAQ job touched) every 40 seconds, and runs the ADK workflows task's
`run` job on its payload (`RunPayload`) under a control backed by the run
(`control.py`). How the job ends decides what becomes of the run:

| The job ... | The run ... |
|---|---|
| returns a result | `succeeded` with the graph's result; or, a failed result, `failed` (`failed`) with its message, step and result |
| asks for a decision or an answer nobody gave yet | `paused` at that approval or question; with a timeout, `expire_pause` is queued for its deadline |
| waits for a time | `waiting`; `run_adk` is queued for then |
| hits a hiccup (`TransientError`: a model's overload, the network, the database) | `queued` again as its next attempt, `run_adk` queued with backoff (30 s → 30 min cap, ±25% jitter); after 10 attempts, `failed` (`transient`) |
| raises anything else (a bug), or its payload doesn't fit | `failed` (`error`) |
| is cut off by a stop | `queued` again at once (`interrupted`), for the next worker |
| loses its lease (it ran out, and another worker took the run) | left to that worker: every write is fenced on the lease, and the job is stopped |

The control (`control.py`): `keep` and `load` work on a copy of what the run
keeps, which a `checkpoint` makes durable; `approval(key=...)` answers at once
with the run's decision for that gate, and without one pauses the run (its
kind, `approval` or `human_input`, from `details["kind"]`; a deadline from
`timeout_seconds`); `wait_until` makes the run wait; `note` adds a line to its
activity. The run's ID is the control's `run_id` and `instance_id`, the same
for every attempt: a resubmitted run is a new run, and a new ADK invocation.

**`expire_pause(run_id, pause_id)`**, at a pause's deadline: unless someone
decided it, the approval is rejected by nobody ("Nobody decided by its
deadline"), and the run is queued to carry on down its rejected way.

**`maintain`**, every minute (a SAQ cron job, once however many workers
serve the queue):

- a running run whose lease ran out (its worker died) is queued again
  (`interrupted`), or fails after 10 attempts;
- a queued run no job took for 2 minutes (its job was lost), and a wait whose
  time has come, get a job; a run waiting out a hiccup's backoff is left to it;
- a paused run past its deadline is rejected and queued, as `expire_pause` would.

A job that can't be queued (Redis is down) is logged and left to the
maintenance. Runs are coroutines, so a worker runs `--concurrency` of them at
once (default 4) on its event loop.

## Configuration

Env vars prefixed `HYBRID_`, nested with `__`, read from the environment and from
`.env` in the working directory (`apps/forge-async-worker/.env`, from
`.env.example`). The worker's own: `HYBRID_ENABLED_TASKS`, `HYBRID_REDIS_URL`
(`forge_tasks.CoreSettings`) and `HYBRID_LOGGING__*` (`config.py`). Logs go
through structlog (`forge_common.logging`): JSON lines, or key=value with
`HYBRID_LOGGING__FORMAT=console` (the default on a terminal), at
`HYBRID_LOGGING__LEVEL`. The ADK workflows task reads its own section,
`HYBRID_ADK_WORKFLOWS__*`; its `SESSION_DATABASE_URL` (the admin MySQL,
`mysql+aiomysql://...`) is the run store's database too, and the worker won't
start without it. Quote JSON values in single quotes so every `.env` reader
(pydantic-settings, Compose, uv) keeps them intact.

Values the worker shares with other apps live once in the repository's
`.env.common`: the MySQL and Redis connections and the model keys. `.env`
names each with a `${NAME}` reference, e.g.
`HYBRID_REDIS_URL=${FORGE_REDIS_URL}` (`forge_tasks.env_files`). A reference
resolves to an earlier line of `.env`, otherwise to `.env.common`; a name
defined in neither stops startup unless the process environment sets that
variable. `.env.common` is read from `../../.env.common`, or
`FORGE_ENV_COMMON_FILE` (empty disables it), and none of it reaches the
settings unless `.env` references it. Compose resolves the references itself
from `.env.common`.

The run store's tables are the admin API's: its migration `0005adk_run_store`
creates them (`make admin-migrate`). `--ensure-schema` sets up the ADK
session tables and says when the run store's aren't there yet.

## Running

From the repository root:

```sh
make async-worker-install     # uv sync --locked: the app, the task packages and the dev tools
make async-worker-check       # ruff, format check, mypy and pytest: the app, then each task package; no services needed
make async-worker-deps        # redis (127.0.0.1:16389) and admin-mysql (127.0.0.1:13326)
make async-worker             # the worker natively, after ensure-schema
make up                       # the whole stack, async-worker-adk-workflows included
make logs-async-worker
make async-worker-test-redis  # the SAQ worker on a real Redis (database 15 of redis, cleared)
```

```sh
forge-async-worker worker [--concurrency 4] [--grace-period 30] [--ensure-schema]
forge-async-worker worker --check      # exit 1 unless this host serves the queue (the container healthcheck)
forge-async-worker prepare             # each installed task's prepare(): what it would download at runtime (image builds)
```

On SIGINT or SIGTERM the worker waits up to `--grace-period` seconds for
running runs; one still running then is queued again, and SAQ re-queues its
job. Compose runs one service, `async-worker-adk-workflows`
(`ASYNC_WORKER_ADK_WORKFLOWS_CONCURRENCY` and `_REPLICAS` in `.env.compose`);
replicas share the queue.

The CLI also runs a job inline, without a worker or the run store (its state
in memory, a wait ending it). Run it from `apps/forge-async-worker` so it
reads `.env`:

```sh
uv run forge-async-worker tasks            # installed task types and their queues
uv run forge-async-worker run adk_workflows run '{"tenant_id": ..., "document": {...}, ...}'
```

## What is and isn't verified

**Verified by tests** on a SQLite run store: the control (state kept and
checkpointed, a lost lease, a database hiccup as a `TransientError`, decided
and undecided approvals and questions, waits, notes); `run_adk` through every
outcome above (success, a failed result, a payload that doesn't fit, a pause
then a decision or an answer, a wait, a hiccup's retry with backoff and its
giving up, a bug, a lost lease found at a checkpoint and by the renewal, the
lease and SAQ job kept fresh, a stop mid-run); `expire_pause`; the
maintenance (interrupted runs recovered or failed, stalled runs and waits
that are over queued, a backoff respected, overdue pauses expired); the SAQ
settings, queue keys and cron. The real ADK workflows task runs through an
approval and its decision, its timeout, a question and a failed step.

**Verified on a real Redis** (`make async-worker-test-redis`): runs go
concurrently up to the limit, a hiccup's retry is scheduled with backoff, the
maintenance has its cron key, the health check sees this host, a stop waits
for running runs but not for idle workers, and a bare enqueue runs under the
worker's settings.

**Not yet run** against MySQL (the run store's tests use SQLite) or as the
Compose stack.
