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
     └── enqueue("run_adk", run_id=…) ──► Redis: adk_workflows ─────────► forge-async-worker ──────────┤
                                                                            run_adk                    │
                                                                            └─► the ADK workflows      │
                                                                                task's run job         │
                                          Redis: adk_workflows_upkeep ──► (same process) ──────────────┘
                                                                            maintain (cron) · expire_pause
```

## Layout

```
src/forge_async_worker/
  saq_worker.py   the SAQ job functions (run_adk, expire_pause, maintain), the settings every job runs under,
                  the runs' worker and the upkeep's (with its cron), graceful stop, the health check, opening the run store
  jobs.py         AdkRunJobs: what each job does to a run (below)
  watchdog.py     Watchdog: a thread that ends the process when a run's job outlives its lease, or its time to stop,
                  backed by faulthandler's timer for when another thread holds the interpreter lock
  control.py      RunControl: a run's JobControl on the run store (kept state, decisions, pauses, waits, notes)
  queue.py        RunQueue: run_adk on the adk_workflows queue, expire_pause on adk_workflows_upkeep, failures
                  kept in Redis for the maintenance, and each job's SAQ settings
  runtime.py      build_runtime(): the installed task packages, built (entry points)
  config.py       WorkerSettings: HYBRID_ENABLED_TASKS, HYBRID_REDIS_URL and HYBRID_LOGGING__*
  cli.py          forge-async-worker: worker, tasks, ensure-schema, prepare, run
tests/                    the jobs on a SQLite run store with a stand-in task (toy_tasks.py), and with the real
                          ADK workflows task (test_adk_runs.py); the worker on a real Redis (test_saq_redis.py)
