# forge-task-workflows

The `workflows` task type of the async worker: it runs an organization's workflows
(`forge.workflow/v1`, built in the web console's builder and kept by the admin
API) as tracked runs of the enhanced task framework, like every other job the
worker runs. A run is one of the organization's background tasks: it's followed,
retried, abandoned and, when it waits for a person, decided there.

Pure Python: every expression is JSONata, evaluated by Forge's own engine
(`packages/python/jsonata`), with no JavaScript runtime.

## How a run starts

`POST /organizations/{organization}/workflows/{id}/runs` on the admin API (the builder's Run
button) checks the input against the start step's fields and submits one job,
`workflows.run`, on the `workflows` queue. The job carries:

- the workflow's document **as it was saved when the run started**: editing
  the workflow never changes a run in progress;
- the input, and the member the run **acts as** (whoever ran it): run-workflow
  steps act with their permissions;
- labels to find it by: `workflow`, and `parent` for a run another run started.

Before the first step, the run compiles every expression and `{{ }}` template
in the document (`checks.py`) and stops with the whole list if any is broken.

## How a run goes

`engine.py` walks the graph from the start step: each step's executor
(`nodes/`) returns the way out it took and its output, and the walk follows
that way's connections. A step can read `input`, `steps.<id>.output` (and
`.error` where it can fail its way), `previous` (what the step before handed
on; logic steps hand on what came into them) and, inside a loop, the item and
`index`.

**Conditions and references.** Type `{{` in a setting to pick a field with
autocomplete. In expression settings, including If conditions, references
keep their value's type: `{{ steps.classify.output.score }} > 9` compares a
number, and `{{ previous.kind }} = "bug"` compares text. Use `=` for equality
(not `==`), `and`/`or` to combine conditions, and no quotes around the
reference itself. Bare paths such as `previous.score > 9` also work. In text
settings such as prompts, `{{ … }}` writes the value into the surrounding text.

**Replay.** Every step finished is recorded in the run's state, keyed by where
it ran (`frame/step#visit`), and kept with the task framework's checkpoint. A
run that resumes (after an approval, a wait, a restart of a failed attempt, a
redelivery after its worker died) walks the graph again from the start,
deterministically, and takes each recorded step's outcome instead of running
it again. So a step with side effects (an HTTP call, a child workflow) runs once
per visit, whatever happens to the worker.

**Waiting.** A step that waits for longer than a moment lets the worker go:

| Waits for | How |
|---|---|
| a person (Approval) | the run pauses at a task framework gate (`AWAITING_VALIDATION`); a decision through the admin API (Background tasks → Approve / Reject) resumes it on a worker, down the approved or rejected way. Rejecting carries the run on; it doesn't stop it |
| a time (Delay over `HYBRID_WORKFLOWS__INLINE_DELAY_SECONDS`) | the run stops (`STOPPED`, waiting) and a SAQ job scheduled for that time resumes it |
| another workflow | the run looks again every `HYBRID_WORKFLOWS__POLL_SECONDS` the same way |

Merges wait for every way into them (`all`) or go on at the first (`any`); a
way that can no longer arrive doesn't hold an `all` merge. Loops run their
body per item, several at once up to their concurrency, one at a time when
the body has a step that waits.

## Steps

| Kind | What it does | Ways out |
|---|---|---|
| Start | checks the input against its fields | next |
| Agent | the model it names from the shared model_provider.yaml (else the default), at its thinking level, with the step's instructions and what the step before handed on; a JSON answer is checked against its schema | next |
| Approval | waits for an organization admin or any member to decide; times out to Rejected when set | Approved / Rejected |
| HTTP request | calls a URL, with retries; never a private or loopback address unless allowed | Success / Error |
| Transform | a JSONata expression, checked against its declared output | next |
| Delay | waits a time | next |
| Run workflow | starts another of the organization's workflows as the member; waits for its result when told to | next |
| If / Switch / Match | route by a condition, a value, or the first rule that holds | their ways |
| Loop | runs its body per item of a list | done |
| Merge | joins ways | next |
| End | finishes the run, succeeded or failed, with a result | — |

A step that fails without an error way fails the run, naming the step.

## What it holds

No one's tokens. Child runs go through the admin API's service routes
(`/internal/workflows/...`), with `FORGE_WORKFLOWS_TOKEN`, naming the
organization and the member; the admin checks the member still may, and
starts the run (`services/admin.py`). Agent steps use the model provider configuration's
credentials (the shared model_provider.yaml, as the Forge assistant does),
else the shared Gemini key.

## Settings

`HYBRID_WORKFLOWS__*` (`config.py`): the admin API's URL and token, the
model provider configuration (or the Gemini key) and default model, HTTP limits (private networks, allowed hosts,
response size), JSONata's time and depth, and what one run may do (steps,
visits of one step, loop items, nesting of runs, output size).

## Running it

It's enabled in the worker's `HYBRID_ENABLED_TASKS`; Compose serves its queue
with `async-worker-workflows`. Tests run in the worker's environment:

```sh
cd packages/python/tasks/workflows
uv run --project ../../../../apps/forge-async-worker pytest
```

`tests/` runs whole workflows with fake services: the admin API, Gemini, an
HTTP transport and a clock. The worker's `tests/test_workflow_runs.py` runs
them through the task framework: pausing for an approval and deciding it
through the background tasks API, a long delay resumed by its scheduled job.
