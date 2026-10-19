import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// A chat agent's test chat drawn over its canvas: the runtime's events name
// the agent answering and what it calls, as ADK names them; they lead back
// to the canvas's nodes and lines.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
})
const { testGraphOf, createTestProgress } = await server.ssrLoadModule(
  "/src/features/agents/lib/test-progress.ts"
)
await server.close()

const node = (id, kind, name, config = {}) => ({
  id,
  kind,
  name,
  config,
  outputs: [],
})
const edge = (source, output, target) => ({
  id: `${source}-${output}-${target}`,
  source,
  source_output: output,
  target,
})

const doc = {
  nodes: [
    node("entry", "agent", "Service desk"),
    node("http", "http_tool", "Look up order"),
    node("kb", "knowledge_base", "Policies"),
    node("mcp", "mcp", "Tickets", { server: "srv", tools: "" }),
    node("api", "openapi", "Billing API", { operations: "getInvoice" }),
    node("skills", "skills", "Skills"),
    node("billing", "sub_agent", "Billing specialist"),
    node("saved", "saved_agent", "Refunds", { agent: "ca_refunds" }),
  ],
  edges: [
    edge("entry", "tools", "http"),
    edge("entry", "tools", "kb"),
    edge("entry", "tools", "mcp"),
    edge("entry", "tools", "api"),
    edge("entry", "tools", "skills"),
    edge("entry", "agents", "billing"),
    edge("entry", "agents", "saved"),
  ],
}
const saved = {
  nodes: [
    node("r1", "agent", "Refunds desk"),
    node("r2", "sub_agent", "Refund checker"),
  ],
  edges: [edge("r1", "agents", "r2")],
}
const graph = testGraphOf(doc, {
  savedAgent: (id) => (id === "ca_refunds" ? saved : undefined),
  mcpTools: (id) => (id === "srv" ? ["list_tickets"] : undefined),
})

const call = (author, name, id, args = {}, extra = {}) => ({
  id: `e-${id}`,
  author,
  content: { role: "model", parts: [{ functionCall: { name, id, args } }] },
  ...extra,
})
const answer = (author, name, id, response = { ok: true }) => ({
  id: `r-${id}`,
  author,
  content: {
    role: "user",
    parts: [{ functionResponse: { name, id, response } }],
  },
})
const says = (author, text = "…", extra = {}) => ({
  id: `t-${author}-${text}`,
  author,
  content: { role: "model", parts: [{ text }] },
  ...extra,
})
const states = (progress) => Object.fromEntries(progress.overlay().steps)
const taken = (progress) => [...progress.overlay().taken].sort()

test("agents are found by their ADK names, a saved agent's as its node", () => {
  assert.equal(graph.entry, "entry")
  assert.equal(graph.agents.get("service_desk"), "entry")
  assert.equal(graph.agents.get("billing_specialist"), "billing")
  // A saved agent is called after the node it's placed at; its own agents are that node too.
  assert.equal(graph.agents.get("refunds"), "saved")
  assert.equal(graph.agents.get("refund_checker"), "saved")
})

test("calls lead to the tool node that offers them", () => {
  const to = (name, args) => graph.callees("entry", name, args ?? {})
  assert.deepEqual(to("look_up_order"), ["http"])
  assert.deepEqual(to("policies"), ["kb"])
  assert.deepEqual(to("policies_system_design"), ["kb"])
  assert.deepEqual(to("list_tickets"), ["mcp"])
  assert.deepEqual(to("get_invoice"), ["api"])
  assert.deepEqual(to("load_skill"), ["skills"])
  assert.deepEqual(
    to("transfer_to_agent", { agent_name: "billing_specialist" }),
    ["billing"]
  )
  // A task (or an agent asked as a tool) is called by its name.
  assert.deepEqual(to("refunds"), ["saved"])
  assert.deepEqual(to("nothing_here"), [])
  // Another agent's tools aren't the entry's.
  assert.deepEqual(graph.callees("billing", "look_up_order", {}), [])
})

test("a toolset whose tools aren't known takes a call only when it's the one left", () => {
  const open = testGraphOf({
    nodes: [
      node("a", "agent", "Desk"),
      node("m", "mcp", "Inline", { server: "", tools: "" }),
      node("h", "http_tool", "Ping"),
    ],
    edges: [edge("a", "tools", "m"), edge("a", "tools", "h")],
  })
  assert.deepEqual(open.callees("a", "anything", {}), ["m"])
  assert.deepEqual(open.callees("a", "ping", {}), ["h"])
  const two = testGraphOf({
    nodes: [
      node("a", "agent", "Desk"),
      node("m", "mcp", "One", { server: "", tools: "" }),
      node("n", "openapi", "Two", { operations: "" }),
    ],
    edges: [edge("a", "tools", "m"), edge("a", "tools", "n")],
  })
  assert.deepEqual(two.callees("a", "anything", {}), [])
})

