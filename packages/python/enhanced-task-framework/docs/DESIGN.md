# Enhanced Task Framework (ETF) — Design Specification

**Status:** Draft v0.1
**Audience:** Framework implementers and integrators
**Scope:** A library to **track, manage, and recover** long-running asynchronous task
runs in any Python application, *triggered* by external ingresses (a task queue, Kafka,
HTTP, cron, …).

---

## 1. Problem statement & goals

We need a library that sits **inside the worker process** and acts as the *control
plane* for asynchronous task runs. Tasks are submitted through many ingresses, but
once a task begins, ETF owns the bookkeeping: lifecycle state, checkpoints, audit,
pause/resume for human-in-the-loop (HITL) validation, and recoverable restarts.

### 1.1 Functional goals

| # | Goal | Mechanism |
|---|------|-----------|
| G1 | Track runs submitted via **any** ingress (task queue, Kafka, HTTP, …) | `Ingress` adapters → `LaunchRequest` → `JobLauncher` |
| G2 | **Pause** a run for human validation, without pinning a worker | Checkpoint-and-release; `AWAITING_VALIDATION` state; `ValidationRequest` |
| G3 | **Restart** a failed run, allowing config changes on failure | `JobOperator.restart(...)` with non-identifying parameter overrides |
| G4 | **Idempotent** like Spring Batch jobs | `JobInstance` identity = job name + identifying params; completed instances cannot re-run; completed steps skipped on restart |
| G5 | **Audit** every run incl. exceptions & decisions | `AuditEvent` stream via `AuditSink`; structured `FailureRecord` |
| G6 | **Pluggable persistence** and infrastructure | ABCs: `StateStore`, `AuditSink`, `RunLockProvider`, `Serializer`, plus policies |

### 1.2 Non-goals

- ETF is **not** a message broker or a worker pool, and depends on none. It does not
  own the host's consume loop; ingress adapters call into ETF from within the host.
- ETF does **not** schedule tasks. Cron/scheduling is an ingress concern.
- ETF does not replace your business logic; you implement `Step`s.

### 1.3 Design principles

1. **Async-native.** The core API is `async def`; the default store (MongoDB via
   Beanie/Motor) is async end to end.
2. **Storage-agnostic core.** The orchestration depends only on abstract contracts.
   The domain model is plain dataclasses; persistence types live behind `StateStore`.
3. **Crash-only / resumable.** Any run can be killed at any point and resumed from the
   last durable checkpoint. State transitions are persisted *before* side effects when
   possible, and idempotency keys protect against at-least-once delivery.
4. **Everything is audited.** State changes, retries, skips, pauses, decisions,
   parameter overrides, and exceptions all emit immutable audit events.

---

## 2. Domain model

ETF mirrors Spring Batch's separation of *definition*, *instance*, and *execution*,
adding first-class HITL and audit concepts.

```
JobDefinition (template, in code)
      │  defines ordered Steps + policies
      ▼
JobInstance  ──< identity = job_name + identifying_parameters >──  (idempotency anchor)
      │  1..N attempts
      ▼
JobRun  (a.k.a. "execution": one attempt; restart creates a new run)
      │  1..N
      ▼
StepRun (one step execution within a run)
      │
      ├── ExecutionContext   (durable checkpoint state, per run and per step)
      ├── FailureRecord[]    (captured exceptions, cause chain, stack)
      └── ValidationRequest? (open HITL gate, if paused)

AuditEvent  (append-only stream, references run/step)
```

### 2.1 Entities

- **`JobDefinition`** — In-code blueprint: `name`, `version`, ordered `steps`, optional
  conditional `flow`, and policy overrides (`RetryPolicy`, `RestartPolicy`,
  `SkipPolicy`, `JobInstanceResolver`). Registered in a `JobRegistry`.

- **`JobParameters`** — Two partitions:
  - `identifying` — contribute to instance identity (e.g. `tenant_id`, `dataset_date`).
  - `non_identifying` — operational knobs (e.g. `batch_size`, `dry_run`).
    **These are what a user may change when restarting after a failure.** Never put
    secrets in either partition: parameters are stored with every run and copied into
    audit events. Resolve credentials when the step executes.

