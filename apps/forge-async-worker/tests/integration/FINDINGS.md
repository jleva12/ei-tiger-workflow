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
| [IT-2](#it-2) | An HTTP step cut off mid-request is skipped when its run carries on: the run succeeds without its End | (b), (g) | `test_b_kill_recovery.py::test_a_run_killed_mid_http_step_…`, `test_g_sigterm.py::test_a_run_let_go_at_shutdown_goes_on_to_its_end` |
| [IT-3](#it-3) | A killed worker's Bash command keeps running beside the step's rerun | (b) | `test_b_kill_recovery.py::test_a_run_killed_mid_bash_step_…` |
| [IT-4](#it-4) | An attempt whose lease ran out still writes activity notes and usage rows | (c) | `test_c_lease_fencing.py::test_an_attempt_whose_lease_ran_out_notes_nothing`, `…_records_no_usage` |
| [IT-5](#it-5) | An attempt whose lease ran out still appends to the ADK session, and fails the attempt that took its run | (c) | `test_c_lease_fencing.py::test_an_attempt_whose_lease_ran_out_appends_nothing_to_the_session`, `…_cant_fail_the_attempt_that_took_its_run` |
| [IT-6](#it-6) | A parent waiting on its child holds the worker slot the child needs | (f) | `test_f_child_starvation.py::test_one_parent_on_a_worker_with_one_slot_…`, `test_two_parents_on_a_worker_with_two_slots_…` |
| [IT-7](#it-7) | A worker draining for shutdown keeps taking new runs | (g) | `test_g_sigterm.py::test_a_worker_that_is_stopping_takes_no_new_runs` |
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
let go, taken again at once, `succeeded` with result `None`.
Fix session: fail-closed resume.

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
  (see IT-4).
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
  go and taken again by the other worker at once (0.0 s), through SAQ's
  `job.retry("cancelled")`; `_let_go` itself queues nothing, so a job that
  takes longer than SAQ's one-second cancellation deadline to stop leaves
  its run to the maintenance (not seen here).
- **(h) A Redis outage of 120 s** (stopped, then started with its
  append-only file) while runs went on, the workers running their own
  maintenance every 5 s: a run in flight finished in MySQL during the
  outage (SAQ couldn't mark its job done: logged, harmless); a decision
  made, and a run started, during the outage (their jobs couldn't be
  queued) were found stalled by the first maintenance after Redis was back
  and ran; a delay that ended during the outage woke by its scheduled job.
  All four succeeded within 1.0 s of Redis being back, each effect once;
  both workers lived through it (but see IT-8).
