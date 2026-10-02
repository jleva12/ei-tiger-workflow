import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// Running workflows without a browser: how a run reads in the runs menu and
// lists, how often lists of runs refresh, and how the run dialog's fields
// (shared with an ADK workflow's human input) make a value and back.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const runs = await load("/src/lib/runs.ts")
const { buildValue, flattenValue, readJsonText, schemaProperties } = await load(
  "/src/components/runs/schema-value.ts"
)
await server.close()

const HOUR = 3_600_000

/** A background task as the list has it, with `fields` over a finished run. */
function task(fields = {}) {
  return {
    id: "task-1",
    job_name: "workflows.run",
    task_type: "workflows",
    kind: "run",
    description: "Order review",
    status: "COMPLETED",
    outcome: "ok",
    attempts: 1,
    created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    updated_at: new Date(Date.now() - HOUR).toISOString(),
    started_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    ended_at: new Date(Date.now() - HOUR).toISOString(),
    duration_ms: HOUR,
    failure: null,
    waiting_until: null,
    waiting_reason: null,
    awaiting_approval: false,
    ...fields,
  }
}

test("a run is open while queued, running, waiting for a person or for a time", () => {
  for (const status of ["PENDING", "RUNNING", "STOPPING", "PAUSING", "AWAITING_VALIDATION"])
    assert.equal(runs.isOpenRun(task({ status })), true, status)
  const later = new Date(Date.now() + HOUR).toISOString()
  assert.equal(runs.isOpenRun(task({ status: "STOPPED", waiting_until: later })), true)
  for (const status of ["COMPLETED", "FAILED", "ABANDONED", "STOPPED", "PAUSED"])
    assert.equal(runs.isOpenRun(task({ status })), false, status)
})

test("a run's line says what it waits for, why it failed, or when it started", () => {
  assert.equal(runs.runNote(task()), undefined)
  assert.match(runs.runLine(task()), /^Started /)
  assert.equal(runs.runLine(task({ status: "AWAITING_VALIDATION" })), "Waiting for a decision")
  const later = new Date(Date.now() + 3 * HOUR).toISOString()
  assert.match(runs.runLine(task({ status: "STOPPED", waiting_until: later })), /^Carries on /)
  const failure = { type: "E", message: "The HTTP step got a 500", category: "permanent", occurred_at: "" }
  assert.equal(runs.runLine(task({ status: "FAILED", failure })), "The HTTP step got a 500")
  // A run that recovered says nothing of its failure.
  assert.match(runs.runLine(task({ failure })), /^Started /)
})

test("a list of runs refreshes quickly while one is open, slowly otherwise, not after it failed", () => {
  const page = (...items) => ({ status: "success", data: { items, total: items.length } })
  assert.equal(runs.runsPollMs(page(task(), task({ status: "RUNNING" }))), runs.OPEN_POLL_MS)
  assert.equal(runs.runsPollMs(page(task({ status: "AWAITING_VALIDATION" }))), runs.OPEN_POLL_MS)
  assert.equal(runs.runsPollMs(page(task(), task({ status: "FAILED" }))), runs.IDLE_POLL_MS)
  assert.equal(runs.runsPollMs({ status: "pending" }), runs.IDLE_POLL_MS)
  assert.equal(runs.runsPollMs({ ...page(task({ status: "RUNNING" })), status: "error" }), false)
})

const SCHEMA = {
  type: "object",
  properties: {
    name: { type: "string" },
    count: { type: "integer" },
    ratio: { type: "number" },
    urgent: { type: "boolean" },
    tags: { type: "array", items: { type: "string" } },
    extra: { type: "object" },
    address: {
      type: "object",
      properties: { city: { type: "string" }, zip: { type: "string" } },
      required: ["city"],
    },
  },
  required: ["name", "count"],
}

test("the run form's fields make the input its start declares", () => {
  const properties = schemaProperties(SCHEMA)
  assert.deepEqual(
    properties.map((p) => p.name),
    ["name", "count", "ratio", "urgent", "tags", "extra", "address"]
  )
  const built = buildValue(properties, {
    name: "Ada",
    count: " 3 ",
    ratio: "0.5",
    urgent: true,
    tags: '["a", "b"]',
    extra: '{"x": 1}',
    "address.city": "Paris",
  })
  assert.deepEqual(built.errors, {})
  assert.deepEqual(built.value, {
    name: "Ada",
    count: 3,
    ratio: 0.5,
    urgent: true,
    tags: ["a", "b"],
    extra: { x: 1 },
    address: { city: "Paris" },
  })
})

test("the run form says what's wrong with each field, by path", () => {
  const properties = schemaProperties(SCHEMA)
  const built = buildValue(properties, {
    count: "2.5",
    ratio: "lots",
    tags: "a, b",
    extra: "{",
    "address.zip": "75001",
  })
  assert.deepEqual(built.errors, {
    name: "Required",
    count: "A whole number",
    ratio: "A number",
    tags: "A JSON list, e.g. [1, 2]",
    extra: "A JSON object",
    "address.city": "Required",
  })
  // An optional group left empty isn't sent; unticked optional booleans neither.
  assert.deepEqual(buildValue(properties, { name: "Ada", count: "1" }).value, {
    name: "Ada",
    count: 1,
  })
})

test("switching to JSON and back keeps what's filled in", () => {
  const properties = schemaProperties(SCHEMA)
  const value = {
    name: "Ada",
    count: 3,
    urgent: true,
    tags: ["a"],
    address: { city: "Paris" },
  }
  const raw = flattenValue(properties, value)
  assert.deepEqual(raw, {
    name: "Ada",
    count: "3",
    urgent: true,
    tags: '[\n  "a"\n]',
    "address.city": "Paris",
  })
  assert.deepEqual(buildValue(properties, raw).value, value)
})

test("JSON reads as its value; blank as the empty value; plain text only when lenient", () => {
  assert.deepEqual(readJsonText(' {"a": 1} '), { ok: true, value: { a: 1 } })
  assert.deepEqual(readJsonText("  "), { ok: true, value: null })
  assert.deepEqual(readJsonText("", { empty: {} }), { ok: true, value: {} })
  const refused = readJsonText("yes please")
  assert.equal(refused.ok, false)
  assert.match(refused.error, /^Not JSON: /)
  assert.deepEqual(readJsonText(" yes please ", { lenient: true }), { ok: true, value: "yes please" })
  assert.deepEqual(readJsonText("42", { lenient: true }), { ok: true, value: 42 })
})

test("a schema without properties declares no fields", () => {
  assert.deepEqual(schemaProperties({}), [])
  assert.deepEqual(schemaProperties({ type: "string" }), [])
})