- **`JobInstance`** — The logical, idempotent unit. Identity = `job_name` +
  `identity_hash(identifying_parameters)`. A store-level **unique constraint** on
  `(job_name, identity_hash)` guarantees one instance. Carries an aggregate
  `BatchStatus`. A `COMPLETED` instance is terminal and cannot be re-run.

- **`JobRun`** — One attempt to execute an instance. Holds `status`, `exit_status`,
  full effective `parameters` (with overrides), timing, `attempt`, `restart_of`
  (parent run), run-level `ExecutionContext`, `failures`, current `validation`,
  `actor`, `correlation_id`/`trace_id`, and an optimistic-lock `version`.

- **`StepRun`** — One execution of a step within a run. Holds `status`, `exit_status`,
  metrics (`read/write/skip/commit` counts), per-step `ExecutionContext` (the
  checkpoint), `failures`, `attempt`.

- **`ExecutionContext`** — A serializable, dirty-tracked key/value bag (Spring Batch
  analogue). Steps stash resume state here (cursor offsets, page tokens, partial
  aggregates). Persisted at each `checkpoint()`.

- **`ValidationRequest` / `ValidationDecision`** — The HITL gate. A request captures
  what a human must review (`reason`, `payload`, `decision_schema`, `required_role`,
  `sla_deadline`). A decision captures `decision` (APPROVE/REJECT/RETRY/OVERRIDE),
  `actor`, `comment`, and `parameter_overrides`.

- **`FailureRecord`** — Structured exception capture: `exception_type`, `message`,
  `stack_trace`, `cause_chain[]`, `step_name`, `attempt`, `retryable`, `fatal`,
  `occurred_at`, plus arbitrary `attributes`.

- **`Actor`** — Who acted: `kind` (SYSTEM / HUMAN / SERVICE), `id`, `display_name`,
  and explicit `roles` used to authorize restricted validation decisions.
  Threaded through every audit event and decision.

- **`AuditEvent`** — Immutable record: `event_type`, `at`, `actor`, `from_status`,
  `to_status`, `exit_status`, `failure?`, `message`, `attributes`, `correlation_id`,
  `trace_id`, and a monotonically increasing `sequence` within the run.

### 2.2 Status model

`BatchStatus` (run/step/instance level), stored as strings, with a severity order so a
run's aggregate status = the most severe of its steps:

```
PENDING → RUNNING ─┬→ PAUSING → PAUSED ─┐
                   ├→ AWAITING_VALIDATION ┤ (resume re-enters RUNNING)
                   ├→ STOPPING → STOPPED
                   ├→ COMPLETED
                   ├→ FAILED ──(restart)──→ new JobRun
                   └→ ABANDONED
                               UNKNOWN (severity ceiling)
```

`ExitStatus` is a separate `(code, description)` value object that drives **conditional
flow** between steps, exactly like Spring Batch (`COMPLETED`, `FAILED`, `STOPPED`, or
custom codes such as `VALIDATION_REQUIRED`, `RECONCILE`). `BatchStatus` is *how* a run
ended; `ExitStatus` is *what to do next*.

---

## 3. Architecture

```
        ┌──────────── Ingress adapters (thin) ───────────┐
        │  QueueIngress    KafkaIngress   HttpIngress ... │
        └───────────────────────┬─────────────────────────┘
                                │ LaunchRequest / ResumeRequest
                                ▼
   ┌───────────────────────── ETF Core ─────────────────────────┐
   │                                                             │
   │   JobRegistry ──► JobLauncher ──► JobRunner ──► Step.execute │
   │                        │              │                      │
   │                     JobOperator (pause/resume/restart/stop)  │
   │                        │              │                      │
   │        ┌───────────────┼──────────────┼───────────────┐     │
   │        ▼               ▼              ▼                ▼     │
   │   StateStore     AuditSink     RunLockProvider    Serializer │
   │   (Beanie/Mongo) (store/Kafka/  (Mongo TTL lock)  (JSON/...) │
   │                   OTel/log)                                  │
   └─────────────────────────────────────────────────────────────┘
            │                    │                    │
            ▼                    ▼                    ▼
        MongoDB             audit target        lock backend
       (default)           (pluggable)          (pluggable)
```

