# forge-task-adk-workflows

The `adk_workflows` task type of the async worker: it runs an organization's
ADK workflows (`forge.agent/v1`, built on the web console's ADK workflows page
and kept by the admin API) on Google ADK's graph engine. Each run is kept in
the run store (`run_store.py`): two tables in the admin MySQL, beside the
runs' ADK sessions, which the admin API starts, lists and acts on and the
worker runs.

- `graph`: builds a document into one ADK `Workflow`. It has one higher-order
  function per kind of node (`graph/factories`), and JSON Schemas become
  Pydantic models (`graph/schemas.py`) that ADK enforces.
- `task.py`: `AdkWorkflowsTaskFactory`, registered with the worker
  (`forge_async_worker.tasks` entry point `adk_workflows`), on the
  `adk_workflows` queue, with one job, `run`.
- `runs.py`: one run, carried on from its ADK session every time the job runs.
- `run_store.py`: `RunStore`, the runs (`adk_runs`) and their activity
  (`adk_run_events`): their status (queued, running, paused, waiting,
  succeeded, failed, abandoned), payload, what they keep, decisions, pause or
  wait, result or failure, and the worker's lease. The admin API's migration
  `0005adk_run_store` creates the tables.
- `models.py`: the models LLM nodes run on.
- `steps.py`: a run's steps, as its run page shows them.
- `support`: what the Forge step kinds are built on: their settings, JSONata
  (on `forge-jsonata`, with typed `{{ }}` references), the HTTP guard that
  keeps requests off the worker's own network, and step errors.

## A run

The admin API creates the run in the run store, queued, with the payload
below, and queues `run_adk` (`{"run_id": ...}`) on the `adk_workflows` queue;
the worker runs the task's `run` job on it:

