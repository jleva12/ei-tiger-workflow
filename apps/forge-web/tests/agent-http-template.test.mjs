import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// An HTTP tool's URL and body as templates, read as the runtime reads them
// (forge_agent_runtime.http_templates): what the model is offered, and an
// older tool's arguments written in.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
})
const load = (path) => server.ssrLoadModule(path)
const http = await load("/src/features/agents/lib/http-template.ts")
const { readSettings } = await load("/src/features/agents/lib/document.ts")
const { exampleChatAgent } = await load("/src/features/agents/lib/example.ts")
const { toGraph } = await load("/src/features/agents/lib/document.ts")
const { validateChatAgent } = await load("/src/features/agents/lib/validate.ts")
await installNodeCatalog(server)
await server.close()

const tool = (config) => ({
  method: "POST",
  url: "",
  body: "",
  parameters: {},
  ...config,
})

test("the URL's path arguments are required and a query pair's own aren't", () => {
  const { names, required } = http.urlArguments(
    "https://api.example.com/{{ input.region }}/stores/{store}/orders?status={status}&page=p{page}&${KEY}"
  )
  assert.deepEqual(names, ["store", "status", "page"])
  assert.deepEqual([...required].sort(), ["page", "store"])
})

test("the model is offered what the URL and body name, as declared", () => {
  const config = tool({
    url: "https://api.example.com/orders/{order_id}?expand={expand}",
    body: '{"note": "{note}", "total": {total}, "who": {{ input.user }}, "x": "${TOKEN}"}',
    parameters: {
      type: "object",
      properties: {
        expand: { type: "string", description: "What to include." },
        note: { type: "string" },
        gone: { type: "number" },
      },
      required: ["note"],
    },
  })
  assert.deepEqual(http.toolParameters(config), {
    type: "object",
    properties: {
      order_id: { type: "string" },
      expand: { type: "string", description: "What to include." },
      note: { type: "string" },
      total: { type: "string" },
    },
    required: ["order_id", "note", "total"],
  })
  assert.deepEqual(http.unusedParameters(config), ["gone"])
  // Its editor shows what's named nowhere too, so it can be put in or taken out.
  assert.deepEqual(Object.keys(http.editedParameters(config).properties), [
    "order_id",
    "expand",
    "note",
    "total",
    "gone",
  ])
  // Undeclared, a query pair's own argument is optional; the path's is required.
  assert.deepEqual(
    http.toolParameters(
      tool({ method: "GET", url: "https://x.example.com/{store}?page={page}" })
    ).required,
    ["store"]
  )
  // A GET sends no body: what only the body names isn't offered.
  assert.deepEqual(
    Object.keys(http.toolParameters({ ...config, method: "GET" }).properties),
    ["order_id", "expand"]
  )
})

test("a body is JSON once its bare placeholders are values", () => {
  assert.equal(
    http.parseBody('{"a": {a}, "b": [{{ input.x }}], "c": "{c} and }"}').ok,
    true
  )
  const broken = http.parseBody('{"a": a}')
  assert.equal(broken.ok, false)
  assert.deepEqual(http.bodyArguments('{"a": {a}, "b": "x {b}", "c": [{a}]}'), [
    "a",
    "b",
  ])
})

test("an HTTP tool from before its body is read with its arguments written in", () => {
  const parameters = {
    type: "object",
    properties: { order_id: { type: "string" }, expand: { type: "string" } },
  }
  const getting = readSettings(
    "http_tool",
    {
      method: "GET",
      url: "https://api.example.com/orders/{order_id}",
      parameters,
    },
    "nodes[0].config",
    []
  )
  assert.equal(
    getting.url,
    "https://api.example.com/orders/{order_id}?expand={expand}"
  )
  assert.equal(getting.body, "")
  const posting = readSettings(
    "http_tool",
    {
      method: "POST",
      url: "https://api.example.com/orders/{order_id}",
      parameters,
    },
    "nodes[0].config",
    []
  )
  assert.equal(posting.url, "https://api.example.com/orders/{order_id}")
  assert.equal(posting.body, '{\n  "expand": {expand}\n}')
  // One with a body is as it is.
  const current = readSettings(
    "http_tool",
    { method: "POST", url: "https://x.example.com/", body: "", parameters },
    "nodes[0].config",
    []
  )
  assert.equal(current.body, "")
})

test("a body that isn't JSON, and an argument named nowhere, are reported", () => {
  const doc = exampleChatAgent("org-1")
  const node = doc.nodes.find((n) => n.kind === "http_tool")
  node.config = {
    ...node.config,
    method: "POST",
    url: "{{ state.base }}/orders",
    body: '{"note": note}',
    parameters: { type: "object", properties: { gone: { type: "string" } } },
  }
  const issues = validateChatAgent(toGraph(doc), {}).filter((i) =>
    i.id.includes(node.id)
  )
  const said = issues.map((i) => i.message).join("\n")
  assert.match(said, /Its body isn't JSON/)
  assert.match(said, /gone isn't in its URL, query parameters or body/)
  // A URL that starts with a run's value is one once it's filled in.
  assert.doesNotMatch(said, /A URL starts with/)
})

test("listed query parameters name arguments: a whole value's is optional, the rest required", () => {
  const query = [
    { id: "a", name: "status", value: "{status}" },
    { id: "b", name: "q", value: "about {topic} " },
    { id: "c", name: "{field}", value: " {since} " },
    { id: "d", name: "region", value: "{{ state.region }}" },
    { id: "e", name: "", value: "{ignored}" },
  ]
  const { names, required } = http.queryArguments(query)
  assert.deepEqual(names, ["status", "topic", "field", "since"])
  assert.deepEqual([...required].sort(), ["field", "topic"])

  // The model is offered them after the URL's; one only a whole value is optional.
  const offered = http.toolParameters(
    tool({
      method: "GET",
      url: "https://api.example.com/{store}/search",
      query,
    })
  )
  assert.deepEqual(Object.keys(offered.properties), [
    "store",
    "status",
    "topic",
    "field",
    "since",
  ])
  assert.deepEqual(offered.required, ["store", "topic", "field"])
  assert.deepEqual(
    http.unusedParameters(
      tool({
        url: "https://api.example.com/search",
        query,
        parameters: { type: "object", properties: { status: {}, gone: {} } },
      })
    ),
    ["gone"]
  )
})

test("query parameters are read from a document, and each needs a name", () => {
  const notes = []
  const read = readSettings(
    "http_tool",
    {
      method: "GET",
      url: "https://api.example.com/search",
      body: "",
      query: [{ name: "q", value: "{q}" }, "nonsense"],
    },
    "nodes[0].config",
    notes
  )
  assert.equal(read.query.length, 1)
  assert.equal(read.query[0].name, "q")
  assert.ok(read.query[0].id)
  assert.match(notes.join("\n"), /isn't an object/)

  const doc = exampleChatAgent("org-1")
  const node = doc.nodes.find((n) => n.kind === "http_tool")
  node.config = { ...node.config, query: [{ id: "x", name: " ", value: "1" }] }
  const issues = validateChatAgent(toGraph(doc), {}).filter((i) =>
    i.id.includes(node.id)
  )
  assert.match(
    issues.map((i) => i.message).join("\n"),
    /Every query parameter needs a name/
  )
})