### 3.1 Components

- **`JobRegistry`** — maps `job_name` → `JobDefinition`. Workers register definitions
  at startup.
- **`JobLauncher`** — entry point for new runs. Resolves identity, enforces
  idempotency, acquires the instance lock, creates the `JobRun`, delegates to the
  runner.
- **`JobRunner`** — executes steps in flow order: skip-already-completed (restart),
  retry/skip on failure, checkpoint, pause on validation, persist transitions, audit.
- **`JobOperator`** — the management/control API: `pause`, `resume`,
  `submit_validation_decision`, `restart`, `stop`, `abandon`, plus query methods
  (`get_status`, `list_runs`, `get_audit_trail`, `list_pending_validations`).
- **Pluggable contracts** — see §6.

### 3.2 Deployment model

ETF is a library embedded in the worker. Multiple workers may process the same job
type; the **`RunLockProvider`** gives one worker a renewable lease for a `JobInstance`.
Pause releases the worker entirely; resume can happen on any worker. A lease is not a
proof that stale code stopped executing after expiry; §5 describes the required fencing
for external side effects.

---

## 4. Lifecycle flows

### 4.1 Launch (new run)

```
Ingress.to_launch_request(msg) ──► JobLauncher.launch(req)
  1. dedupe: store.register_idempotency_key(req.idempotency_key)
        └─ already seen → served by the instance's latest run, as in step 6
  2. resolve the latest JobDefinition version from the registry
  3. identity_hash = InstanceResolver.resolve_identity(name, params.identifying)
        (independent of the definition version: upgrades never repeat completed work)
  4. instance = store.find_or_create_job_instance(name, identity_hash, ...)
  5. lock = lock_provider.acquire(instance_id)        # acquire renewable writer lease
  6. by the instance's latest run:
        ├─ COMPLETED / ABANDONED        → reject: JobInstanceAlreadyCompleteError
        ├─ PAUSED / AWAITING_VALIDATION → return it (durably waiting)
        ├─ PENDING                      → run it
        ├─ RUNNING / STOPPING / PAUSING → nobody owns it (we hold the lock): recover it
        │                                 as FAILED (etf.WorkerLost), then as below
        └─ FAILED / STOPPED             → interrupted (WorkerLost or cancelled) and within
                                          max_auto_redrives → restart it (re-drive);
                                          otherwise reject: use JobOperator.restart(...)
  7. run = store.create_job_run(instance, attempt=1, params, definition fingerprint)
        → audit RUN_CREATED
  8. JobRunner.run(run)   # on the run's own definition version
```

### 4.2 Step execution & retry

```
for step in definition.flow_from(current):
  if store.find_last_step_run(instance_id, step.name, COMPLETED):   # restart skip
      emit IDEMPOTENT_SKIP; continue
  step_run = store.create_step_run(run, step.name)                  # RUNNING
  ctx = StepContext(load run & step ExecutionContext)               # resume state
  attempt = 0
  while True:
    try:
        result = await step.execute(ctx)                            # business logic
    except PauseForValidation as p:        → see §4.3 (pause)
    except StopExecution:                  → STOPPED; persist; return
    except Exception as e:
        failure = FailureRecord.from_exception(e, step.name, attempt)
        store.append_failure(run, failure)                          # audit STEP_FAILED
        if SkipPolicy.should_skip(failure): emit ITEM_SKIPPED; break
        d = RetryPolicy.should_retry(failure, attempt)
        if d.retry: emit RETRY_ATTEMPT; await sleep(d.delay); attempt+=1; continue
        step_run → FAILED; run → FAILED; return
    else:
        persist run & step ExecutionContext                         # CHECKPOINT_SAVED
        step_run → COMPLETED(result.exit_status); break
  next = definition.transition(step, result.exit_status)            # conditional flow
run → COMPLETED; instance → COMPLETED                               # audit COMPLETED
```