| Field | What |
| --- | --- |
| `tenant_id` | The organization. |
| `agent_id`, `revision`, `version`, `name` | The ADK workflow, its revision, which version ran (`"draft"` or a published one's number) and its name when the run started. |
| `document` | The ADK workflow as it was then: a run is pinned to it. |
| `saved` | The saved ADK workflows it runs (Saved nodes, workflow tools), by reference (`ag_x`, `ag_x@3`, `ag_x@draft`), each at the version named, as they were then. |
| `input` | The run's input, held to its start's input schema. |
| `session_id` | The run's ADK session, made by the admin API. |
| `run_as`, `run_as_name` | The member it acts as: the session's user. |
| `trigger` | How it started (`{"kind": "manual"}`). |

Every attempt of the run (the first, each one after a decision or a wait,
each retry) runs `RunAdkWorkflowJob.run`, which carries the run on from its
session:

1. **Start.** The session (app `adk_workflows`, user `run_as`, ID
   `session_id`) is created if it isn't there, and the graph runs on the
   input, sent as a JSON text message, until it ends or pauses. The run is one
   ADK invocation of that session, whose ID comes from the run's: a
   resubmitted run (same payload, same session) is another invocation of the
   session, run afresh; the session's latest invocation is the latest run's,
   which is what the run page shows.
2. **Pauses.** A node that waits asks for input (ADK's `RequestInput`). The
   oldest pending pause of the run is asked of its control, then answered in
   the session, which resumes the same invocation; finished steps don't run
   again. A run waits at one question at a time, so pauses in parallel ways
   are answered one after another.
   - **Approval**: an approval gate, `control.approval(key=<interrupt id>,
     reason=<message>, details={kind: "approval", approvers, expires_at, step,
     step_name, workflow_name, agent_id, message, session_id, interrupt_id},
     timeout_seconds=<until expires_at, and a second>)`. The decision answers
     `{approved, decided_by, comment, decided_at}`. The timeout, or any
     decision at or after the deadline, is the timer's answer: rejected by
     Forge, "No decision within N hours".
   - **Human input**: the same gate, with `details={kind: "human_input",
     response_schema, ...}`. The admin API holds the answer to the response
     schema and approves the gate with the answer as JSON in its comment.
     Declined, or anything but a JSON object, fails the run.
   - **Delay**: once its time has come (or is within `inline_delay_seconds`,
     slept in place), the timer wakes it; before that the run lets the worker
     go until then (`control.wait_until`: the worker queues the run for
     then).

   A gate nobody has decided yet ends the attempt by raising the control's
   signal; its decision runs the job again.
3. **Progress.** Each step of the document that finishes (and each loop
   item's) is a note on the run's activity (`Transform (shape) finished`), and
   a checkpoint.
4. **End.** The graph's finish, `{outcome, result}`, is the job's result (`ok`
   with `outcome`, `result`, `steps` and `session_id`). A `RunFailed` (input
   that doesn't fit the start, a failed End, a step that failed with no way to
   take, a human input declined) fails it with its message and `step`. A
   document that doesn't build fails it at once, without a retry. A model's,
   the network's or the database's hiccup is a `TransientError`: the worker
   tries again, with backoff.
5. **Interrupted.** When the run's invocation has events but neither a pending
   pause nor a finish (the worker died mid-way, a hiccup, a failed run
   retried), it carries on where it stopped: ADK resumes the invocation, and
   a step that was running, or failed, runs again. When ADK can't, the run
   starts over in a new session, `<session_id>-r<n>`, kept with the job and
   noted (with its `session_id`), and the result names it; side effects may
   then repeat.

LLM nodes run on the worker's models (`models.py`): the shared model provider
configuration's, as one ADK model (`forge_common.adk.models.ProviderModels`).
Each LLM agent's `before_model_callback` (`graph.services.model_selection`)
runs its requests on the model its settings pick (`provider/model`; the
default when none, or one the worker doesn't offer) with its thinking level,
and the model timeout.

## Settings

`HYBRID_ADK_WORKFLOWS__*`, in `apps/forge-async-worker/.env` (documented in
its `.env.example`), read into `config.AdkWorkflowsSettings`:

| Setting | Default | What |
| --- | --- | --- |
| `SESSION_DATABASE_URL` | none | Where sessions are kept: the admin MySQL, `mysql+aiomysql://user:password@host:3306/forge_admin`. ADK creates its tables on first use. Unset, runs fail saying so. |
| `MODEL_PROVIDER_CONFIG` | none | The shared model-provider YAML LLM nodes run on; its `${NAME}` references resolve from the environment, then `.env`. |
| `GOOGLE_API_KEY` | none | Without a YAML, Gemini with this key. Neither: LLM nodes fail the run saying so. |
| `DEFAULT_MODEL` | the YAML's | The model a node that picks none runs on. |
| `MODEL_TIMEOUT` | 180 | Seconds one model call may take. |
| `HTTP_ALLOW_PRIVATE` | false | Whether HTTP nodes may reach private, loopback and link-local addresses. |
| `HTTP_ALLOWED_HOSTS` | `[]` | Hosts HTTP nodes may always reach (a JSON array). |
| `HTTP_MAX_RESPONSE_BYTES` | 5 MiB | The most of a response an HTTP node reads. |
| `EXPRESSION_TIMEOUT_MS`, `EXPRESSION_DEPTH` | 2000, 300 | JSONata's limits. |
| `MAX_LOOP_ITEMS` | 10000 | The most items one loop goes through. |
| `INLINE_DELAY_SECONDS` | 20 | Delays up to this wait in place; longer ones pause the run. |
| `RUN_TIMEOUT` | 3600 | The most seconds a run goes on at a time (from its start or an answer to its end or next pause) before it fails. |

## Tests

They run in the workspace's environment (`make async-worker-check` runs them
with lint, format and type checks):

```bash
uv run pytest
```

`tests/test_task.py` runs the job as the worker does, with ADK's
`DatabaseSessionService` on SQLite, `LocalJobControl`, scripted models and a
fake HTTP transport; `tests/test_run_store.py` the run store on SQLite. The
worker's tests run the job on the run store. `tests/test_expressions.py` checks
the `{{ }}` references the builder writes against
`tests/fixtures/expression-references.json`, which the web console's tests
read too.
