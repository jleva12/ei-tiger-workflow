# ADK run integration suite

The run machinery (run store, `run_adk`, the lease keeper and watchdog, the
maintenance, SAQ) on a real MySQL 8 and Redis, with real worker processes,
several at once. The unit tests run it on SQLite with one connection and one
process, where `SELECT ... FOR UPDATE` does nothing and nobody else is there;
these are the tests that can show its concurrency and recovery.

    make async-worker-test-integration                      # stack, migrations, the whole suite (~7 min)
    make async-worker-test-integration IT_ARGS="-k claim"   # a scenario or two
    make async-worker-test-integration-down                 # remove the stack

The stack is a Compose project of its own (`compose.yaml`, `forge-worker-it`,
MySQL 8.4 on 13346 and Redis on 16409, clear of both repositories' stacks), so
stale `make up-all-local` workers never see its jobs. Every test clears its
tables and Redis first. Outside the make target the tests skip unless
`FORGE_IT_MYSQL_URL` and `FORGE_IT_REDIS_URL` are set (conftest.py).

What the scenarios found (IT-1 to IT-8) is fixed and listed in the repo's
[CURRENT_STATE.md](../../../../CURRENT_STATE.md); every scenario passes. A
new finding's tests are marked `xfail(strict=True)`, their reason naming it:
once fixed they XPASS, which fails the suite until the marker is removed.

## Reports

Each scenario writes `.reports/<test>.txt` (what it observed, then a
timeline merging the test's actions, the mock server's requests, the runs'
activity and the workers' warnings, by time) and `<test>.workers.log` (the
workers' JSON logs). A failing test shows its timeline too.

## The harness

- `harness/worker_main.py`: a worker process, the real `saq_worker.serve`,
  with only jobs.py's and saq_worker.py's timing constants shortened from
  the environment (a 6 s lease by default; `FORGE_IT_*`), its maintenance off
  unless asked for (the tests then run it themselves), and optionally a run
  store clock ahead of the host's.
- `harness/mock_server.py`: the effects the tests count. `/effect/<name>`
  records each request as it arrives, and `hold(name)` keeps the next ones
  waiting until `release(name)`, so a test knows a step is in flight.
  `/v1/chat/completions` is the workers' model (an OpenAI-compatible API): it
  calls the first tool offered, then answers in words.
- `harness/workflows.py`: workflow documents (HTTP, Bash, approval, LLM and
  workflow tool steps) and runs of them, as the admin API starts them.
- `harness/scenario.py`: `Scenario`, one test's world. `spawn` and
  `spawn_all` start workers; `owner_of` finds the worker holding a run;
  `start`, `enqueue`, and the `store` play the admin API; `maintain(skew=)`
  runs the maintenance as a worker would; `until`, `finished`, `events`,
  `usage` and `session_events` read what happened; `observe` records what
  the report's summary says. `played` runs one scenario for a module's
  tests to share, each holding one property to account.

A scenario is an async test taking the `scenario` fixture:

```python
async def test_something(scenario: Scenario) -> None:
    await scenario.warm_sessions()  # ADK's state rows first (CURRENT_STATE.md IT-1)
    await scenario.spawn_all("w1", "w2", maintenance=2)
    scenario.mock.hold("charge")
    run_id = await scenario.start(w.line(w.http("charge", scenario.mock.effect_url("charge")), w.end("done")))
    await scenario.mock.arrived("charge")
    scenario.owner_of(await scenario.run(run_id)).kill()
    scenario.mock.release("charge")
    run = await scenario.finished(run_id, seconds=40)
    assert scenario.observe("charges", scenario.mock.count("charge")) == 2
```
