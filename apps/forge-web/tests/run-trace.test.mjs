import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// A run's trace without a browser: what each event is tagged and says, how
// the filters keep events, and the state rebuilt at any event from the
// state before the run and each event's changes.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const trace = await server.ssrLoadModule("/src/features/runs/lib/trace.ts")
await server.close()

/** An event of a run's trace, with `fields` over one that did nothing. */
function event(seq, fields = {}) {
  return {
    seq,
    id: `e-${seq}`,
    at: `2026-10-11T12:00:0${seq}.000Z`,
    author: "steps",
    role: null,
    branch: null,
    path: "",
    step: null,
    steps: [],
    own: false,
    text: null,
    thought: null,
    calls: [],
    responses: [],
    files: [],
    output: null,
    has_output: false,
    output_for: [],
    state_delta: {},
    artifact_delta: {},
    route: null,
    transfer_to_agent: null,
    escalate: false,
    error: null,
    usage: null,
    model: null,
    metadata: null,
    partial: false,
    interrupted: false,
    long_running: [],
    ...fields,
  }
}

const RUN = {
  state_before: { kept: 1 },
  state_after: { kept: 1, input: { key: "k" }, total: 3 },
  events: [
    event(1, { author: "user", role: "user", text: '{"key": "k"}' }),
    event(2, {
      step: "start",
      steps: ["start"],
      own: true,
      has_output: true,
      output: { key: "k" },
      state_delta: { input: { key: "k" } },
    }),
    event(3, {
      step: "count",
      steps: ["count"],
      own: true,
      calls: [{ id: "c1", name: "lookup", args: { q: "k" } }],
      usage: { prompt_tokens: 10, output_tokens: 4, total_tokens: 14 },
    }),
    event(4, {
      step: "count",
      steps: ["count"],
      responses: [{ id: "c1", name: "lookup", response: { hits: 2 } }],
      state_delta: { total: 2 },
    }),
    event(5, {
      step: "count",
      steps: ["count"],
      own: true,
      state_delta: { total: 3, "forge:endings:x": [] },
      error: { code: "RunFailed", message: "boom" },
    }),
  ],
}

test("each event is tagged with what it did, and says it in a line", () => {
  const [message, start, call, answer, failed] = RUN.events
  assert.deepEqual(trace.eventKinds(message), ["message"])
  assert.deepEqual(trace.eventKinds(start), ["output", "state"])
  assert.deepEqual(trace.eventKinds(call), ["call"])
  assert.deepEqual(trace.eventKinds(answer), ["response", "state"])
  assert.deepEqual(trace.eventKinds(failed), ["error", "state"])
  assert.equal(trace.eventSummary(call), "Calls lookup")
  assert.equal(trace.eventSummary(start), 'Hands on {"key":"k"}')
  assert.equal(trace.eventSummary(failed), "boom")
  // A question for a person is a pause, not a tool call.
  const asks = event(6, { calls: [{ id: "q", name: "adk_request_input", args: {} }] })
  assert.deepEqual(trace.eventKinds(asks), ["pause"])
})

test("the filters keep a step's events, a kind's, or those with the text", () => {
  const seqs = (filter) =>
    RUN.events.filter((each) => trace.matchesTrace(each, filter)).map((each) => each.seq)
  assert.deepEqual(seqs({}), [1, 2, 3, 4, 5])
  assert.deepEqual(seqs({ step: "count" }), [3, 4, 5])
  assert.deepEqual(seqs({ kinds: ["state"] }), [2, 4, 5])
  assert.deepEqual(seqs({ kinds: ["call", "error"] }), [3, 5])
  assert.deepEqual(seqs({ query: "HITS" }), [4])
  assert.deepEqual(seqs({ step: "count", query: "lookup" }), [3, 4])
})

test("the state at an event is the state before with each change up to it", () => {
  assert.deepEqual(trace.stateAt(RUN, 0), { kept: 1 })
  assert.deepEqual(trace.stateAt(RUN, 2), { kept: 1, input: { key: "k" } })
  assert.deepEqual(trace.stateAt(RUN, 4), { kept: 1, input: { key: "k" }, total: 2 })
  assert.deepEqual(trace.stateAt(RUN, 5).total, 3)
})

test("each change says what the key held before, and each key how often it changed", () => {
  const changes = trace.stateChanges(RUN)
  assert.deepEqual(
    changes.map((change) => [change.key, change.seq, change.existed, change.before, change.after]),
    [
      ["input", 2, false, undefined, { key: "k" }],
      ["total", 4, false, undefined, 2],
      ["total", 5, true, 2, 3],
      ["forge:endings:x", 5, false, undefined, []],
    ]
  )
  assert.deepEqual(trace.stateKeys(RUN), [
    { key: "kept", changes: 0, added: false },
    { key: "input", changes: 1, added: true },
    { key: "total", changes: 2, added: true },
  ])
  assert.ok(trace.isPrivateKey("forge:endings:x") && !trace.isPrivateKey("total"))
})

test("the totals add up the run's events", () => {
  assert.deepEqual(trace.traceTotals(RUN.events), {
    events: 5,
    stateChanges: 4,
    calls: 1,
    errors: 1,
    modelTurns: 1,
    promptTokens: 10,
    outputTokens: 4,
    totalTokens: 14,
  })
  assert.equal(trace.gapMs(RUN.events[0].at, RUN.events[2].at), 2000)
  assert.equal(trace.gapMs(undefined, RUN.events[2].at), undefined)
})
