import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// A test run drawn over its workflow's canvas: each step's state (the one at
// work spinning, even before ADK records anything of it) and the lines the
// run went along.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const { runProgress, reachedSteps } = await server.ssrLoadModule(
  "/src/features/runs/lib/progress.ts"
)
await server.close()

const step = (id, kind, status, fields = {}) => ({
  id,
  name: id,
  kind,
  status,
  output: null,
  error: null,
  started_at: null,
  finished_at: null,
  handed_to: false,
  ...fields,
})

const states = (progress) => Object.fromEntries(progress.steps)

test("the step the run handed on to spins while it goes, before it records anything", () => {
  const data = {
    steps: [
      step("start", "start", "done", { started_at: "2026-10-11T10:00:00Z" }),
      step("write", "llm", "not_reached", { handed_to: true }),
      step("done", "end", "not_reached"),
    ],
    taken_edges: ["start:next->write"],
  }
  const going = runProgress({ status: "running" }, data)
  assert.deepEqual(states(going), { start: "done", write: "running", done: "idle" })
  assert.deepEqual([...going.taken], ["start:next->write"])
  assert.equal(going.live, true)
  // Once it's stopped, nothing spins.
  assert.equal(runProgress({ status: "succeeded" }, data).steps.get("write"), "idle")
})

test("a step the run stopped at fails with it, and waiting steps wait", () => {
  const data = {
    steps: [
      step("start", "start", "done"),
      step("call", "http", "running"),
      step("review", "approval", "waiting"),
    ],
  }
  assert.deepEqual(states(runProgress({ status: "failed" }, data)), {
    start: "done",
    call: "failed",
    review: "waiting",
  })
  assert.deepEqual(states(runProgress({ status: "paused" }, data)), {
    start: "done",
    call: "idle",
    review: "waiting",
  })
})

test("before anything is recorded, a queued run is at its start", () => {
  const data = { steps: [step("start", "start", "not_reached"), step("x", "llm", "not_reached")] }
  assert.deepEqual(states(runProgress({ status: "queued" }, data)), { start: "running", x: "idle" })
  assert.deepEqual(states(runProgress(undefined, undefined)), {})
})

test("the steps reached are listed in the order they started, the one at work last", () => {
  const data = {
    steps: [
      step("b", "llm", "done", { started_at: "2026-10-11T10:00:02Z" }),
      step("a", "start", "done", { started_at: "2026-10-11T10:00:00Z" }),
      step("c", "llm", "not_reached", { handed_to: true }),
      step("d", "end", "not_reached"),
    ],
  }
  const progress = runProgress({ status: "running" }, data)
  assert.deepEqual(reachedSteps(data, progress).map((s) => s.id), ["a", "b", "c"])
})
