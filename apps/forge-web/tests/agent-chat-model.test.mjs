import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// The Agents page's model without a browser: a chat agent's document, what
// can be attached where, its checks, and the builder store holding it.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const { toDocument, toGraph, parseChatAgent, usesOf } = await load("/src/features/agents/lib/document.ts")
const { exampleChatAgent } = await load("/src/features/agents/lib/example.ts")
const model = await load("/src/features/agents/lib/model.ts")
const { validateChatAgent } = await load("/src/features/agents/lib/validate.ts")
const { CHAT_AGENT_ADAPTER } = await load("/src/features/agents/lib/adapter.ts")
const { CHAT_AGENT_JSON_SCHEMA } = await load("/src/features/agents/lib/schema.ts")
const { createBuilderStore, documentOf } = await load("/src/features/builder/components/store.ts")
await server.close()

const ORG = "org-1"
const example = () => exampleChatAgent(ORG)
const issuesOf = (doc, context = {}) => validateChatAgent(toGraph(doc), context).map((i) => i.id)

/** A chat agent from nodes `[id, kind, name, config]` and edges `[source, output, target]`. */
function agent(nodes, edges = []) {
  const steps = nodes.map(([id, kind, name, config = {}]) => {
    const base = model.newNode(kind, [], name)
    return { id, data: { ...base, config: { ...base.config, ...config } }, position: { x: 0, y: 0 } }
  })
  return toDocument(
    { id: "ca_test", name: "Test", description: "", organization_id: ORG, created_at: "", updated_at: "" },
    { steps, connections: edges.map(([source, output, target]) => ({ source, output, target })) }
  )
}

const told = { instruction: "Help." }

