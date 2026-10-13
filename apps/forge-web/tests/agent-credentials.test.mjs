import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// API credentials in the builders, without a browser: where one is sent (as
// the runtime decides), and what the checks say about the credential an HTTP
// step, HTTP tool or OpenAPI tool signs its requests with.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const { sendsTo, mayGo, originOf, sentToLabel } = await load("/src/features/credentials/lib/display.ts")
const chat = {
  ...(await load("/src/features/agents/lib/document.ts")),
  ...(await load("/src/features/agents/lib/validate.ts")),
  model: await load("/src/features/agents/lib/model.ts"),
}
const flow = {
  ...(await load("/src/features/adk-workflows/lib/document.ts")),
  ...(await load("/src/features/adk-workflows/lib/validate.ts")),
  model: await load("/src/features/adk-workflows/lib/model.ts"),
  tools: await load("/src/features/adk-workflows/lib/tools.ts"),
}
await installNodeCatalog(server)
await server.close()

const ORG = "org-1"
const API = "https://api.example.com/v1"

/** A builder document from nodes `[id, kind, name, config]` and edges `[source, output, target]`. */
function build(kit, id, nodes, edges = []) {
  const steps = nodes.map(([step, kind, name, config = {}]) => {
    const base = kit.model.newNode(kind, [], name)
    return { id: step, data: { ...base, config: { ...base.config, ...config } }, position: { x: 0, y: 0 } }
  })
  return kit.toDocument(
    { id, name: "Test", description: "", organization_id: ORG, created_at: "", updated_at: "" },
    { steps, connections: edges.map(([source, output, target]) => ({ source, output, target })) }
  )
}

/** The organization's credentials as the builders look them up. */
const credentials = new Map([
  ["cr_billing", { name: "Billing", method: "Bearer token", urls: [API], interactive: false, connected: true }],
  ["cr_github", { name: "GitHub", method: "OAuth sign-in", urls: ["https://api.github.com"], interactive: true, connected: false }],
])

test("a credential goes where the runtime sends it: its scheme, host, port and path", () => {
  for (const [url, sent] of [
    ["https://api.example.com/v1", true],
    ["https://api.example.com/v1/orders?page=2", true],
    ["https://API.example.com:443/v1/orders", true],
    ["https://api.example.com/v10", false],
    ["https://api.example.com/v2/orders", false],
    ["http://api.example.com/v1/orders", false],
    ["https://api.example.com:8443/v1", false],
    ["https://evil.example.com/v1", false],
    ["https://api.example.com.evil.io/v1", false],
    ["not a url", false],
    // Where the request lands, its dot segments resolved.
    ["https://api.example.com/v1/./orders", true],
    ["https://api.example.com/v1/../admin", false],
    ["https://api.example.com/v1/%2e%2e/admin", false],
  ]) {
    assert.equal(sendsTo(url, [API]), sent, url)
  }
  assert.equal(sendsTo("https://api.example.com/anything", ["https://api.example.com"]), true)
  assert.equal(sendsTo("https://api.example.com/x", []), false)
})

test("before a run, a URL with placeholders is judged by what's written out", () => {
  // Written out.
  assert.equal(mayGo("https://api.example.com/v1/orders", [API]), true)
  assert.equal(mayGo("https://api.example.com/v2/orders", [API]), false)
  // Only the query is filled in: the address is known.
  assert.equal(mayGo("https://api.example.com/v1/orders?id={{ previous.id }}", [API]), true)
  assert.equal(mayGo("https://api.example.com/v2/orders?id={{ previous.id }}", [API]), false)
  // The path goes on from a placeholder: under it, or can't tell.
  assert.equal(mayGo("https://api.example.com/v1/orders/{order_id}", [API]), undefined)
  assert.equal(mayGo("https://api.example.com/{{ state.version }}/orders", [API]), undefined)
  assert.equal(mayGo("https://api.example.com/v{{ state.version }}", [API]), undefined)
  // But never under it: another path, another host, a way out of it.
  assert.equal(mayGo("https://api.example.com/v2/{{ state.id }}", [API]), false)
  assert.equal(mayGo("https://other.example.com/v1/{{ state.id }}", [API]), false)
  assert.equal(mayGo("https://api.example.com/v1/../admin/{{ state.id }}", [API]), false)
  // Where to is all filled in.
  assert.equal(mayGo("{{ state.url }}", [API]), undefined)
  assert.equal(mayGo("https://${API_HOST}/v1/orders", [API]), undefined)
})

