import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

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
const runs = await load("/src/features/runs/lib/runs.ts")
const { exampleAgent } = await load("/src/features/adk-workflows/lib/example.ts")
const { OPEN_POLL_MS, IDLE_POLL_MS } = await load("/src/features/runs/lib/display.ts")
await installNodeCatalog(server)
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

test("a run's words: its reference, its failures, its activity", () => {
  assert.equal(runs.shortId("0123456789abcdef"), "01234567")
  assert.equal(runs.humanize("timed_out"), "Timed out")
  assert.equal(runs.failureCategory("transient").label, "Retrying automatically")
  // A category the console doesn't know reads as an error.
  assert.equal(runs.failureCategory("odd").label, "Error")
  assert.equal(runs.eventTitle({ kind: "decided" }), "Decided")
  assert.equal(runs.eventTitle({ kind: "worker_moved" }), "Worker moved")
  assert.equal(runs.isFailureEvent({ kind: "failed" }), true)
  assert.equal(runs.isFailureEvent({ kind: "succeeded" }), false)
  assert.equal(runs.failureTitle({ status: 503 }, "Couldn't"), "Workflow runs are unavailable")
  assert.equal(runs.failureTitle({ status: 500 }, "Couldn't"), "Couldn't")
})

test("the page's and a run's tabs", () => {
  assert.equal(runs.isAgentsTab("runs"), true)
  assert.equal(runs.isAgentsTab("overview"), true)
  assert.equal(runs.isAgentsTab("tasks"), false)
  for (const tab of ["overview", "steps", "activity"])
    assert.equal(runs.isAdkRunTab(tab), true, tab)
  assert.equal(runs.isAdkRunTab("attempts"), false)
  assert.equal(runs.isAdkRunTab("runs"), false)
  assert.equal(runs.isAdkRunTab(undefined), false)
})

test("a run's document is the ADK workflow it started with, when it's one", () => {
  assert.equal(runs.runDocumentOf(doc), doc)
  assert.equal(runs.runDocumentOf({ format: "forge.workflow/v1", nodes: [] }), undefined)
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
  // Its method and URL, which its message says, aren't more to look into.
  const url = "https://api.example.com/customers/40"
  assert.deepEqual(
    runs.stepErrorOf({
      message: `GET ${url} answered 404 Not Found: No such customer`,
      status: 404,
      body: { detail: "No such customer" },
      method: "GET",
      url,
    }),
    {
      message: `GET ${url} answered 404 Not Found: No such customer`,
      code: "HTTP 404",
      more: { detail: "No such customer" },
    }
  )
})

test("a step's call to send is titled by its kind, and a workflow call names its run", () => {
  assert.equal(runs.calledWithTitle("saved"), "Started its workflow with")
  assert.equal(runs.calledWithTitle("http"), "Request sent")
  assert.equal(runs.calledWithTitle("transform"), "Called with")
  assert.equal(runs.callRunId({ response: { run_id: "r-1", status: "failed" } }), "r-1")
  assert.equal(runs.callRunId({ response: { status: "failed", reason: "x" } }), undefined)
  assert.equal(runs.callRunId({ response: null }), undefined)
})

test("a paused run waits for a person's answer or, otherwise, a decision", () => {
  const pause = (kind, details) => ({ id: "req-1", kind, reason: "Ship it?", details, requested_at: null, deadline: null })
  const schema = { type: "object", properties: { note: { type: "string" } } }
  const asked = pause("human_input", { response_schema: schema, message: "Why?" })
  assert.equal(runs.pauseKindOf(asked), "human_input")
  assert.deepEqual(runs.responseSchemaOf(asked), schema)
  assert.equal(runs.pauseKindOf(pause("approval", { approvers: "org:member" })), "approval")
  // A pause the console doesn't know is decided, as an approval is.
  assert.equal(runs.pauseKindOf(pause("odd", {})), "approval")
  const approval = (details) => pause("approval", details)
  assert.deepEqual(runs.responseSchemaOf(approval({ response_schema: ["no"] })), {})
  assert.deepEqual(runs.responseSchemaOf(approval({})), {})
})

test("deciding an ADK approval takes what its approvers need; anything else is the admins'", () => {
  assert.equal(runs.adkApprovePermission("org:member"), "agents:run")
  assert.equal(runs.adkApprovePermission("org:admin"), "agents:approve")
  assert.equal(runs.adkApprovePermission(undefined), "agents:approve")
})

test("a run's steps refresh quickly while it goes or was just acted on, slowly while it waits", () => {
  assert.equal(runs.stepsPollMs({ status: "running" }, false), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "queued" }, false), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "paused" }, true), OPEN_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "paused" }, false), IDLE_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "waiting" }, false), IDLE_POLL_MS)
  assert.equal(runs.stepsPollMs({ status: "succeeded" }, false), false)
  assert.equal(runs.stepsPollMs(undefined, false), false)
})