test("a turn: the agent works, its tool spins until it answers, a hand-off moves on", () => {
  const progress = createTestProgress(graph)
  progress.start()
  assert.deepEqual(states(progress), { entry: "running" })
  assert.equal(progress.overlay().live, true)

  assert.equal(progress.event(says("service_desk", "Let me look")), false)
  assert.equal(
    progress.event(call("service_desk", "look_up_order", "c1")),
    true
  )
  assert.deepEqual(states(progress), { entry: "running", http: "running" })
  assert.deepEqual(taken(progress), ["entry-tools-http"])

  progress.event(answer("service_desk", "look_up_order", "c1"))
  assert.equal(states(progress).http, "done")

  progress.event(call("service_desk", "policies", "c2"))
  progress.event(
    answer("service_desk", "policies", "c2", { error: "no index" })
  )
  assert.equal(states(progress).kb, "failed")

  progress.event(
    call("service_desk", "transfer_to_agent", "c3", {
      agent_name: "billing_specialist",
    })
  )
  // Handed to along its line; it works once it answers.
  assert.ok(taken(progress).includes("entry-agents-billing"))
  assert.equal(states(progress).billing, undefined)
  progress.event(says("billing_specialist", "Hi"))
  assert.equal(states(progress).entry, "done")
  assert.equal(states(progress).billing, "running")

  progress.end()
  assert.equal(progress.overlay().live, false)
  assert.deepEqual(states(progress), {
    entry: "done",
    http: "done",
    kb: "failed",
    billing: "done",
  })

  // The next turn starts with the agent it was handed to.
  progress.start()
  assert.deepEqual(states(progress), { billing: "running" })
  assert.deepEqual(taken(progress), [])
})

test("an agent asking the person waits for them; their answer clears it", () => {
  const progress = createTestProgress(graph)
  progress.start()
  progress.event(
    call(
      "service_desk",
      "_adk_request_input",
      "q1",
      {},
      {
        longRunningToolIds: ["q1"],
      }
    )
  )
  progress.end()
  assert.equal(states(progress).entry, "waiting")
  progress.start()
  progress.end()
  assert.equal(states(progress).entry, "done")
})

test("a task handed off waits for the person, goes on, and hands back", () => {
  const progress = createTestProgress(graph)
  progress.start()
  progress.event(
    call("service_desk", "refunds", "t1", {}, { longRunningToolIds: ["t1"] })
  )
  progress.event(says("refunds", "Which order?"))
  progress.end()
  assert.equal(states(progress).saved, "waiting")
  assert.equal(states(progress).entry, "done")

  progress.start()
  assert.ok(taken(progress).includes("entry-agents-saved"))
  assert.equal(states(progress).entry, "done")
  assert.equal(states(progress).saved, "running")
  progress.event(says("refund_checker", "Checking"))
  assert.equal(states(progress).saved, "running")
  progress.event(answer("service_desk", "refunds", "t1"))
  assert.equal(states(progress).saved, "done")
  assert.equal(states(progress).entry, "running")
  progress.end()
  assert.equal(states(progress).entry, "done")
})

test("a turn that stops with an error marks the agent answering", () => {
  const progress = createTestProgress(graph)
  progress.start()
  progress.event(call("service_desk", "look_up_order", "c1"))
  progress.end(true)
  assert.equal(states(progress).entry, "failed")
  assert.equal(states(progress).http, "done")

  const errored = createTestProgress(graph)
  errored.start()
  errored.event(says("service_desk", "", { errorCode: "MODEL_ERROR" }))
  assert.equal(states(errored).entry, "failed")
})

test("another conversation draws nothing of the last", () => {
  const progress = createTestProgress(graph)
  progress.start()
  progress.event(says("billing_specialist"))
  progress.reset()
  assert.deepEqual(states(progress), {})
  progress.start()
  assert.deepEqual(states(progress), { entry: "running" })
})

test("requests filed to intake agents light the intakes they're with", () => {
  const desk = testGraphOf({
    nodes: [
      node("a", "agent", "Service desk"),
      node("access", "intake_agent", "Access request"),
      node("laptop", "intake_agent", "Laptop request"),
    ],
    edges: [edge("a", "tools", "access"), edge("a", "tools", "laptop")],
  })
  assert.deepEqual(
    desk.callees("a", "start_requests", { intakes: ["laptop_request"] }),
    ["laptop"]
  )
  // Which of several a request is with isn't in the call.
  assert.deepEqual(
    desk.callees("a", "answer_requests", { requests: ["r1"] }),
    []
  )

  const progress = createTestProgress(desk)
  progress.start()
  progress.event(
    call("service_desk", "start_requests", "s1", {
      intakes: ["laptop_request"],
    })
  )
  assert.equal(states(progress).laptop, "running")
  assert.equal(states(progress).access, undefined)
  progress.event(
    answer("service_desk", "start_requests", "s1", {
      started: [{ id: "r1", intake: "Laptop request" }],
      resumed: [],
    })
  )
  assert.equal(states(progress).laptop, "done")
  progress.end()

  // Its requests are with the intake their answer named.
  progress.start()
  progress.event(
    call("service_desk", "answer_requests", "s2", { requests: ["r1"] })
  )
  assert.equal(states(progress).laptop, "running")
  assert.deepEqual(taken(progress), ["a-tools-laptop"])
  progress.event(
    answer("service_desk", "answer_requests", "s2", {
      requests: [{ id: "r1", intake: "Laptop request" }],
    })
  )
  assert.equal(states(progress).laptop, "done")
  assert.equal(states(progress).access, undefined)
})