### 4.3 Pause for human validation (checkpoint-and-release)

A step requests validation by raising `PauseForValidation(request)` (or returning a
`StepResult` with `status=AWAITING_VALIDATION`). The runner then:

```
1. ctx.flush() → persist run & step ExecutionContext (durable checkpoint)
2. vr = store.create_validation_request(run, step, reason, payload, schema, role, sla)
3. run.status → AWAITING_VALIDATION ; step_run.status → PAUSED
4. emit VALIDATION_REQUESTED ; release lock ; RETURN (worker is freed)
```

The worker is now free. The validation request surfaces to humans via whatever channel
the integrator wires (a UI polling `list_pending_validations`, a Kafka topic, an email).

**Resume** arrives as a `ResumeRequest` (from an HTTP endpoint, a Kafka decision topic,
a UI). `JobOperator.submit_validation_decision(request_id, decision)`:

```
1. load run (must be AWAITING_VALIDATION) and validation request
2. record ValidationDecision (actor, comment) → audit VALIDATION_DECISION
3. if decision.parameter_overrides: merge into run.parameters.non_identifying
        → audit PARAMETERS_OVERRIDDEN (with before/after diff)
4. decision == REJECT  → run.status → STOPPED (or FAILED), emit STOPPED, return
   decision == APPROVE/RETRY/OVERRIDE:
        run.status → RUNNING ; emit RESUMED
        acquire lock ; JobRunner.run(run) resuming at the paused step from checkpoint
```

Because the step's `ExecutionContext` was persisted, the resumed step continues from
where it left off — or re-runs cleanly if it is written idempotently.

### 4.4 Restart after failure (with config changes)

```
JobOperator.restart(instance_or_run_id, parameter_overrides=None, from_step=None,
                    *, actor=None, definition_version=None)
  1. instance = store.get_job_instance(...)
  2. last = store.find_latest_run(instance.id); EtfConfig.authorizer(OperatorAction)
  3. assert last.status in {FAILED, STOPPED}
  4. definition = last run's version (fingerprint-checked), or definition_version
  5. assert RestartPolicy.can_restart(instance, last)
  6. params = merge(last.parameters, overrides into non_identifying only)   # identity unchanged
        └─ identifying params are immutable; changing them = a different JobInstance
  7. new_run = store.create_job_run(instance, attempt=last.attempt+1, restart_of=last.id,
                                    params, run context of last)
        → audit RESTARTED credited to actor (+ PARAMETERS_OVERRIDDEN with before/after)
  8. JobRunner.run(new_run)  # skips COMPLETED steps; resumes the failed step from its checkpoint
```

The key idempotency guarantees, mirroring Spring Batch:

- **You cannot restart a `COMPLETED` instance.** (No duplicate side effects.)
- **Completed steps are not re-executed** on restart (the runner skips them via
  `find_last_step_run(..., COMPLETED)`).
- **Identifying parameters are immutable** across restarts; only non-identifying
  parameters may change. To run with different identifying params, you create a *new*
  instance.

### 4.5 Stop / abandon

`stop` sets a cooperative `STOPPING` flag the runner checks at step boundaries;
the run transitions to `STOPPED` and may be restarted. `abandon` marks a run
`ABANDONED` (terminal, non-restartable) — used to retire a poisoned run.

---

## 5. Idempotency, consistency & delivery

ETF assumes **at-least-once** delivery from ingresses and provides three layers:

