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
     `{approved, decided_by, comment, decided_at}`, `decided_at` when it was
     made (the control's `Decision.decided_at`). The timeout, or a decision
     made at or after the deadline, is the timer's answer: rejected by Forge,
     "No decision within N hours". A decision is held to when it was made,
     not to when the run reads it: one made in time stands, however long its
     job waited.
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
   with `outcome`, `result`, `steps` and `session_id`), once no way of the
   run still waits. The result is the one ending's; when ways run at once (a
   Match taking every rule that holds, a step leading to several) and more
   than one ends, it's every ending, `[{step, name, outcome, result}]`, in
   the order they ended. A failed End doesn't stop the other ways: once every
   way has ended, the run fails naming each failed End (its message), with
   the first one's `step`; in a loop's body or a saved agent it fails the run
   at once. A `RunFailed` (input that doesn't fit the start, a step that
   failed with no way to take, a human input declined) fails it at once with
   its message and `step`. A
   document that doesn't build fails it at once, without a retry. A model's,
   the network's or the database's hiccup is a `TransientError`: the worker
   tries again, with backoff (no job takes the run before it ends).
5. **Interrupted.** When the run's invocation has events but no pending
   pause (the worker died mid-way, a hiccup, a failed run retried), it
   carries on where it stopped: ADK resumes the invocation, and a step that
   failed runs again. A step that was running runs again when that's safe
   (a GET, PUT or DELETE, a logic step, an agent, Create branch, Pi with
   tools that only read). One whose effect may not be (a POST or PATCH,
   Bash, Pi with tools that change things, Clone repository, Commit and
   push, Open pull request) recorded that it began before it acted, so it
   isn't: what it left running is stopped (its commands go with their
   worker anyway, `forge_common.processes`), and it takes its Error way
   with `may_have_run` in its error, or with none the run fails. Each step
   execution has a key, the same for every attempt of it: a POST or PATCH
   sends it as `Idempotency-Key`, Bash and Pi read it as
   `FORGE_IDEMPOTENCY_KEY`. A run never ends as a success it can't show: a
   step cut off that nothing ran again fails it, and without a finish it
   succeeds (with no result) only where a way ended at a way out that leads
   nowhere. When ADK can't resume it, the run
   fails, to be resubmitted; or, when its start says so (`start_over`), it
   starts over in a new session, `<session_id>-r<n>`, kept with the job and
   noted (with its `session_id`), and the result names it: its steps run
   again, side effects too.
6. **Tool calls.** Each call an LLM agent's tool takes is noted with the job,
   durably, before it runs (unless the tool only reads), until its answer is
   in the session. ADK runs a step its worker stopped mid-call again by
   asking its model afresh, so each attempt first answers, where they were,
   the calls an earlier one left unanswered, and the model sees what became
   of them: a workflow called as a tool with the run it started (a call
   starts one run, under an ID it makes for it, whatever becomes of the
   call), waited for as the tool waits; any other as cut off (it may have
   taken effect) or unanswered. An agent that sees only its own turn
   (`include_contents` none, as agents from the Agents page are) sees no
   answer an earlier attempt's call got, whether it went through or was cut
   off, when its step runs again (after a model's hiccup, a stop, a retry):
   each of its calls that took effect, or may have, is journaled with the
   job (`replays`), durably, until its step ends, and the same call again
   (the same tool, arguments and agent, at the same step) is answered as it
   was, once each attempt, instead of running. A run started this way names
   the run that started it, and abandoning that run abandons it while it's
   going.

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
| `CONTEXT_COMPACTION`, `CONTEXT_COMPACTION_THRESHOLD` | true, 0.8 | Each LLM agent's context is kept in the run's state (`forge:context:<agent>`); one whose request reaches the threshold's share of what its model may read (`contextWindow` less `maxTokens`) is compacted in the middle of its step: its earlier steps summarized by its own model, its input and last step kept (`forge_common.adk.context_window`). Off: counted, compacted only when a tool asks. |
| `HTTP_ALLOW_PRIVATE` | false | Whether HTTP nodes may reach private, loopback and link-local addresses. |
| `HTTP_ALLOWED_HOSTS` | `[]` | Hosts HTTP nodes may always reach (a JSON array). |
| `HTTP_MAX_RESPONSE_BYTES` | 5 MiB | The most of a response an HTTP node reads. |
| `EXPRESSION_TIMEOUT_MS`, `EXPRESSION_DEPTH` | 2000, 300 | JSONata's limits, for one evaluation. Every evaluation runs on a thread of its own pool (`support.expressions`), never on the worker's event loop. Its regular expressions run on the `regex` engine (forge-common's `forge_common.patterns`, through `support.patterns`), not Python's `re`: each lets go of the interpreter lock while it matches and fails its step past the timeout, where `re` would hold the lock for as long as it backtracks; one that would hold the lock too long compiling (`((a{1000}){1000}){1000}`) fails its step before it's compiled. A JSON Schema's patterns in a run (a fallback Rust's regex engine doesn't read, a called workflow's input, an Agents page agent's inputs) are held to half a second the same way, all of a value's together, whichever keyword matches them. |
| `EXPRESSION_LIST_TIMEOUT_MS` | 30000 | The most a condition read for each item of a list (a filter's) takes in all; past it, the step fails. It goes through the items in batches of at most 50 ms, the event loop free (and the step cancellable) between them. |
| `MAX_LOOP_ITEMS` | 10000 | The most items one loop goes through. |
| `INLINE_DELAY_SECONDS` | 20 | Delays up to this wait in place; longer ones pause the run. |
| `RUN_TIMEOUT` | 3600 | The most seconds a run goes on at a time (from its start or an answer to its end or next pause) before it fails. |
| `ATTEMPT_TIMEOUT` | twice `RUN_TIMEOUT` | The most seconds one attempt of a run takes in all, closing what it opened included. Past it the worker stops the attempt and the run is tried again later, failing once out of attempts. |

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
