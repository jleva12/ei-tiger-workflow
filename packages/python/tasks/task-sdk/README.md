# forge-tasks

The contract between the [Forge async worker](../../../../apps/forge-async-worker/README.md)
and its task packages (`import forge_tasks`). A task package depends on this,
never on the worker.

- `protocols`: `TaskFactory` (what a package registers), `Task`, `Job`,
  `JobObserver`, `JobQueue`.
- `tasks`: `JobSpec` (what a queue carries: `task_type`, `kind`, `payload`),
  `JobResult`, `Schedule`, `TaskContext` (shared settings, the task's own
  options, shared clients through `ctx.shared(...)`, a lazily created Mongo
  client through `ctx.mongo()`), `TaskRegistry`.
- `errors`: `TaskError` (permanent), `TransientError` (retried with backoff),
  `StorageError` and `from_pymongo`, which sorts pymongo errors into the two.
- `settings`: `CoreSettings` (`HYBRID_ENABLED_TASKS`, `HYBRID_REDIS_URL`,
  `HYBRID_MONGO__*`), `EnvSettings` and `load_section`, which reads a task's
  own `HYBRID_<NAME>__*` section; `env_files` resolves `.env`'s `${NAME}`
  references to the repository's `.env.common`.
- `runtime` / `runner`: an inline runtime that runs jobs in-process,
  breadth-first with their follow-ups, for tests and the CLI. The worker
  builds the same tasks, and runs an ADK workflow run's job off its queue, on
  the run in the run store (`forge_task_adk_workflows.run_store`).
- `control`: what a long-running job asks of its run (`current_control()`):
  state it keeps between attempts, checkpoints, a person's decision, a wait
  until later, notes. The worker's control is backed by the run store;
  `LocalJobControl` keeps it in memory.

## The task abstraction

```python
class Job(Protocol):  # one kind of work inside a task
    name: str  # "run", "record", ...
    payload_model: type[BaseModel]

    def lock_key(self, payload) -> str | None: ...  # dedupe concurrent work on the same target
    async def run(self, payload) -> JobResult: ...  # JobResult.followups fan out more jobs


class Task(Protocol):  # a built task: its jobs and its resources
    name: str
    queue: str
    jobs: Mapping[str, Job]

    async def ensure_schema(self) -> None: ...
    async def close(self) -> None: ...


class TaskFactory(Protocol):  # what a package registers
    name: str
    queue: str
    schedules: Sequence[Schedule]  # declared; the worker runs none today
    settings_model: type[BaseModel]  # optional: HYBRID_<NAME>__*, handed to build as ctx.options

    def build(self, ctx: TaskContext) -> Task: ...
    def prepare(self) -> None: ...  # optional: download what the task fetches at runtime (image builds)
```

Everything travels as `JobSpec(task_type, kind, payload)`. `JobRunner`
resolves the job, validates the payload, runs it and applies one error
contract:

| Outcome | Result |
|---|---|
| returns `JobResult` | `ok` / `skipped` / `superseded`; `followups` get enqueued |
| raises `TransientError` (network, 429/5xx, Mongo failover) | the worker retries it with backoff |
| raises any other `TaskError`, or a bad payload | `failed`, no retry |
| raises anything else (a bug) | the job fails with its traceback, no retry |

A task's `observer` (a `JobObserver`), if it has one, hears every attempt's
outcome; it can never change it.

## Adding a task type

A package of its own under `packages/python/tasks/` (or anywhere), depending
on `forge-tasks`:

```python
class InvoicePayload(BaseModel):
    tenant_id: str
    invoice_id: str
    amount: float


class InvoicesSettings(BaseModel):  # HYBRID_INVOICES__*
    collection: str = "invoices"


class RecordInvoiceJob:
    name, payload_model = "record", InvoicePayload

    def __init__(self, task):
        self.task = task

    def lock_key(self, p):
        return f"{p.tenant_id}:{p.invoice_id}"

    async def run(self, p):
        await self.task.invoices.replace_one({"_id": p.invoice_id}, p.model_dump(), upsert=True)
        return ok(invoice=p.invoice_id)


class InvoicesTask:
    name = queue = "invoices"

    def __init__(self, ctx: TaskContext):
        options: InvoicesSettings = ctx.options
        self.invoices = ctx.mongo()[ctx.settings.mongo.database][options.collection]
        self.jobs = {"record": RecordInvoiceJob(self)}

    async def ensure_schema(self): ...

    async def close(self): ...


class InvoicesTaskFactory:
    name = queue = "invoices"
    settings_model = InvoicesSettings
    schedules = [Schedule(name="invoices.sync", job=JobSpec(task_type="invoices", kind="sync"), every_seconds=900)]

    def build(self, ctx):
        return InvoicesTask(ctx)
```

Register it from the package's `pyproject.toml`, and add the package to the
worker (a path source and a dependency in `apps/forge-async-worker/pyproject.toml`):

```toml
[project.entry-points."forge_async_worker.tasks"]
invoices = "forge_task_invoices.task:InvoicesTaskFactory"
```

Then `forge-async-worker tasks` lists it, and `forge-async-worker run
invoices record '{...}'` runs its jobs inline. The worker itself serves only
the `adk_workflows` queue, running each ADK workflow run on its run in the run
store: serving another task type's queue means giving the worker a job
function for it (and somewhere its runs are kept). The full example is
`NotesTask` in `tests/test_framework.py`.
