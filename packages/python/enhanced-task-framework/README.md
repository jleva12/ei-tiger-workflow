# Enhanced Task Framework (ETF)

A Python library to **track, manage, pause, and recover** long-running asynchronous task
runs in any Python application, whatever triggers them (a task queue, Kafka, HTTP, a
scheduler, …). ETF is the control plane that lives inside your application: it owns
lifecycle state, durable checkpoints,
human-in-the-loop validation, recoverable restarts, and a full audit trail.

> **Full design spec:** [`docs/DESIGN.md`](docs/DESIGN.md)

## What it gives you

- **Spring Batch-style idempotency** — a `JobInstance` is identified by job name +
  *identifying* parameters; a completed instance can't re-run, and completed steps are
  skipped on restart.
- **Human-in-the-loop pause** — a step calls `ctx.request_validation(...)`; ETF
  checkpoints and **releases the worker**. A decision (from a UI/HTTP/Kafka) resumes it,
  optionally changing configuration.
- **Restart with config changes** — failed runs restart from the last checkpoint;
  *non-identifying* parameters may be changed on restart, identity is immutable.
- **Full audit** — every transition, retry, skip, pause, decision, override, and
  exception is captured as an immutable `AuditEvent` (with structured `FailureRecord`s).
- **Pluggable everything** — swap the store, audit sink, lock provider, serializer,
  retry/skip/restart policies, and ingress adapters behind small ABCs.

## Architecture at a glance

```
Ingress (queue/Kafka/HTTP/…) ──► LaunchRequest ──► JobLauncher ──► JobRunner ──► Step.execute
                                                       │
                                                   JobOperator  (pause / resume / restart / stop)
                                                       │
              StateStore · AuditSink · RunLockProvider · Serializer · Policies   (all pluggable)
                  └─ default: MongoDB via Beanie ─┘
```

## Install

```bash
pip install -e .            # core (in-memory store, no external deps)
pip install -e ".[mongo]"   # + MongoDB/Beanie default store
pip install -e ".[dev]"     # + pytest, ruff
pip install -e ".[dev,integration]"  # + real Mongo verification profile
```

Requires Python ≥ 3.11.

## Quick start

```python
from etf import (
    EtfConfig, JobLauncher, JobOperator, JobRegistry, JobDefinition,
    Step, StepContext, StepResult, LaunchRequest, JobParameters, Actor, ValidationDecision,
)
from etf.audit import StoreBackedAuditSink
from etf.locking import InMemoryLockProvider
from etf.stores.memory import InMemoryStateStore   # swap for BeanieStateStore in prod

class ReserveInventory(Step):
    name = "reserve_inventory"
    async def execute(self, ctx: StepContext) -> StepResult:
        if not ctx.get("reserved"):
            ...                      # idempotent side effect
            ctx.put("reserved", True)
            await ctx.checkpoint()   # durable resume point
        outcome = await ctx.request_validation(reason="approve high-value order")
        if not outcome.approved:
            raise RuntimeError("rejected")
        return StepResult.completed()

store = InMemoryStateStore()
registry = JobRegistry()
registry.register(JobDefinition(name="fulfill_order", steps=[ReserveInventory()]))
cfg = EtfConfig(store=store, audit=StoreBackedAuditSink(store),
                lock_provider=InMemoryLockProvider(), registry=registry)

# in your queue consumer / message handler / HTTP endpoint:
run = await JobLauncher(cfg).launch(LaunchRequest(
    job_name="fulfill_order",
    parameters=JobParameters(identifying={"order_id": "A-1001"}),
    idempotency_key="delivery-id",
    requested_by=Actor.service("checkout")))

# later, a human approves (or rejects), optionally overriding config:
op = JobOperator(cfg)
status = await op.get_status(run.id)
await op.submit_validation_decision(status.open_validation.id,
                                    ValidationDecision.approve(
                                        Actor.human("u-42", roles={"ops-approver"})
                                    ))

# a failed run, restarted with changed config:
await op.restart(run.instance_id, parameter_overrides={"batch_size": 500})
```

### Use MongoDB (default store) in production

```python
from etf.stores.beanie_store import BeanieStateStore, MongoLockProvider

store = BeanieStateStore("mongodb://localhost:27017", db_name="etf")
await store.initialize()                       # creates indexes
locks = MongoLockProvider(store._db)
await locks.initialize()                       # creates the lease TTL index
cfg = EtfConfig(store=store, audit=StoreBackedAuditSink(store),
                lock_provider=locks, registry=registry,
                require_transactional_store=True)
```

Each `BeanieStateStore` binds its own document classes, so several stores — one
database per tenant, say — can share a process.

The Mongo store uses transactions by default so lifecycle state and store-backed audit
events commit together; production MongoDB must therefore run as a replica set or sharded
cluster. `require_transactional_store=True` makes that production requirement fail fast.
For local standalone MongoDB only, pass `use_transactions=False` and accept that
multi-document transitions are not atomic.