test("a new credential starts with the step's address, and says where it goes", () => {
  assert.equal(originOf("https://api.example.com/v1/orders?page=2"), "https://api.example.com")
  assert.equal(originOf("https://api.example.com/v1/{{ state.id }}"), "https://api.example.com")
  assert.equal(originOf("{{ state.url }}"), undefined)
  assert.equal(sentToLabel(["https://api.example.com/v1/", "https://api.github.com"]), "api.example.com/v1, api.github.com")
})

test("an HTTP tool's credential: gone, nobody signed in, or not sent where it calls", () => {
  const doc = build(chat, "ca_test", [
    ["agent", "agent", "Helper", { instruction: "Help." }],
    ["ok", "http_tool", "Charges", { url: `${API}/charges`, credential: "cr_billing", description: "d" }],
    ["gone", "http_tool", "Orders", { url: `${API}/orders`, credential: "cr_deleted", description: "d" }],
    ["away", "http_tool", "Repos", { url: "https://api.github.com/repos", credential: "cr_billing", description: "d" }],
    ["out", "http_tool", "Issues", { url: "https://api.github.com/issues", credential: "cr_github", description: "d" }],
    ["spec", "openapi", "Spec", { source: "url", url: "https://api.example.com/openapi.json", credential: "cr_deleted" }],
  ], [
    ["agent", "tools", "ok"],
    ["agent", "tools", "gone"],
    ["agent", "tools", "away"],
    ["agent", "tools", "out"],
    ["agent", "tools", "spec"],
  ])
  const found = chat.validateChatAgent(chat.toGraph(doc), { credentials })
  const credentialIssues = found.filter((i) => i.field === "credential")
  assert.deepEqual(
    credentialIssues.map((i) => [i.id, i.level]),
    [
      ["gone:credential", "error"],
      ["away:credential-url", "error"],
      ["spec:credential", "error"],
      ["out:credential-sign-in", "warning"],
    ]
  )
  assert.match(credentialIssues[1].message, /Billing is sent only to api\.example\.com\/v1/)
  // While the credentials load, nothing is said about them.
  assert.deepEqual(chat.validateChatAgent(chat.toGraph(doc)).filter((i) => i.field === "credential"), [])
})

test("a workflow's HTTP step and an LLM step's HTTP tool are checked the same way", () => {
  const tool = {
    ...flow.tools.newTool("http_tool", []),
    id: "tool_http",
    name: "Look up repo",
    description: "Looks up a repository.",
  }
  tool.config.url = "https://api.github.com/repos/{name}"
  tool.config.credential = "cr_billing"
  const doc = build(flow, "ag_test", [
    ["start", "start", "Start"],
    ["h", "http", "Fetch", { url: "https://api.example.com/v2/orders", credential: "cr_billing" }],
    ["a", "llm", "Helper", { instruction: "Help.", tools: [tool] }],
  ], [
    ["start", "next", "h"],
    ["h", "success", "a"],
  ])
  const found = Object.fromEntries(
    flow.validateAgent(flow.toGraph(doc), { credentials }).filter((i) => /credential/.test(i.id)).map((i) => [i.id, i.field])
  )
  assert.deepEqual(found, {
    "h:credential-url": "credential",
    "a:tool-tool_http-credential-url": "tools.tool_http.credential",
  })
  // Pointed where it's sent, the step is fine.
  doc.nodes.find((n) => n.id === "h").config.url = `${API}/orders/{{ steps.start.output.id }}`
  assert.ok(!flow.validateAgent(flow.toGraph(doc), { credentials }).some((i) => i.id.startsWith("h:credential")))
})
