# Integration suite findings

What the integration suite (this folder; `make async-worker-test-integration`)
found on 2026-10-18, running the run machinery of `main` (e3cad29) against
MySQL 8.4 (REPEATABLE READ) and Redis 8.8 with real worker processes; and,
the same tests on the main checkout's uncommitted working tree of that day
(its venv, its migrations to 0032), the same: 20 passed, the same 12 xfailed,
with the same numbers within a second. Each
finding's tests are marked `xfail(strict=True)` with its ID: a fix turns
them XPASS, which fails the suite until the marker comes off. Timelines are
trimmed from the scenarios' reports (`.reports/<test>.txt`); times are
seconds into the scenario. Workers run with a 6 s lease (the real one is
120 s): every other timing in jobs.py is a fraction of it. A fixed
finding's tests have lost their marker and pass: its section says what
changed.

| ID | Finding | Scenario | Tests |
|----|---------|----------|-------|
| [IT-1](#it-1) | Fixed: a new member's first runs started together fail on ADK's state rows | (a) | `test_a_claim_race.py::test_a_new_members_first_runs_started_together_all_run` |
| [IT-2](#it-2) | Fixed: an HTTP step cut off mid-request was skipped when its run carried on: the run succeeded without its End | (b), (g) | `test_b_kill_recovery.py::test_a_run_killed_mid_post_…`, `test_g_sigterm.py::test_a_run_let_go_at_shutdown_goes_on_to_its_end` |
| [IT-3](#it-3) | Fixed: a killed worker's Bash command kept running beside the step's rerun | (b) | `test_b_kill_recovery.py::test_a_run_killed_mid_bash_step_…` |
| [IT-4](#it-4) | Fixed: an attempt whose lease ran out still wrote activity notes and usage rows | (c) | `test_c_lease_fencing.py::test_an_attempt_whose_lease_ran_out_notes_nothing`, `…_records_no_usage` |
| [IT-5](#it-5) | Fixed: an attempt whose lease ran out still appended to the ADK session, and failed the attempt that took its run | (c) | `test_c_lease_fencing.py::test_an_attempt_whose_lease_ran_out_appends_nothing_to_the_session`, `…_cant_fail_the_attempt_that_took_its_run` |
| [IT-6](#it-6) | Fixed: a parent waiting on its child held the worker slot the child needs | (f) | `test_f_child_starvation.py::test_one_parent_on_a_worker_with_one_slot_…`, `test_two_parents_on_a_worker_with_two_slots_…` |
| [IT-7](#it-7) | Fixed: a worker draining for shutdown kept taking new runs | (g) | `test_g_sigterm.py::test_a_worker_that_is_stopping_takes_no_new_runs` |
| [IT-8](#it-8) | Fixed: while Redis is away, each worker spins a CPU core | (h) | `test_h_redis_outage.py::test_workers_dont_spin_while_redis_is_away` |

## IT-1

**A new member's first runs started together fail on ADK's state rows.**
Twelve runs of a member with no ADK user state yet, started at once on three
workers: 11 failed, for good.

```
The run failed: IntegrityError: (pymysql.err.IntegrityError) (1062, "Duplicate entry
'adk_workflows-user-newcomer' for key 'user_states.PRIMARY'")
```

ADK 2.10's `DatabaseSessionService.create_session` makes the app's and the
user's state rows on first use (`_get_or_create_state`): it inserts in a
SAVEPOINT and, on an IntegrityError, reads the row again. On MySQL's
REPEATABLE READ that second read is, as far as the code shows, a consistent
read of the snapshot the transaction's first read took, before the other
insert committed, so it finds nothing and raises (the error above is that
re-raise). `runs._classified` doesn't count an IntegrityError
as a hiccup, so the run fails rather than being tried again. The same race
hits `app_states` on a database where no run ever ran (the suite saw it on
its first concurrent test). SQLite never shows it (one connection, no
snapshots). The other scenarios make the rows first (`Scenario.warm_sessions`)
so they test what they mean to.

**Fixed.** The worker's session service (`task.build_sessions`) opens its
engine at READ COMMITTED (`task.SESSION_ISOLATION`; SQLite aside): the
re-read after a lost insert sees the winner's committed row. All 12 runs
now succeed, each at its first attempt (the test checks both). Should a
session database still race (one whose isolation isn't the worker's to
set), an IntegrityError while a run opens its session is a hiccup
(`runs._classified(opening=True)`): the run is tried again and finds the
row. READ COMMITTED needs MySQL's binary log in ROW format (8.0's default)
if the log is on.

## IT-2

**An HTTP step cut off mid-request is skipped when its run carries on; the
run succeeds without its End.** Confirms the 2026-10-18 runtime review's
critical finding on real processes. `kill -9` of the worker while the step's
POST is in flight:

```
 4.573s  run ead69ab4   started: A worker started it [18277 epoch 1]
 4.732s  mock           POST charge (held)
 4.785s  test           kill -9 worker w1 (pid 18277)
12.563s  run ead69ab4   recovered: Its worker went while it ran: queued again
12.575s  run ead69ab4   resumed: A worker carries it on [18278 epoch 2]
12.684s  run ead69ab4   note: The run carries on where it stopped
12.693s  worker w2      [warning] Node 'charge' has conditional/DEFAULT edges but none were matched
                        by the emitted route(s): None. The branch will end.
12.702s  run ead69ab4   succeeded: Succeeded
```

Status `succeeded`, result `None`, one `charge` request: the step neither ran
again (at least once, as runs.py promises: "a step that was running ... runs
again") nor failed the run. The HTTP step's node (graph/factories/actions.py)
yields its `called_with` event before it sends, without `rerun_on_resume`,
so ADK counts it as having run; it routes nowhere, the branch ends, and
`_carry_on` takes a graph with no finish for `succeeded`. SIGTERM past the
grace period does the same (scenario (g), `g_sigterm_step_outlasts_the_grace_period`):
let go, taken again at once, `succeeded` with result `None`. So does a
model's 503 on another way of the run, with no infrastructure event at
all: ADK cancels the POST's way as the hiccup stops the run, and the next
attempt skips the POST.

**Fixed** (2026-10-18). A step that was running when its run stopped is
neither skipped nor run again blindly. Every step whose effect may not be
safe to repeat records that it began, durably, before it acts (an event of
its node's, `STARTED_KEY`, with its key and machine: the HTTP step's is the
`called_with` event it kept already), and is run again when the run carries
on (`rerun_on_resume`), so it decides its own fate (`factories.base.attempt`):

- a GET, PUT or DELETE is sent again; Create branch, logic steps, agents
  (whose cut-off tool calls the call journal answers already) and Pi with
  tools that only read run again;
- a POST or PATCH, Bash, Pi with tools that change things, Clone
  repository, Commit and push and Open pull request don't: what they left
  running is stopped (IT-3), and they take their Error way with
  `may_have_run` and the step's `idempotency_key` in `steps.<id>.error`, or,
  with no Error way, the run fails, saying so.

Each step execution has a key, the same for every attempt of it and another
once an attempt ended and the run was tried again: a POST or PATCH sends it
as `Idempotency-Key` (unless the step sets one), Bash and Pi read it as
`FORGE_IDEMPOTENCY_KEY`. And `_carry_on` fails closed (`AdkRun._ended`): a
step that began with nothing after it saying how it ended fails the run,
finish or not, and a run without a finish succeeds (with no result, noted)
only where a way ended at a way out that leads nowhere, by the run's graph's
edges; it never defaults to `succeeded`. The same scenario now:

```
 4.164s  mock           POST charge (held)
 4.193s  test           kill -9 worker w1 (pid 62720)
10.963s  run 80a6e030   recovered: Its worker went while it ran: queued again
10.976s  run 80a6e030   resumed: A worker carries it on [62721 epoch 2]
11.096s  run 80a6e030   note: The run carries on where it stopped
11.127s  run 80a6e030   failed: Failed: Charge (charge) failed: POST http://127.0.0.1:59459/effect/charge
                        was cut off: the run stopped while it waited for the answer, so it may have taken
                        effect. A POST isn't sent again on its own: check whether it did
         observed       charge requests: 1
```

With an Error way the run takes it instead (adk-workflows
`tests/test_cut_off_steps.py`, which also has the model's 503 on another
way). Scenario (g)'s step is now a GET, which is sent again: the run goes on
to its End.

## IT-3

**A killed worker's Bash command keeps running beside the step's rerun.**

```
 4.263s  run 44e42cd0   started: A worker started it [18421 epoch 1]
 4.416s  mock           POST bash-began          (the command, pid A)
 4.469s  test           kill -9 worker w2 (pid 18421)
 8.434s  mock           POST bash-ended          (pid A: 4 s after its worker died)
10.287s  run 44e42cd0   recovered: Its worker went while it ran: queued again
10.303s  run 44e42cd0   resumed: A worker carries it on [18420 epoch 2]
10.436s  mock           POST bash-began          (the rerun, pid B)
14.455s  mock           POST bash-ended          (pid B)
14.494s  run 44e42cd0   succeeded: Succeeded
```

`support/commands.run_command` starts each command in a session of its own
(`start_new_session=True`), so nothing ends it with its worker: it goes on
to its end, effects and all, while (or before) the recovered attempt runs it
again. The run itself recovers properly (the step runs again, the run ends
with the rerun's output); the effect happens twice, the two copies
overlapping when the command outlasts the lease. Fix session: worker
shutdown (a command's process group should die with its worker, e.g. a
parent-death signal or a supervisor).

**Fixed** (2026-10-18). Every command a step runs (Bash, the GitHub steps'
git, Pi) is tied to its worker (`forge_common.processes.Tie`): it starts
under a small watcher in its process group, which holds the reading end of
a pipe the worker holds the other end of. When the worker goes, however it
goes, the kernel closes its end, and the watcher kills the group. (Linux's
`PR_SET_PDEATHSIG` reaches one process, not what it started, and macOS has
none; the pipe works on both, checked on macOS and in `python:3.13-slim`.)
The watcher's arguments carry the step's key, so the run's next attempt on
the same machine finds and kills what a worker that froze rather than died
left running (`commands.stop_leftovers`), and an OpenShell sandbox carries
it as a label (`forge-step`), so the next attempt deletes it, from any
worker. Then the step takes its Error way, or the run fails (IT-2): a
command cut off isn't run again. The same scenario now:

```
 4.689s  mock           POST bash-began          (the command)
 4.706s  test           kill -9 worker w1 (pid 62838)
11.439s  run 62016b02   recovered: Its worker went while it ran: queued again
11.581s  run 62016b02   note: The run carries on where it stopped
11.670s  run 62016b02   failed: Failed: Work (work) failed: Its command was cut off: the run stopped
                        while it ran, so it may have run, in whole or in part. It isn't run again on
                        its own: check what it did
         observed       commands begun 1, ended 0; effects after its worker died: 0
```

## IT-4

**An attempt whose lease ran out still writes activity notes and usage
rows.** Clock-skew scenario (`c_lease_ran_out_by_clock_skew`): the
maintenance runs with a clock 65 s ahead (lease 60 s), finds the holder's
lease run out, and another worker takes the run and finishes it; then the
holder's model call answers.

```
 4.405s  run a42fe63c   started: A worker started it [19881 epoch 1]
 7.029s  mock           POST llm (held)                   (w2's model call)
 7.069s  run a42fe63c   resumed: A worker carries it on [19880 epoch 2]
 8.927s  mock           POST llm                          (w1's)
 9.018s  run a42fe63c   succeeded: Succeeded
         test           the mock answers w2's model call
 9.126s  worker w2      [warning] a run stopped: The session has been modified in storage since it was loaded
 9.139s  run a42fe63c   note: The run failed: StaleSessionError: The session has been modified ...
 9.147s  worker w2      [warning] run a42fe63c… is dropped: … is no longer …'s, or its lease ran out
```

The run row is fenced (status, result and state stay the new attempt's;
the step after doesn't run twice), but `RunStore.note` and
`UsageStore.record` aren't fenced at all: a succeeded run's activity says
"The run failed", and its usage counts a model call twice. The old attempt
never noticed its lease was gone (on its own clock it held it): it ran
until its first fenced write failed, here 2 s after the new attempt took
the run.

A worker frozen past its lease (SIGSTOP, then SIGCONT once another worker
had finished the run) doesn't write anything: faulthandler's backstop ends
it 0.05 s after it goes on (exit 1, the watchdog's message beside it).

**Fixed** (2026-10-18). A worker's notes carry its attempt's owner
(`RunControl.note`), and `RunStore.note` writes one only under
`RunStore.hold`: in the note's transaction, the run's row read `FOR SHARE`
where it's running under that owner's lease and the lease hasn't run out,
else `LostLease`. The worker's usage sink records each call with the same
fence in the insert's transaction (`UsageStore.record(fence=...)`) and drops
a refused one. The same scenario now:

```
 8.449s  run 893de190   succeeded: Succeeded
         test           the mock answers w1's model call
 8.492s  worker w1      [info] a model call of run 893de190… isn't recorded: its attempt lost the run
 8.511s  worker w1      [warning] run 893de190… is dropped: … is no longer …'s, or its lease ran out
         observed       activity, usage rows and session events written after the takeover: none
```

## IT-5

**An attempt whose lease ran out still appends to the ADK session, and
fails the attempt that took its run.** As IT-4, but the old attempt's model
call answers while the new attempt's own is still out
(`c_lease_ran_out_mid_step_of_the_next`):

```
 6.651s  mock           POST llm (held)                   (w2's: its lease runs out next)
 6.717s  run de1e9b92   resumed: A worker carries it on [20800 epoch 2]
 8.639s  mock           POST llm (held)                   (w1's)
         test           the mock answers w2's model call first
 8.701s  worker w2      [warning] run de1e9b92… is dropped
         test           then w1's
 8.901s  worker w1      [warning] a run stopped: The session has been modified in storage since it was loaded
 8.919s  run de1e9b92   failed: Failed: The run can't carry on where it stopped (StaleSessionError: …).
                        Resubmit it to run it again from the start
```

The old attempt's event lands in the run's session; the new attempt's next
append is then refused by ADK's own stale-session check, and the run
fails. Nothing fences ADK's `append_event` on the run's lease; the stale
check refuses whichever writer comes second, which may be the attempt that
holds the lease. In the IT-4 scenario the old attempt's append was refused
only because the new one had already written.

**Fixed** (2026-10-18). On the worker, a run's ADK sessions are wrapped
(`FencedSessions`, adk-workflows' fenced_sessions.py): each append happens
inside `RunStore.holding`, which holds the run's row `FOR SHARE` (owner and
lease, as `hold`) from before ADK's append until after it commits. No
other worker can take the run, and the maintenance can't queue it again,
while an append is landing. A refused append raises `LostLease`, which
runs.py passes on as it is (it's no failure of the run's), and the attempt
is dropped. The same scenario now:

```
 5.687s  run c035d5f4   resumed: A worker carries it on [… epoch 2]
         test           the mock answers w1's model call first (its lease gone)
 7.609s  worker w1      [warning] run c035d5f4… is dropped: … is no longer …'s, or its lease ran out
         observed       session events the old attempt appended: 0
         test           then w2's
 7.889s  run c035d5f4   succeeded: Succeeded                ('after' ran once)
```

## IT-6

**A parent waiting on its child holds the worker slot the child needs.** An
LLM agent's workflow tool starts a child run and polls for it
(`RunWorkflows.attach`) for `workflow_tool_wait` (8 s here), in the
parent's slot:

| worker slots | parents | the tool answered | child queued for | child ran while its parent waited |
|---|---|---|---|---|
| 2 | 1 | `succeeded` after 0.5 s | 0.0 s | yes |
| 1 | 1 | `queued` after 8.1 s | 8.1 s | no |
| 2 | 2 | `queued` after 8.1 s (both) | 8.2 s (both) | no |

When every slot holds a waiting parent, each child waits out its parent's
whole wait; the parent then succeeds without its child's result (its model
is told the child is `queued`), and the child runs afterwards. With the
real 120 s wait and concurrency 4, four parents stall a worker for two
minutes each time. Fix session: child runs as gates.

**Fixed** (2026-10-18). In the run's own invocation the workflow tool is
long-running (`forge_agent_runtime.WorkflowTools`, as
`RunWorkflows.waits_apart` says): its call starts the child and returns
nothing, so ADK pauses the LLM agent at the call, and the parent pauses at
a gate of its own (`child_run`, `details.child_run_id`; graph/pauses.py
finds the call, runs.py `_waited_for` makes the gate), its lease and slot
given back. Nobody decides it: the child's terminal write (`finish`,
`fail_interrupted`, `abandon`, `abandon_running`, `abandon_stopping`)
decides the parent's gate and queues the parent in the same transaction
(`RunStore._child_ended`, the parent's row locked first, so the lock order
is always ancestor first), and the worker sends the parent a job. A child
that ended before the parent paused is found as it pauses (`pause`, the
child's row read `FOR SHARE`): the parent is queued at once. The gate's
deadline is the call's wait from the child's start (`trigger.wait_seconds`),
timed out by `expire_pause` or the maintenance as any gate's. The model is
answered as the tool answered before, `{status: "success", payload: {run_id,
status, result | error}}`: how the child ended, or where it got to once the
wait is up. A sub-agent's own runner (an Agents-page agent's sub-agent tool)
can't pause the run: a workflow call there still waits in place. The same
scenarios now:

| worker slots | parents | the tool answered | child queued for | child ran while its parent waited |
|---|---|---|---|---|
| 2 | 1 | `succeeded` after 0.2 s | 0.0 s | yes |
| 1 | 1 | `succeeded` after 0.2 s | 0.0 s | yes |
| 2 | 2 | `succeeded` after 0.3 s and 0.2 s | 0.0 s (both) | yes |

```
 5.974s  run 65a42e9f   created: Started by run 78dd2b16
 5.987s  run 78dd2b16   waiting: Waits for Child (run 65a42e9f) to end
 5.991s  worker w1      [info] run 78dd2b16…: paused                 (its one slot given back)
 5.994s  run 65a42e9f   started: A worker started it [67482 epoch 1]
 6.095s  run 78dd2b16   child_ended: Run 65a42e9f it waits for succeeded: it carries on
 6.095s  run 65a42e9f   succeeded: Succeeded
 6.102s  run 78dd2b16   resumed: A worker carries it on [67482 epoch 2]
 6.117s  run 78dd2b16   note: Orchestrate (orchestrate): its call to child is answered: run 65a42e9f succeeded
 6.173s  run 78dd2b16   succeeded: Succeeded
```

## IT-7

**A worker draining for shutdown keeps taking new runs.**

```
 3.873s  run 4399918b   started: A worker started it [23949 epoch 1]
 4.007s  mock           POST busy (held)
 4.022s  test           SIGTERM to worker w1 (pid 23949)
 4.022s  worker w1      [info] stopping: waiting up to 8s for running jobs
 4.540s  test           started run de0ff9a2
 4.547s  run de0ff9a2   started: A worker started it [23949 epoch 1]
 4.689s  run de0ff9a2   succeeded: Succeeded
```

`saq_worker.run_workers` waits for running jobs (`_drain`) before it stops
SAQ, and SAQ's workers go on dequeuing meanwhile: whatever they take is cut
off at the end of the grace period (and then meets IT-2). Fix session:
worker shutdown.

**Fixed** (2026-10-18). On SIGTERM `run_workers` first stops its workers
taking jobs (`PatientQueue.stop_taking`), then drains. A dequeue waits on
Redis `TAKE_SECONDS` (1 s) at a time rather than for good, so one out as the
worker stops comes back within a second; a job it took meanwhile goes back
to the head of the queue (`_put_back`: LREM from the active list and LPUSH
onto the queued one, in one transaction), and from then on each of the
worker's dequeues waits, parked, taking nothing, until SAQ's stop cancels
it. The drain waits until every one of them is parked (up to the grace
period, and at least a dequeue's way back), so none is cut off holding a job
it took. With it, in the same change: a run let go at shutdown is queued with
a `run_adk` of its own (`_let_go`), not only through SAQ's re-queue of the
job it cut off, which may come first and skip the run, still running; being
cut off is an interruption, counted apart from attempts
(`adk_runs.interruptions`, migration `0035adk_run_interruptions`: a run
fails for interruptions only after 25 in a row with its worker gone, never
for a worker stopping); and `run_adk` has no SAQ heartbeat (whether its
attempt is alive is its lease's to say), its SAQ timeout what its attempt
may take and ten minutes, so SAQ's sweep takes only a dead worker's job. The
same scenario now:

```
 4.114s  run eb492bce   started: A worker started it [62738 epoch 1]
 4.297s  mock           POST busy (held)
 4.315s  test           SIGTERM to worker w1 (pid 62738)
 4.315s  worker w1      [info] stopping: taking no more jobs, waiting up to 8s for running ones
 4.830s  test           started run cf5550cf
 4.834s  worker w1      [info] queue adk_workflows: job adk-run:cf5550cf…:9afbbb0c goes back to its queue, as this worker stops
                        (twice more by 4.840s: each dequeue out takes it once, then waits)
 7.846s  test           observed new run, 3s after SIGTERM: queued, taken by nobody
 7.944s  run eb492bce   succeeded: Succeeded
 8.563s  worker w1      exited with 0
```

## IT-8

**While Redis is away, each worker spins a CPU core.** Redis stopped for
120 s under two workers (concurrency 4 each): each used 114.9 s of CPU in
those 120 s, while writing only a few log lines a second (the upkeep's
"Upkeep task failed unexpectedly", once a second per upkeep task). SAQ's
`Worker.process` catches a failed dequeue, returns, and its done-callback
starts the next dequeue at once: with Redis refusing connections, each of
the worker's process tasks (its concurrency, and the upkeep worker's two)
loops with no pause. Nothing else went wrong (see (h) below): the runs come
through, and the workers recover by themselves.

**Fixed.** The worker's queues are `saq_worker.PatientQueue`s: a failed
dequeue waits before it says so, as long as the queue has been failing so
far (0.5 s to 2 s), and once Redis answers a ping dequeues go on at once
(one warning when a queue can't be read, one line when it can again).
SAQ's own `poll_interval` only paces its Postgres queue. The same outage
now costs each worker 1.0 to 1.2 s of CPU in its 120 s, and the four runs
end within 2.0 s of Redis being back (a dequeue's last pause, at most 2 s).

## What holds

- **(a) Claims.** 30 runs, each claimed by 4 claimers at once on connections
  of their own: one claim each. 10 runs, each given to 3 worker processes
  at once: each ran once (20 jobs found it taken), one `started`, epoch 1.
- **(c) The run row's fence**, and a frozen worker resuming past its lease
  (see IT-4); and, since IT-4 and IT-5 were fixed, the attempt's notes,
  usage rows and ADK session events too.
- **(d) Gates.** Two decisions at once on 10 approvals (approve on one
  connection, reject on another): one landed each time, both ways seen
  (3/7); each run then took its decision's way, once. A decision and an
  abandonment at once on 10 approvals: every run ended abandoned, both
  orders seen (5/5), nothing ran.
- **(e) Abandon racing completion.** 24 runs abandoned 0 to 2.5 times the
  completion window after their last step answered (the window, from the
  answer to the run's end, is 35 ms here): each ended one way, saying so
  once, its lease cleared; both outcomes seen (one run: 17 abandoned and 3
  succeeded of 20; another: 10 and 14 of 24); an abandonment refused only
  once the run had finished; one accepted as the run finished left it
  succeeded, its abandonment cleared ("it ended first", as `finish` means
  it to).
- **(g) SIGTERM.** A step that ends within the grace period ends its run on
  that worker, which then exits 0. A step that outlasts it: the run is let
  go and taken again by the other worker at once (0.0 s), at the same
  attempt (an interruption isn't one), by the job `_let_go` queues once the
  run is queued again; SAQ's `job.retry("cancelled")` re-queues the cut-off
  job too, and whichever comes second finds the run taken. (Before, `_let_go`
  queued nothing, so a job that took longer than SAQ's one-second
  cancellation deadline to stop left its run to the maintenance.) A worker
  that is stopping takes no new runs (IT-7).
- **(h) A Redis outage of 120 s** (stopped, then started with its
  append-only file) while runs went on, the workers running their own
  maintenance every 5 s: a run in flight finished in MySQL during the
  outage (SAQ couldn't mark its job done: logged, harmless); a decision
  made, and a run started, during the outage (their jobs couldn't be
  queued) were found stalled by the first maintenance after Redis was back
  and ran; a delay that ended during the outage woke by its scheduled job.
  All four succeeded within 1.0 s of Redis being back, each effect once;
  both workers lived through it (but see IT-8). Not played here: a run still
  in flight once Redis is back after more than 120 s. Its job's touches
  failed meanwhile, and SAQ's sweep, by `run_adk`'s heartbeat of 120 s,
  would have aborted it, cutting off an attempt that held its lease;
  `run_adk` has no heartbeat since (queue.py's `HEARTBEAT`).
