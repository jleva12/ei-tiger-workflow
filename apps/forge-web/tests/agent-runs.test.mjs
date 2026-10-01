import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// Running ADK workflows without a browser: the run page's tabs, how its
// steps read (kinds from the ADK workflow the run started with, names,
// statuses, times), what a paused run waits for and who may answer, and
// how often its steps refresh.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const runs = await load("/src/lib/agents/runs.ts")
const { taskTypeOf } = await load("/src/lib/background-tasks.ts")
const { exampleAgent } = await load("/src/lib/agents/example.ts")
const { OPEN_POLL_MS, IDLE_POLL_MS } = await load("/src/lib/workflows/runs.ts")
await server.close()

const doc = exampleAgent("org-1")
const step = (fields = {}) => ({
  id: "x",
  name: "",
  kind: "transform",
  status: "done",
  output: null,
  error: null,
  started_at: null,
  finished_at: null,
  ...fields,
})

test("ADK workflow runs are their own task type, named as such", () => {
  assert.equal(runs.ADK_WORKFLOW_TASK_TYPE, "adk_workflows")
  assert.equal(taskTypeOf("adk_workflows").label, "ADK workflow")
  assert.equal(taskTypeOf("workflows").label, "Workflow")
})

test("the page's and a run's tabs", () => {
  assert.equal(runs.isAgentsTab("runs"), true)
  assert.equal(runs.isAgentsTab("overview"), true)
  assert.equal(runs.isAgentsTab("tasks"), false)
  for (const tab of ["overview", "steps", "attempts", "activity"])
    assert.equal(runs.isAdkRunTab(tab), true, tab)
  assert.equal(runs.isAdkRunTab("runs"), false)
  assert.equal(runs.isAdkRunTab(undefined), false)
})

test("a run's document is the snapshot its input carries, when it's an ADK workflow", () => {
  assert.equal(runs.runDocumentOf({ document: doc, input: {} }), doc)
  assert.equal(runs.runDocumentOf({ document: { format: "forge.workflow/v1", nodes: [] } }), undefined)
  assert.equal(runs.runDocumentOf({ input: {} }), undefined)
  assert.equal(runs.runDocumentOf(undefined), undefined)
})

test("a step's kind and name come from its node, else from what the run says", () => {
  const node = doc.nodes.find((n) => n.kind !== "start")
  assert.ok(node)
  // The node's kind wins over the run's word for it.
  assert.equal(runs.stepKindOf(step({ id: node.id, kind: "something" }), doc), node.kind)
  assert.equal(runs.stepKindOf(step({ id: "gone", kind: "llm" }), doc), "llm")
  assert.equal(runs.stepKindOf(step({ id: "gone", kind: "__finish__" }), doc), undefined)
  assert.equal(runs.stepKindOf(step({ kind: "delay" }), undefined), "delay")

  assert.equal(runs.stepNameOf(step({ id: node.id, name: "As it ran" }), doc), "As it ran")
  assert.equal(runs.stepNameOf(step({ id: node.id }), doc), node.name)
  assert.equal(runs.stepNameOf(step({ id: "gone", kind: "human_input" }), undefined), "Human input")
  assert.equal(runs.stepNameOf(step({ id: "hidden", kind: "odd" }), undefined), "hidden")
})

test("a step's status and time, in words", () => {
  assert.deepEqual(runs.stepStatusOf("done"), { label: "Done", tone: "success" })
  assert.deepEqual(runs.stepStatusOf("failed"), { label: "Failed", tone: "danger" })
  assert.deepEqual(runs.stepStatusOf("waiting"), { label: "Waiting", tone: "notice" })
  assert.deepEqual(runs.stepStatusOf("running"), { label: "Running", tone: "warning" })
  assert.deepEqual(runs.stepStatusOf("not_reached"), { label: "Not reached", tone: "outline" })
  assert.deepEqual(runs.stepStatusOf("skipped_over"), { label: "Skipped over", tone: "neutral" })

  const started_at = "2026-09-28T12:00:00Z"
  const finished_at = "2026-09-28T12:00:03.500Z"
  assert.equal(runs.stepDurationMs(step({ started_at, finished_at })), 3500)
  assert.equal(runs.stepDurationMs(step({ started_at })), undefined)
  assert.equal(runs.stepDurationMs(step({ started_at: finished_at, finished_at: started_at })), undefined)
  assert.match(runs.stepTiming(step({ started_at, finished_at })), / · took 4s$/)
  assert.match(runs.stepTiming(step({ status: "waiting", started_at })), /^Waiting since /)
  assert.match(runs.stepTiming(step({ status: "running", started_at })), /^Running since /)
  assert.match(runs.stepTiming(step({ status: "failed", started_at })), /^Started /)
  assert.equal(runs.stepTiming(step({ status: "not_reached" })), undefined)
})