Dockerfile                uv-built image with the ADK workflows task and what its prepare() downloads
compose.yaml              async-worker-adk-workflows
.env.example              settings template; make env copies it to .env
pyproject.toml            dependencies (a member of the root uv workspace, whose uv.lock and .venv it uses)
```

## The jobs

Other services enqueue by name, with no code from this package:
`enqueue("run_adk", run_id=..., key=...)` on the `adk_workflows` queue (the
admin API does after it creates, decides, answers, retries or resubmits a
run). A job whose key is queued or running already isn't queued again, and a
duplicate is harmless: taking a run is atomic. The worker runs every job
under its own settings (`queue.JOB_OPTIONS`: `run_adk` has no timeout, a
2-minute heartbeat and no SAQ retries), whatever it was enqueued with.

**`run_adk(run_id)`** takes the run (`claim`, which counts the takings in
`lease_epoch`). A run that isn't to be run now (another worker's, paused,
finished, being abandoned, or waiting for a time that hasn't come) is left
alone, and so is one taken with less than a quarter of its 2-minute lease
left (the lease is reckoned from when the claim was asked for, and checked
against the `lease_until` it returned): the maintenance has it once its
lease runs out. Otherwise the worker holds it, renewing the lease (and
touching its SAQ job) every 40 seconds from its claim until what became of
the run is written, and runs the ADK workflows task's `run` job on its
payload (`RunPayload`) under a control backed by the run (`control.py`), as
a task of its own. The job is stopped when someone abandons the run (a
checkpoint stops it at its next step, the renewal within 40 seconds while a
step goes on), once the attempt has gone on for the task's `attempt_timeout`,
when the lease was lost, when the worker stops, and while renewals fail
(each bounded, and tried again every 5 seconds), 30 seconds before its last
confirmed lease runs out. A stopped job has 30 seconds to stop, its lease
held meanwhile, and never longer than until 10 seconds before the lease runs
out. One that doesn't, because it ignores the cancellation or holds the
event loop, ends the worker process: the watchdog (`watchdog.py`), a thread
of its own, holds each running job's deadline, so a job never goes on beside
another worker's attempt at its run (it exits with status 70, saying why).
Should another thread hold the interpreter lock meanwhile (C code, such as
Python's `re` backtracking), so that the watchdog's thread can't run,
faulthandler's timer, a C thread of its own, ends the process a second after
the deadline instead, writing every thread's stack to stderr (status 1). The
process's other runs are cut off with it, and the maintenance recovers them
once their leases run out. How the job ends decides what becomes of the run:

| The job ... | The run ... |
|---|---|
| returns a result | `succeeded` with the graph's result; or, a failed result, `failed` (`failed`) with its message, step and result |
| asks for a decision or an answer nobody gave yet | `paused` at that approval or question; with a timeout, `expire_pause` is queued for its deadline |
| waits for a time | `waiting`; `run_adk` is queued for then |
| hits a hiccup (`TransientError`: a model's overload, the network, the database) | `queued` again as its next attempt, not taken before its backoff ends (`next_attempt_at`, 30 s → 30 min cap, ±25% jitter: whatever job comes for it sooner takes nothing), and `run_adk` queued for then; after 10 attempts, `failed` (`transient`) |
| goes on past its attempt's deadline | the same, as a hiccup |
| is stopped by an abandonment | `abandoned`; whatever would carry it on (a pause, a wait, a retry) abandons it instead, and a run that ends first ends as it did |
| raises anything else (a bug), or its payload doesn't fit | `failed` (`error`) |
| is stopped as its worker stops | `queued` again at once (`interrupted`), for the next worker, once the job has stopped; or `abandoned`, when someone abandoned it |
| loses its lease (it ran out, and another worker took the run), or can't renew it | left to whoever has it, or the maintenance: every write is fenced on the lease, and the job is stopped |

What became of the run is written while its lease is held, each try taking
10 seconds at most, up to 4 times while the database hiccups or hangs (1, 2
and 4 seconds apart). A failure, or a hiccup, is kept in Redis first
(`forge:adk-run-failure:<run>:<lease_epoch>`, a week: each taking of the run
its own, so one taking forgetting its own never takes a later one's) and
forgotten once written: should the write not land, the maintenance records
it once the run's lease runs out, so the run fails as its worker meant,
rather than running again or failing as "its worker went" (a kept hiccup
queues it again). Every Redis call (a job queued, a failure kept or read)
takes 5 seconds at most.

The control (`control.py`): `keep` and `load` work on a copy of what the run
keeps, which a `checkpoint` makes durable; `approval(key=...)` answers at once
with the run's decision for that gate, and without one pauses the run (its
kind, `approval` or `human_input`, from `details["kind"]`; a deadline from
`timeout_seconds`), with when it was made (the deadline holds to that, not to
when the run reads it); `wait_until` makes the run wait; `note` adds a line to its
activity; `should_stop` says once someone abandoned the run, and a checkpoint
then raises `StopRun`. Checkpoints land one at a time, each with all the run
keeps (steps and tool calls go on at once). `children` starts the runs the
run's LLM agents call as workflows: each linked to it, under the ID its call
makes for it (a call answered again finds that run, and queues no other),
and only while this worker has the run and nobody's abandoning it. The run's ID is the control's `run_id` and
`instance_id`, the same for every attempt: a resubmitted run is a new run,
and a new ADK invocation. `lease_epoch` says which taking of the run the
attempt is, for keys that make its effects happen once.

**`expire_pause(run_id, pause_id)`**, at a pause's deadline: unless someone
decided it, the approval is rejected by nobody ("Nobody decided by its
deadline"), and the run is queued to carry on down its rejected way.

**`maintain`**, every minute (a SAQ cron job, once however many workers
serve the queue):

- a running run whose lease ran out (its worker died) is queued again
  (`interrupted`), or fails after 10 attempts; one whose worker kept its
  failure fails with it (or, a kept hiccup, is queued again with it). While
  Redis can't say whether its worker kept one, it's left for the next
  maintenance (`deferred`): queued again, the next taking of it would make
  the failure kept an earlier taking's, and lost;
- a run abandoned while it ran, whose worker went before it stopped it, is
  abandoned;
- a queued run no job took for 2 minutes (its job was lost), and a wait whose
  time has come, get a job; a run waiting out a hiccup's backoff is left to it
  until a while after it ends;
- a paused run past its deadline is rejected and queued, as `expire_pause` would.

One run's upkeep failing doesn't hold up the others'. A job that can't be
queued (Redis is down) is logged and left to the maintenance. The upkeep
(`maintain`, `expire_pause`) has a queue of its own, `adk_workflows_upkeep`,
served by a worker of its own in the same process (2 jobs at once), so runs
filling every slot never hold it up. The runs' worker still takes the upkeep's
jobs queued on `adk_workflows` before it had a queue of its own. Runs are
coroutines, so a worker runs `--concurrency` of them at once (default 4) on
its event loop.

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
creates them, and later ones add to them (`0020adk_run_control`: abandoning
a running run, and its takings), so `make admin-migrate` runs before a worker
of new code. `--ensure-schema` sets up the ADK session tables and says when
the run store's aren't there yet.

## Running

From the repository root:

```sh
make async-worker-install     # uv sync --locked --all-packages: the whole Python workspace and its dev tools, in the root .venv
make async-worker-check       # ruff, format check, mypy and pytest: the app, then each task package; no services needed
make async-worker-deps        # redis (127.0.0.1:16389) and admin-mysql (127.0.0.1:13326)
make async-worker             # the worker natively, after ensure-schema
make up                       # the whole stack, async-worker-adk-workflows included
make logs-async-worker
make async-worker-test-redis  # the SAQ worker on a real Redis (database 15 of redis, cleared)
```

```sh
forge-async-worker worker [--concurrency 4] [--grace-period 30] [--ensure-schema]
forge-async-worker worker --check      # exit 1 unless this host serves the queue(s), the upkeep's too (the container healthcheck)
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
and undecided approvals and questions, waits, notes, a checkpoint of an
abandoned run stopping it); `run_adk` through every outcome above (success, a
failed result, a payload that doesn't fit, a pause then a decision or an
answer, a wait, a hiccup's retry with backoff and its giving up, a bug, a
lost lease found at a checkpoint and by the renewal, the lease and SAQ job
kept fresh, a stop mid-run, an attempt past its deadline); abandoning a
running run (at its next step, during a long step, as it would pause, wait
or retry, as it ends, as its worker stops); renewals that hang or fail
stopping the job before its lease runs out, and one that fails now and then
tried again; a run taken too late to run (a slow claim, or a lease run out on
the run store's clock), and a lease that ran out meanwhile, stopping the job
at the next renewal; a job that won't stop (past its deadline, as its lease
runs out, as its worker stops) holding its run, never let go, until the
watchdog ends the process, and one holding the event loop ended by it too;
the watchdog ending a process of its own (`test_watchdog.py`): at the
deadline, and, while another thread holds the interpreter lock, by
faulthandler's backstop; a stop letting the run go only once its job stopped;
outcomes written again while the database hiccups, each try bounded, under a
lease held throughout, and a failure kept for the maintenance before it's
written (a write that hangs loses nothing; a stale one an earlier taking kept
ignored; an earlier taking's cleanup never taking a later one's; one kept for
the run by a worker from before read for its own taking only);
`expire_pause`; the maintenance (interrupted runs recovered or
failed, kept failures recorded, recovery waiting while Redis can't say
what a run's worker kept, or hangs, runs abandoned while their worker went,
stalled runs and waits that are over queued, a backoff respected, overdue
pauses expired, one run's failure not holding up the others); the SAQ
settings, queues, keys and cron. The real ADK workflows task runs through an
approval and its decision, its timeout, a question and a failed step.

**Verified on a real Redis** (`make async-worker-test-redis`): runs go
concurrently up to the limit, a hiccup's retry is scheduled with backoff, the
maintenance has its cron key on the upkeep's queue, a pause expires while runs
fill every slot, the health check sees this host, a stop waits for running
runs but not for idle workers, and a bare enqueue runs under the worker's
settings.

**Not yet run** against MySQL (the run store's tests use SQLite) or as the
Compose stack.