1. **Ingress dedupe** — `LaunchRequest.idempotency_key` (e.g. Kafka
   `topic-partition-offset`, a queue's message id, or a business key). `register_idempotency_key`
   is an atomic insert; a duplicate returns the already-created run instead of launching.
2. **Instance identity** — the unique `(job_name, identity_hash)` constraint makes
   concurrent launches of the same logical job converge on one instance.
3. **Step-level resume** — completed steps are skipped; in-progress steps resume from
   their checkpoint. Steps should still be written to be **idempotent** (upserts, keyed
   writes) because a crash can occur after a side effect but before the checkpoint.

**Write ordering.** The runner persists the *intent* (status transition + checkpoint)
around side effects. With the default Mongo store, run/step transitions use optimistic
concurrency (`version` field). Multi-document state changes run through the retryable
callback API (`StateStore.run_in_transaction()`), allowing Mongo to replay a whole
transaction on `TransientTransactionError`.

**Leased writer and fencing.** The `RunLockProvider` (default: a TTL-based Mongo lock
document) prevents concurrent advancement while its lease remains valid. Locks
auto-expire so a crashed worker's lock is reclaimed. Expiry can create an overlap window:
the old process may still be executing while a new holder starts. ETF detects lease loss
when control returns, but cannot revoke an external side effect already in flight.
Acquisitions therefore carry monotonic fencing tokens exposed as
`StepContext.fencing_token`. Where duplicate writes are unsafe, the downstream system
must atomically persist the highest accepted token and reject lower tokens. Without that
downstream check, ETF provides at-least-once/idempotent recovery—not exactly-once
execution.

---

## 6. Pluggability — the extension contracts

All are abstract base classes; the framework ships a default + an in-memory reference.

| Contract | Responsibility | Default impl |
|----------|----------------|--------------|
| **`StateStore`** | Persist instances, runs, steps, checkpoints, validations, idempotency keys | `BeanieStateStore` (MongoDB) |
| **`AuditSink`** | Receive/emit/query the audit event stream | `StoreBackedAuditSink`; pluggable to Kafka/OTel/log |
| **`RunLockProvider`** | Single-writer lock per instance | `MongoLockProvider` (TTL) / `InMemoryLockProvider` |
| **`Serializer`** | (De)serialize `ExecutionContext` & params | `JsonSerializer` |
| **`JobInstanceResolver`** | Compute identity hash from identifying params | `HashingInstanceResolver` |
| **`RetryPolicy`** | Decide retry + backoff per failure | `BackoffRetryPolicy` |
| **`SkipPolicy`** | Decide whether to skip a failed item | `NeverSkipPolicy` |
| **`RestartPolicy`** | Govern whether/how a failed instance restarts | `DefaultRestartPolicy` |
| **`Clock`** | Inject time (testability) | `SystemClock` |
| **`IdGenerator`** | Generate ids | `UlidGenerator` |
| **`Ingress`** | Map an external delivery → `LaunchRequest`/`ResumeRequest` | `CallableLaunchIngress`; host-specific adapters (queue, Kafka, HTTP) |

The `StateStore` ABC is the primary swap point. To move off MongoDB, implement the ~25
async methods (CRUD for each entity + `register_idempotency_key` + `initialize` indexes)
and pass your instance to the launcher/operator. Nothing else changes.

### 6.1 Why MongoDB + Beanie as default

- Document model fits the nested, evolving shape of `ExecutionContext`, `FailureRecord`,
  and parameter bags without rigid migrations.
- Beanie gives async (Motor) access with Pydantic validation — aligning with the
  async-native core — and supports unique indexes for instance identity, TTL indexes
  for locks/idempotency keys, and multi-document transactions on replica sets.

---

## 7. Audit subsystem

Every meaningful transition emits an `AuditEvent` through the `AuditSink`. Event types:

```
RUN_CREATED · RUN_STARTED · STATUS_CHANGED · STEP_STARTED · STEP_COMPLETED ·
STEP_FAILED · RETRY_ATTEMPT · ITEM_SKIPPED · CHECKPOINT_SAVED ·
VALIDATION_REQUESTED · VALIDATION_DECISION · PAUSED · RESUMED · RESTARTED ·
PARAMETERS_OVERRIDDEN · STOPPED · COMPLETED · FAILED · RECOVERED ·
IDEMPOTENT_SKIP · ANNOTATION
```

Each event carries `actor` (who), `from_status`/`to_status`, optional `exit_status`,
optional `FailureRecord` (full exception incl. cause chain & stack), free-form
`attributes`, and correlation/trace ids for distributed tracing. Events are **append
-only** and ordered by a per-run `sequence`. `AuditSink.query(...)` reconstructs a full
timeline (`get_audit_trail(run_id)`), and `FailureRecord`s give you exception analytics
across runs.

The default `StoreBackedAuditSink` writes to a Mongo `audit_event` collection. A
`CompositeAuditSink` can fan out to multiple sinks (e.g. store + Kafka + OpenTelemetry);
external sinks are invoked only after the state/store-audit transaction commits.

---

## 8. Example (abridged)

```python
class ReserveInventory(Step):
    name = "reserve_inventory"
    async def execute(self, ctx: StepContext) -> StepResult:
        order = ctx.params.get("order_id")
        if not ctx.get("reserved"):
            await inventory.reserve(order)          # idempotent upsert keyed by order
            ctx.put("reserved", True)
            await ctx.checkpoint()                  # durable resume point
        if needs_human_review(order):
            await ctx.request_validation(           # → checkpoint-and-release
                reason="High-value order needs approval",
                payload={"order_id": order, "amount": amount},
                required_role="ops-approver")
        return StepResult.completed()

job = JobDefinition(name="fulfill_order", version=1,
                    steps=[ReserveInventory(), ChargePayment(), Ship()])
registry.register(job)

# Launch (from a queue consumer / Kafka handler / HTTP endpoint):
run = await launcher.launch(LaunchRequest(
    job_name="fulfill_order",
    parameters=JobParameters(identifying={"order_id": "A-1001"},
                             non_identifying={"dry_run": False}),
    idempotency_key=f"orders-queue:{message_id}",
    requested_by=Actor.service("checkout-svc")))

# Later, a human approves via your UI/HTTP → ingress → operator:
await operator.submit_validation_decision(
    request_id, ValidationDecision.approve(actor=Actor.human("u-42")))

# A failed run, restarted with a bigger batch size:
await operator.restart(run.instance_id,
                       parameter_overrides={"batch_size": 500})
```

---

## 9. Package layout

```
src/etf/
  __init__.py        public API exports
  status.py          BatchStatus, ExitStatus, AuditEventType, enums
  model.py           dataclasses: JobInstance, JobRun, StepRun, FailureRecord, ...
  context.py         ExecutionContext, RunContext, StepContext
  exceptions.py      framework exceptions + control-flow signals
  steps.py           Step ABC, StepResult, JobDefinition, JobRegistry, flow
  audit.py           AuditEvent, AuditSink ABC, CompositeAuditSink
  store.py           StateStore ABC  (primary swap point)
  locking.py         RunLockProvider ABC + InMemoryLockProvider
  serialization.py   Serializer ABC + JsonSerializer
  policies.py        JobInstanceResolver, Retry/Skip/RestartPolicy, Clock, IdGenerator
  ingress.py         LaunchRequest, ResumeRequest, Ingress ABC
  bridge.py          AsyncBridge: drive the async core from synchronous hosts
  operator.py        JobLauncher, JobRunner, JobOperator (orchestration)
  stores/
    memory.py        InMemoryStateStore (reference, for tests)
    beanie_store.py  Beanie documents + BeanieStateStore (default)
  examples/
    order_job.py     end-to-end usage example
```

---

## 10. Open questions / future work

- **Distributed pause signaling** — `stop`/`pause` of an *actively running* step
  currently takes effect at the next step boundary. Mid-step cooperative cancellation
  needs a cancellation token the step polls (`ctx.should_stop()` is backed by a persisted
  control-signal monitor; honoring
  it is the step's responsibility).
- **Parallel / partitioned steps** — the v0.1 flow is linear with conditional
  transitions; fan-out/partitioning (Spring Batch partitioning) is a planned extension
  to the `JobDefinition` flow model.
- **Saga / compensation** — an orchestrated rollback policy across already-completed
  steps is sketched but not implemented (no compensation API ships yet).
- **Schema migration** — document-version stamping on stored entities to evolve the
  model safely.
```