test("a run's start in words: what started it, who asked, via what, for whom", () => {
  const asked = { requested_by: { id: "chat-agent:ca_desk", name: "Chat agent Desk" }, resubmit_of: null }
  const chat = runs.provenanceWords({
    ...asked,
    provenance: {
      trigger_type: "chat_agent",
      channel: "page",
      on_behalf_of: { id: "visitor-9", name: "", verified: false },
      via: { agent_id: "ca_desk", version: "3" },
      api_key_id: null,
      auth_method: "anonymous",
    },
  })
  assert.deepEqual(chat, {
    how: "By a chat agent",
    by: "Chat agent Desk",
    via: "ca_desk v3",
    onBehalfOf: "visitor-9",
    verified: false,
    channel: "a chat page",
    credential: "None (anonymous caller)",
  })
  assert.equal(
    runs.provenanceLine(chat),
    "By a chat agent · Chat agent Desk · via ca_desk v3 · on behalf of visitor-9 (unverified)"
  )
  // A person's own test run is theirs: nobody else to name.
  const tested = runs.provenanceWords({
    requested_by: { id: "u-1", name: "Mia" },
    resubmit_of: null,
    provenance: {
      trigger_type: "builder_test",
      channel: "console",
      on_behalf_of: { id: "u-1", name: "Mia", verified: true },
      via: null,
      api_key_id: null,
      auth_method: "jwt",
    },
  })
  assert.equal(runs.provenanceLine(tested), "Test run from the builder · Mia")
  assert.equal(tested.credential, "Sign-in")
  // A key's, by its ID; a run from before, by its trigger.
  const keyed = runs.provenanceWords({
    requested_by: { id: "apikey:k-1", name: "API key CI" },
    resubmit_of: null,
    provenance: {
      trigger_type: "api_key",
      channel: "api",
      on_behalf_of: { id: "apikey:k-1", name: "API key CI", verified: true },
      via: null,
      api_key_id: "k-1",
      auth_method: "api_key",
    },
  })
  assert.equal(keyed.credential, "API key k-1")
  assert.equal(runs.provenanceLine(keyed), "With an API key · API key CI")
  assert.equal(
    runs.provenanceLine(runs.provenanceWords({ ...asked, trigger: { type: "runtime", protocol: "a2a" } })),
    "Over A2A · Chat agent Desk"
  )
  assert.equal(runs.provenanceWords({ ...asked, resubmit_of: "r-0" }).how, "Resubmitted")
})

test("the runs' filters are read from the address, leaving out what couldn't be one", () => {
  assert.deepEqual(
    runs.readRunFilters({
      workflow: "ag_ship",
      trigger: "builder_test",
      person: "  u-1 ",
      via: "ca_desk",
      apiKey: "k-1",
      root: "r".repeat(32),
      startedFrom: "2026-10-01",
      startedTo: "2026-10-03",
      view: "agents",
    }),
    {
      workflow: "ag_ship",
      trigger: "builder_test",
      person: "u-1",
      via: "ca_desk",
      apiKey: "k-1",
      root: "r".repeat(32),
      startedFrom: "2026-10-01",
      startedTo: "2026-10-03",
    }
  )
  // A way runs aren't started, a day that isn't one, blanks and non-text are dropped.
  assert.deepEqual(
    runs.readRunFilters({
      trigger: "sideways",
      startedFrom: "Oct 1",
      startedTo: "2026-02-30",
      person: "   ",
      via: 7,
    }),
    {}
  )
  // Days the wrong way round are the same days.
  assert.deepEqual(runs.readRunFilters({ startedFrom: "2026-10-09", startedTo: "2026-10-02" }), {
    startedFrom: "2026-10-02",
    startedTo: "2026-10-09",
  })
  assert.equal(runs.hasRunFilters({}), false)
  assert.equal(runs.hasRunFilters({ root: "r-1" }), true)
})

test("the runs' filters become the runs API's query, days as the reader's", () => {
  const filters = {
    workflow: "ag_ship",
    trigger: "chat_agent",
    person: "visitor-9",
    via: "ca_desk",
    apiKey: "k-1",
    root: "r-1",
    startedFrom: "2026-10-01",
    startedTo: "2026-10-03",
  }
  assert.deepEqual(runs.runFilterParams(filters), {
    agent_id: "ag_ship",
    trigger_type: "chat_agent",
    on_behalf_of: "visitor-9",
    via_agent_id: "ca_desk",
    api_key_id: "k-1",
    root_run_id: "r-1",
    // From the first day's midnight to the day after the last's, here.
    created_from: new Date(2026, 9, 1).toISOString(),
    created_to: new Date(2026, 9, 4).toISOString(),
  })
  // Refused starts make no tree.
  assert.equal(runs.runFilterParams(filters, { refusals: true }).root_run_id, undefined)
  // A month's last day runs to the next month's first.
  assert.equal(
    runs.runFilterParams({ startedTo: "2026-10-31" }).created_to,
    new Date(2026, 10, 1).toISOString()
  )
  assert.deepEqual(runs.runFilterParams({}), {})
})
