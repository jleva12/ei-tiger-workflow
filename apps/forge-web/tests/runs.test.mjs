import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// Running ADK workflows without a browser: how a run reads in the runs menu
// and lists, how often lists of runs refresh, and how the run dialog's fields
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

/** An ADK workflow run as the list has it, with `fields` over a succeeded run. */
function run(fields = {}) {
  return {
    id: "run-1",
    organization_id: "org-1",
    agent_id: "ag-1",
    agent_name: "Order review",
    revision: 2,
    session_id: "session-1",
    status: "succeeded",
    attempt: 1,
    requested_by: { id: "user-1", name: "Ada" },
    resubmit_of: null,
    created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    updated_at: new Date(Date.now() - HOUR).toISOString(),
    started_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    finished_at: new Date(Date.now() - HOUR).toISOString(),
    duration_ms: HOUR,
    waiting_until: null,
    waiting_reason: null,
    pause: null,
    error: null,
    ...fields,
  }
}

const pause = (kind) => ({ id: "req-1", kind, reason: "Ship it?", details: {}, requested_at: null, deadline: null })

test("a run is open while queued, running, or waiting for a person or a time", () => {
  for (const status of ["queued", "running", "paused", "waiting"])
    assert.equal(runs.isOpenRun(run({ status })), true, status)
  for (const status of ["succeeded", "failed", "abandoned"])
    assert.equal(runs.isOpenRun(run({ status })), false, status)
  assert.equal(runs.isLiveRun(run({ status: "queued" })), true)
  assert.equal(runs.isLiveRun(run({ status: "paused" })), false)
  assert.equal(runs.isLiveRun(undefined), false)
})

test("a run's badge says what it waits for", () => {
  assert.deepEqual(runs.runStatusDisplay(run()), { status: "completed", label: "Succeeded" })
  assert.deepEqual(runs.runStatusDisplay(run({ status: "paused", pause: pause("approval") })), {
    status: "review",
    label: "Awaiting approval",
  })
  assert.equal(runs.runStatusDisplay(run({ status: "paused", pause: pause("human_input") })).label, "Awaiting an answer")
  assert.equal(runs.runStatusDisplay(run({ status: "waiting" })).status, "pending")
  assert.equal(runs.runStatusDisplay(run({ status: "abandoned" })).status, "cancelled")
})

test("a run's line says what it waits for, why it failed, or when it started", () => {
  assert.equal(runs.runNote(run()), undefined)
  assert.match(runs.runLine(run()), /^Started /)
  assert.equal(runs.runLine(run({ status: "paused", pause: pause("approval") })), "Waiting for a decision")
  assert.equal(runs.runLine(run({ status: "paused", pause: pause("human_input") })), "Waiting for an answer")
  const later = new Date(Date.now() + 3 * HOUR).toISOString()
  assert.match(runs.runLine(run({ status: "waiting", waiting_until: later })), /^Carries on /)
  const error = { message: "The HTTP step got a 500", category: "failed", step: null, occurred_at: "" }
  assert.equal(runs.runLine(run({ status: "failed", error })), "The HTTP step got a 500")
  assert.equal(runs.runLine(run({ status: "queued", error })), "Retrying: The HTTP step got a 500")
  // A run that recovered and finished says nothing of its hiccup.
  assert.match(runs.runLine(run({ error })), /^Started /)
})

test("a list of runs refreshes quickly while one is open, slowly otherwise, not after it failed", () => {
  const page = (...items) => ({ status: "success", data: { items, total: items.length } })
  assert.equal(runs.runsPollMs(page(run(), run({ status: "running" }))), runs.OPEN_POLL_MS)
  assert.equal(runs.runsPollMs(page(run({ status: "paused" }))), runs.OPEN_POLL_MS)
  assert.equal(runs.runsPollMs(page(run(), run({ status: "failed" }))), runs.IDLE_POLL_MS)
  assert.equal(runs.runsPollMs({ status: "pending" }), runs.IDLE_POLL_MS)
  assert.equal(runs.runsPollMs({ ...page(run({ status: "running" })), status: "error" }), false)
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