Validation requests may declare `required_role`; decision actors must carry that role in
`Actor.roles`. `decision_schema` accepts the dependency-free JSON Schema subset implemented
by ETF: `type`, `enum`, `const`, object `required`/`properties`/`additionalProperties`, and
array `items`.

### ⚠️ Steps must not block the event loop

The runner, its **lock-lease heartbeat**, and the stop/pause control monitor all share
one event loop. A step that does synchronous I/O or heavy CPU work inside
`execute()` silently stops the heartbeat: the lease expires, another worker (or the
recovery sweep) treats the run as orphaned, and work is duplicated or failed. Always:

```python
from etf import BlockingStep, run_blocking_cancellation_safe

# wrap blocking work without releasing the run lock while its thread is alive…
data = await run_blocking_cancellation_safe(requests.get, url)

# …or subclass BlockingStep, which runs execute_sync() in a worker thread:
class Crunch(BlockingStep):
    name = "crunch"
    def execute_sync(self, ctx: StepContext) -> StepResult:
        heavy_pandas_stuff()
        return StepResult.completed()
```

### Crash recovery & maintenance sweeps

If a worker dies without cleanup (SIGKILL, OOM, hard time limit), the runner can't
mark the run FAILED, so it would stay `RUNNING` forever. Two mechanisms cover this:

- **Fail-fast** — any unexpected exception in the runner (store error, cancellation)
  transitions the run to `FAILED` (restartable) while the lock is held, keeping every
  failure recorded so far. If the audit write fails too, the run is still marked
  `FAILED` without the event (and the gap is logged). A worker that *lost its lease*
  writes nothing — it no longer owns the run — and logs why it stopped; a lock refresh
  that merely errors is retried until the lease could actually lapse.
- **`JobOperator.recover_stale_runs(older_than=...)`** — a sweep that reclaims runs
  stuck in `PENDING`/`RUNNING`/`STOPPING`/`PAUSING` whose instance lock is free (the
  owner is dead), failing the run and any active step with a `RECOVERED` audit event
  so `restart()` works. Run it periodically from any scheduler. Pair it with
  **`JobOperator.expire_validations()`**, which expires `PENDING` validation requests
  past their `sla_deadline` and moves their runs to `STOPPED` (restartable). Both
  sweeps page past records they skip (a live worker's run, a request whose instance
  is busy), and a record they cannot update is logged and skipped rather than
  stopping the sweep.

Failure texts (stack trace, message, cause chain) pass through
`EtfConfig.stack_trace_scrubber` and are bounded by `max_stack_trace_length` /
`max_failure_message_length` (keeping the start and the end) before they are stored;
a run or step keeps at most `max_failures_per_record` failures (the first and the most
recent), and every failure stays in the audit trail. A step whose code calls
`sys.exit` fails with `StepExitError` instead of stopping the event loop.

### Scaling knobs

Each *running* step costs recurring store reads: the control monitor polls the run
every `EtfConfig.control_poll_interval` (default 1 s) to notice `stop()`/`pause()`,
and the runner re-reads the run at every step boundary. With hundreds of concurrent
runs, raise `control_poll_interval` (slower stop/pause reaction, proportionally fewer
reads). The lease heartbeat refreshes the lock every `lock_ttl / 3`.

### Lease and side-effect safety

ETF's locks are renewable **leases**, not an exactly-once execution boundary. A process
can keep running after its lease expires—for example, during a long blocking call or a
network partition—while another worker acquires the same instance. ETF will stop the
stale runner when it regains control, but it cannot roll back side effects the stale
process already sent to another system.

Make external writes idempotent and include `ctx.fencing_token` in the downstream
compare-and-set whenever duplicate execution is unsafe. The downstream system must
persist the highest token for the resource and reject writes with an older token.
Merely reading the token, or relying on the Mongo lease alone, does not enforce fencing.

### Triggering runs from your application

ETF has no dependency on any task queue, broker or web framework. Whatever receives
the work — a queue consumer, a message handler, an HTTP endpoint, a scheduler — maps it
to a `LaunchRequest` (or a `ResumeRequest` for a human decision), directly or through an
`Ingress` adapter such as `CallableLaunchIngress`, and hands it to `JobLauncher` or
`JobOperator`. Use a delivery id that stays stable across redeliveries as the
`idempotency_key`, so duplicate deliveries dedupe to a single run.

A launch runs the job to its next durable state (terminal **or** `AWAITING_VALIDATION`).
Some outcomes are states, not errors, and a host should treat them that way:

- `JobInstanceAlreadyCompleteError` — the instance already finished; acknowledge the
  delivery.
- `RunNotRestartableError` — the latest run failed on its own merits (or was being
  stopped or paused) and needs an operator `restart()`; redelivering can never fix it.