test("the example goes to the builder's graph and back, reads back whole, and is ready", () => {
  const doc = example()
  assert.deepEqual({ ...toDocument(doc, toGraph(doc)), layout: {} }, { ...doc, layout: {} })
  const read = parseChatAgent(JSON.stringify(doc), { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [])
  assert.equal(read.needsLayout, true)
  assert.deepEqual(read.doc.nodes, doc.nodes)
  assert.deepEqual(issuesOf(doc), [])
  assert.deepEqual(usesOf(doc), [])
})

test("only agents are handed off to; anything but the chat agent is a tool", () => {
  const root = model.newNode("agent")
  const tool = model.newNode("http_tool")
  assert.equal(model.accepts(root, model.HANDS_OFF, "sub_agent"), true)
  assert.equal(model.accepts(root, model.HANDS_OFF, "saved_agent"), true)
  assert.equal(model.accepts(root, model.HANDS_OFF, "memory"), false)
  assert.equal(model.accepts(root, model.TOOLS, "sub_agent"), true)
  assert.equal(model.accepts(root, model.TOOLS, "adk_workflow"), true)
  assert.equal(model.accepts(root, model.TOOLS, "agent"), false)
  assert.equal(model.accepts(tool, model.TOOLS, "memory"), false)
  assert.deepEqual(model.outputsOf(tool), [])
  assert.deepEqual(
    model.outputsOf(model.newNode("sub_agent")).map((o) => o.id),
    [model.TOOLS, model.HANDS_OFF]
  )
})

test("importing drops what doesn't fit and says so", () => {
  const doc = example()
  doc.nodes.push({ id: "agent_two", kind: "agent", name: "Two", config: {}, outputs: [] })
  doc.nodes.find((n) => n.id === "orders").config.method = "FETCH"
  doc.edges.push({ id: "x", source: "agent", source_output: "agents", target: "memory" })
  const read = parseChatAgent(doc, { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [
    'nodes[2].config.method should be one of GET, POST, PUT, PATCH, DELETE.',
    "nodes[6] is a second chat agent; an agent has one, so it was dropped.",
    "edges[5]: agent can't lead to memory that way; dropped.",
  ])
  assert.match(parseChatAgent({ format: "forge.agent/v1", nodes: [] }, { organizationId: ORG }).error, /isn't a Forge agent/)
})

test("the checks find what ADK would refuse", () => {
  const doc = agent(
    [
      ["agent", "agent", "Helper", { instruction: "" }],
      ["a", "sub_agent", "Specialist", { ...told, mode: "task" }],
      ["b", "sub_agent", "Specialist", told],
      ["e", "http_tool", "Call it", { url: "api.example.com" }],
      ["f", "openapi", "Spec", { source: "inline", spec: "{ nope" }],
      ["g", "mcp", "Server"],
      ["h", "memory", "Lonely memory"],
    ],
    [
      ["agent", "agents", "a"],
      ["agent", "agents", "b"],
      ["a", "agents", "b"],
      ["agent", "tools", "e"],
      ["agent", "tools", "f"],
      ["agent", "tools", "g"],
    ]
  )
  assert.deepEqual(issuesOf(doc), [
    "agent:instruction",
    // Both are called Specialist.
    "a:same-specialist",
    "a:task-leaf",
    "b:same-specialist",
    // Two agents hand off to it.
    "b:parents",
    "e:url",
    "f:spec",
    "g:url",
    // Warnings after errors.
    "a:description",
    "b:description",
    "e:description",
    "h:unreached",
  ])
  const fields = Object.fromEntries(validateChatAgent(toGraph(doc)).map((i) => [i.id, i.field]))
  assert.equal(fields["agent:instruction"], "instruction")
  assert.equal(fields["a:task-leaf"], "mode")
  assert.equal(fields["e:url"], "url")
})

test("agents saved with Google's built-in tools open without them", () => {
  const doc = example()
  doc.nodes.push({ id: "search", kind: "google_search", name: "Google Search", config: {}, outputs: [] })
  doc.edges.push({ id: "s", source: "agent", source_output: "tools", target: "search" })
  const read = parseChatAgent(doc, { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [
    'nodes[6].kind "google_search" isn\'t something Forge\'s agents know; dropped.',
    "edges[5].target isn't a node; dropped.",
  ])
  assert.equal(read.doc.nodes.some((n) => n.kind === "google_search"), false)
  assert.equal(model.CHAT_KIND_LIST.some((kind) => ["google_search", "url_context", "code_execution"].includes(kind)), false)
})

test("agents calling each other in a circle, and saved agents and workflows that aren't there", () => {
  const doc = agent(
    [
      ["agent", "agent", "Helper", told],
      ["a", "sub_agent", "One", { ...told, description: "One." }],
      ["b", "sub_agent", "Two", { ...told, description: "Two." }],
      ["s", "saved_agent", "Other", { agent: "ca_gone" }],
      ["w", "adk_workflow", "Flow", { workflow: "ag_gone" }],
    ],
    [
      ["agent", "agents", "a"],
      ["a", "tools", "b"],
      ["b", "tools", "a"],
      ["agent", "tools", "s"],
      ["agent", "tools", "w"],
    ]
  )
  const context = {
    selfId: "ca_test",
    agents: new Map([["ca_other", { name: "Other", uses: ["ca_test"] }]]),
    workflows: new Map(),
  }
  assert.deepEqual(issuesOf(doc, context), ["a:cycle", "b:cycle", "s:agent", "w:workflow"])
  doc.nodes.find((n) => n.id === "s").config.agent = "ca_other"
  assert.match(
    validateChatAgent(toGraph(doc), context).find((i) => i.id === "s:agent").message,
    /Other uses this agent/
  )
})

test("the builder store refuses a hand-off to a tool, and adds what fits", () => {
  const store = createBuilderStore({ adapter: CHAT_AGENT_ADAPTER, doc: example(), context: {} })
  const s = () => store.getState()
  const before = s().edges.length
  s().connect("agent", model.HANDS_OFF, "memory")
  assert.equal(s().edges.length, before)
  const added = s().addStep("mcp", { x: 0, y: 0 }, { source: "agent", output: model.TOOLS })
  assert.ok(documentOf(s()).edges.some((e) => e.target === added && e.source_output === "tools"))
  assert.equal(s().nodes.find((n) => n.id === added).data.name, "MCP server")
})

test("Tidy up lays the agent out first, what it calls and hands off to after it", () => {
  const graph = toGraph(example())
  const measure = { size: () => ({ width: 240, height: 64 }), port: () => 28 }
  const at = CHAT_AGENT_ADAPTER.tidy(graph, measure)
  assert.equal(at.get("agent").x, 0)
  assert.ok(at.get("orders").x > 0 && at.get("billing").x > 0)
  assert.ok(at.get("billing_api").x > at.get("billing").x)
})

test("the JSON Schema knows every kind", () => {
  assert.deepEqual(CHAT_AGENT_JSON_SCHEMA.$defs.node.properties.kind.enum, model.CHAT_KIND_LIST)
  for (const kind of model.CHAT_KIND_LIST) {
    const config = CHAT_AGENT_JSON_SCHEMA.$defs[`config_${kind}`]
    assert.deepEqual(Object.keys(config.properties).sort(), Object.keys(model.CHAT_KINDS[kind].defaults()).sort(), kind)
  }
})
