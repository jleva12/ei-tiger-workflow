import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const {
  followEvery,
  isGoing,
  phaseOf,
  runEndOf,
  runStartOf,
  timelineOf,
  workflowAnswerOf,
  workflowRunFollower,
} = await server.ssrLoadModule("/src/features/agents/lib/workflow-runs.ts")
const { workflowNamesOf } = await server.ssrLoadModule(
  "/src/features/agents/lib/document.ts"
)
await server.close()

const step = (id, status, extra = {}) => ({
  id,
  name: id[0].toUpperCase() + id.slice(1),
  kind: id === "start" ? "start" : "transform",
  status,
  started_at: null,
  finished_at: null,
  handed_to: false,
  ...extra,
})

const run = (status, steps, extra = {}) => ({
  run_id: "r1",
  workflow: "Ship",
  status,
  created_at: "2026-10-15T10:00:00+00:00",
  started_at: null,
  finished_at: null,
  waiting_reason: null,
  error: null,
  steps,
  ...extra,
})

test("a call's answer is read from ADK's JSON text or value", () => {
  const answered = {
    run_id: "r1",
    status: "succeeded",
    result: { outcome: "succeeded", result: "shipped" },
  }
  // The run's own result is its own: not mistaken for ADK's { result } wrap.
  assert.deepEqual(workflowAnswerOf(JSON.stringify(answered)), {
    runId: "r1",
    status: "succeeded",
  })
  assert.deepEqual(
    workflowAnswerOf({
      run_id: "r1",
      status: "failed",
      error: { message: "No route" },
    }),
    { runId: "r1", status: "failed", error: "No route" }
  )
  // A call that couldn't start a run answers with why.
  assert.deepEqual(workflowAnswerOf({ error: "The workflow has no start." }), {
    error: "The workflow has no start.",
  })
  // As Forge's tools answer: in an envelope.
  assert.deepEqual(
    workflowAnswerOf(JSON.stringify({ status: "success", payload: answered })),
    { runId: "r1", status: "succeeded" }
  )
  assert.deepEqual(
    workflowAnswerOf({
      status: "failed",
      reason:
        "ValueError: The input doesn't fit: 'order_id' is a required property",
      suggested_fixes: ["Check the arguments."],
    }),
    {
      error:
        "ValueError: The input doesn't fit: 'order_id' is a required property",
    }
  )
  assert.deepEqual(workflowAnswerOf("Timed out"), { error: "Timed out" })
  assert.equal(workflowAnswerOf(undefined), undefined)
  assert.equal(workflowAnswerOf(""), undefined)
})

test("the timeline is the steps the run reached, in the order it did", () => {
  // Queued: nothing recorded yet, so its start is at work.
  const queued = timelineOf(run("queued", [step("start", "not_reached")]))
  assert.deepEqual(
    queued.map((s) => [s.id, s.state]),
    [["start", "running"]]
  )

  const going = run(
    "running",
    [
      step("start", "done", {
        started_at: "2026-10-15T10:00:01+00:00",
        finished_at: "2026-10-15T10:00:01.500+00:00",
      }),
      // Listed first in the document, reached after "shape".
      step("review", "not_reached", { handed_to: true }),
      step("shape", "done", {
        started_at: "2026-10-15T10:00:02+00:00",
        finished_at: "2026-10-15T10:00:04+00:00",
      }),
    ],
    { started_at: "2026-10-15T10:00:01+00:00" }
  )
  const timeline = timelineOf(going)
  assert.deepEqual(
    timeline.map((s) => [s.id, s.state]),
    [
      ["start", "done"],
      ["shape", "done"],
      // Handed to, and nothing of its own yet: at work.
      ["review", "running"],
    ]
  )
  assert.equal(timeline[0].tookMs, 500)
  assert.equal(timeline[1].tookMs, 2000)
  // At work with nothing recorded: started as the step before finished.
  assert.equal(timeline[2].startedAt, Date.parse("2026-10-15T10:00:04Z"))
  assert.equal(timeline[2].tookMs, undefined)
  assert.equal(runStartOf(going), Date.parse("2026-10-15T10:00:01Z"))
  assert.equal(runEndOf(going), undefined)

  // Ended: the step it was at, failed with it.
  const failed = timelineOf({
    ...going,
    status: "failed",
    steps: [
      going.steps[0],
      step("shape", "running", { started_at: "2026-10-15T10:00:02+00:00" }),
    ],
  })
  assert.deepEqual(
    failed.map((s) => [s.id, s.state]),
    [
      ["start", "done"],
      ["shape", "failed"],
    ]
  )
})