test("a step's error: why it failed the run, or the error it took its Error way with", () => {
  assert.equal(runs.stepErrorOf(null), undefined)
  assert.equal(runs.stepErrorOf(undefined), undefined)
  assert.deepEqual(runs.stepErrorOf("It broke"), { message: "It broke" })
  // A failed step: ADK's message and code.
  assert.deepEqual(runs.stepErrorOf({ message: "The model timed out", code: "DEADLINE_EXCEEDED" }), {
    message: "The model timed out",
    code: "DEADLINE_EXCEEDED",
  })
  assert.deepEqual(runs.stepErrorOf({ message: "Stopped", code: null }), { message: "Stopped" })
  assert.deepEqual(runs.stepErrorOf({ message: null, code: "RunFailed" }), {
    message: "Failed with RunFailed",
    code: "RunFailed",
  })
  // An HTTP request that took its Error way: its status, and its body to look into.
  assert.deepEqual(
    runs.stepErrorOf({ message: "Not found", status: 404, body: { error: "no such customer" } }),
    { message: "Not found", code: "HTTP 404", more: { error: "no such customer" } }
  )
  assert.deepEqual(runs.stepErrorOf({ message: "Odd", extra: 1, body: "x" }), {
    message: "Odd",
    more: { extra: 1, body: "x" },
  })
  assert.deepEqual(runs.stepErrorOf({}), { message: "It failed" })
})

test("a paused run waits for a person's answer or, otherwise, a decision", () => {
  const approval = (details) => ({ id: "req-1", reason: "Ship it?", details, requested_at: null, deadline: null })
  const schema = { type: "object", properties: { note: { type: "string" } } }
  const asked = approval({ kind: "human_input", response_schema: schema, message: "Why?" })
  assert.equal(runs.pauseKindOf(asked), "human_input")
  assert.deepEqual(runs.responseSchemaOf(asked), schema)
  assert.equal(runs.pauseKindOf(approval({ kind: "approval", approvers: "org:member" })), "approval")
  // A pause the console doesn't know is decided, as an approval is.
  assert.equal(runs.pauseKindOf(approval({})), "approval")
  assert.deepEqual(runs.responseSchemaOf(approval({ response_schema: ["no"] })), {})
  assert.deepEqual(runs.responseSchemaOf(approval({})), {})
})

test("deciding an ADK approval takes what its approvers need; anything else is the admins'", () => {
  assert.equal(runs.adkApprovePermission("org:member"), "agents:run")
  assert.equal(runs.adkApprovePermission("org:admin"), "agents:approve")
  assert.equal(runs.adkApprovePermission(undefined), "agents:approve")
})

test("a run's steps refresh quickly while it goes or was just acted on, slowly while it waits", () => {
  const later = new Date(Date.now() + 3_600_000).toISOString()
  assert.equal(runs.stepsPollMs({ status: "RUNNING", waiting_until: null }, false), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "PENDING", waiting_until: null }, false), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "AWAITING_VALIDATION", waiting_until: null }, true), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "AWAITING_VALIDATION", waiting_until: null }, false), IDLE_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "STOPPED", waiting_until: later }, false), IDLE_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "COMPLETED", waiting_until: null }, false), false)
  assert.equal(runs.stepsPollMs(undefined, false), false)
})