- `JobAlreadyRunningError` / `LockAcquisitionError` — another worker is advancing the
  instance; redeliver later with a jittered backoff.

**Crashed work is not lost.** A delivery that finds a run nobody owns — its worker died
or lost its lease — settles it instead of returning it as accepted. A run whose worker
went away without a verdict (`etf.WorkerLost` from the sweep, or a cancellation such as
a shutdown or a timeout) is restarted as the next attempt when its delivery comes back,
up to `EtfConfig.max_auto_redrives` times in a row (3 by default). Without message
redelivery, re-dispatch the runs `recover_stale_runs()` returns to a worker that calls
`JobOperator.redrive(run.instance_id)`.

### Changing a job definition

Register each changed flow under a new `version`: the registry keeps versions side by
side. New launches use the latest version and every run executes on the version it
started with, so keep an old version registered until its runs (paused and failed ones
included) have finished — or move a failed instance forward with
`restart(..., definition_version=N)`. Each run records its flow's fingerprint (step
names and transitions), so a version edited without a bump is refused with
`DefinitionVersionMismatchError` rather than resumed against the wrong steps. Instance
identity does not depend on the version: work completed under one version is not
repeated after an upgrade.

### Operator actions and authorization

`stop`, `pause`, `resume`, `restart`, `redrive` and `abandon` take an `actor=`, and
their audit events are credited to it (the sweeps' to the system actor). `resume()`
never approves a waiting validation gate: decide it with `submit_validation_decision`
(or pass `decision=`). Set `EtfConfig.authorizer` to check every operator action — it
receives an `OperatorAction` (name, actor, run, job, details) and raises to deny;
denials and rejected validation decisions are written to the audit trail. `Actor.roles`
are claims made by the caller: verify them in the authorizer before relying on
`required_role` gates.

### Labels: listing a tenant's work

A `LaunchRequest` may carry `labels` (short string pairs, keys without `=`), which go
on the instance it creates and never change: a tenant, a task type. List instances by
them, by job name (exact or prefix) and by status, newest first, with the total that
match, for a UI or an API:

```python
page = await store.query_instances(InstanceQuery(
    labels={"tenant": "org-1"}, job_name_prefixes=["workflows."],
    statuses=[BatchStatus.FAILED], limit=50, offset=0,
))
page.items, page.total
```

The Mongo store serves it from one index over the labels; any other store gets a
correct, paging default from `StateStore`.

### Synchronous hosts: `AsyncBridge`

ETF and the Mongo store are asyncio-native. From synchronous code (thread- or
process-pool workers, WSGI apps, CLIs) run them through an `AsyncBridge`, a background
event loop that keeps one Motor/store client alive across calls:

```python
from etf import AsyncBridge

bridge = AsyncBridge().start()          # exactly ONE bridge per process
bridge.run(store.initialize())
run = bridge.run(JobLauncher(cfg).launch(request), timeout=110)
...
bridge.close(finalizer=store.close)     # at process shutdown
```

- Use **one bridge per process**: the Motor/store client is bound to the loop it was
  initialized on, so every ETF call in a process must go through the same `AsyncBridge`
  (the store fail-fasts with a clear error if it doesn't).
- `timeout` is the cancellation threshold for each call; on timeout the in-flight run
  is cancelled and fail-fasted to `FAILED` (recoverable). Cancellation-safe blocking
  work is drained before control returns, so the observed wait can exceed `timeout`.
  If your host enforces its own time limit, set `timeout` just under it; a process kill
  is the final backstop, covered by `recover_stale_runs`.

The opt-in real Mongo replica-set profile is documented in
[`tests/integration/README.md`](tests/integration/README.md); its tests skip unless
their external-service environment variables are supplied.

## Run the example & tests

```bash
PYTHONPATH=src python -m etf.examples.order_job     # full lifecycle demo
pytest                                              # lifecycle test suite
```

## Swapping the persistence backend

Implement the ~two-dozen async methods of [`etf.store.StateStore`](src/etf/store.py)
against your backend and pass it to `EtfConfig`. The two hard guarantees a store must
provide are (1) an atomic unique constraint on `(job_name, identity_hash)` and (2)
optimistic `version` checks on updates — these are what make idempotency and
concurrent-update safety work. `InMemoryStateStore` and `BeanieStateStore` are reference
implementations.

## Package layout

See [`docs/DESIGN.md` §9](docs/DESIGN.md). Core modules live in `src/etf/`; concrete
stores in `src/etf/stores/`; a runnable example in `src/etf/examples/order_job.py`.

## Status

v0.1 design + scaffolding. Linear flows with conditional transitions are implemented;
parallel/partitioned steps and saga compensation orchestration are planned (see
[`docs/DESIGN.md` §10](docs/DESIGN.md)).