test("where the run is, from what was read and what the call answered", () => {
  assert.equal(phaseOf(undefined, undefined), "starting")
  assert.equal(phaseOf(run("queued", []), undefined), "starting")
  assert.equal(phaseOf(run("running", []), undefined), "running")
  assert.equal(phaseOf(run("paused", []), undefined), "waiting")
  assert.equal(phaseOf(run("waiting", []), undefined), "waiting")
  assert.equal(phaseOf(run("abandoned", []), undefined), "stopped")
  // What was read wins over an answer from before it ended.
  assert.equal(
    phaseOf(run("succeeded", []), { status: "running" }),
    "succeeded"
  )
  // A run the chat couldn't read: the call's answer says.
  assert.equal(phaseOf(undefined, { runId: "r1", status: "failed" }), "failed")
  assert.equal(phaseOf(undefined, { error: "No such workflow" }), "failed")
  assert.equal(isGoing("waiting"), true)
  assert.equal(isGoing("succeeded"), false)
})

test("it's asked about while the call waits, then while the run goes", () => {
  assert.equal(followEvery(undefined, false), 1500)
  assert.equal(followEvery(null, false), 1500)
  assert.equal(followEvery(run("running", []), true), 2500)
  assert.equal(followEvery(run("paused", []), true), 5000)
  assert.equal(followEvery(run("succeeded", []), true), false)
  // A call that answered without a run to read: nothing to ask about.
  assert.equal(followEvery(null, true), false)
})

test("the follower reads the run a call started from the chat's runtime", async () => {
  const asked = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    asked.push({ url, headers: init.headers })
    if (url.endsWith("/call_2")) return new Response("{}", { status: 404 })
    if (url.endsWith("/call_3")) return new Response("{}", { status: 503 })
    return Response.json(run("running", []))
  }
  try {
    const follower = workflowRunFollower({
      url: "https://forge.example/runtime/",
      appName: "ca_x@draft",
      userId: "user 1",
      headers: () => ({ Authorization: "Bearer t" }),
      names: { ship_it: "Ship it" },
      runHref: (id) => `/runs/${id}`,
    })
    const found = await follower.progress("s-1", "call_1")
    assert.equal(found.status, "running")
    assert.equal(
      asked[0].url,
      "https://forge.example/runtime/apps/ca_x%40draft/users/user%201/sessions/s-1/workflow-runs/call_1"
    )
    assert.deepEqual(asked[0].headers, { Authorization: "Bearer t" })
    // None started (yet): null; the runtime failing: an error.
    assert.equal(await follower.progress("s-1", "call_2"), null)
    await assert.rejects(follower.progress("s-1", "call_3"), /503/)
    assert.equal(follower.runHref("r1"), "/runs/r1")
    assert.deepEqual(follower.names, { ship_it: "Ship it" })
  } finally {
    globalThis.fetch = original
  }
})

test("a document's workflow tools are named as the model calls them", () => {
  const doc = {
    nodes: [
      {
        id: "agent",
        kind: "agent",
        name: "Concierge",
        config: {},
        outputs: [],
      },
      {
        id: "ship",
        kind: "adk_workflow",
        name: "Ship order",
        config: {},
        outputs: [],
      },
      {
        id: "kb",
        kind: "knowledge_base",
        name: "Docs",
        config: {},
        outputs: [],
      },
    ],
  }
  assert.deepEqual(workflowNamesOf(doc), { ship_order: "Ship order" })
})
