import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog, readNodeCatalogs } from "./node-catalog.mjs"

// The kinds of node as the admin API serves them: a workflow's (the
// workflows task's node catalog) and the Agents canvas's (the agent
// runtime's), whose tools a workflow's LLM agents have too. The builders
// read every kind from them, and set up only the ones they know.

const serve = () =>
  createServer({
    configFile: false,
    root: fileURLToPath(new URL("..", import.meta.url)),
    server: { middlewareMode: true, watch: null },
    optimizeDeps: { noDiscovery: true, include: [] },
    resolve: {
      alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
    },
  })

// Nothing installed yet: what reads a kind says so.
const bare = await serve()
const unloaded = await bare.ssrLoadModule(
  "/src/features/adk-workflows/lib/model.ts"
)
const unloadedChat = await bare.ssrLoadModule(
  "/src/features/agents/lib/model.ts"
)
await bare.close()

const server = await serve()
const load = (path) => server.ssrLoadModule(path)
const model = await load("/src/features/adk-workflows/lib/model.ts")
const { parseAgent } = await load("/src/features/adk-workflows/lib/document.ts")
const { AGENT_ADAPTER } = await load(
  "/src/features/adk-workflows/lib/adapter.ts"
)
const { agentJsonSchema } = await load(
  "/src/features/adk-workflows/lib/schema.ts"
)
const tools = await load("/src/features/adk-workflows/lib/tools.ts")
const chatModel = await load("/src/features/agents/lib/model.ts")
const { CHAT_AGENT_ADAPTER } = await load("/src/features/agents/lib/adapter.ts")
const { chatAgentJsonSchema } = await load("/src/features/agents/lib/schema.ts")
const catalog = await installNodeCatalog(server)
await server.close()

const { workflow: SERVED, chat: CHAT_SERVED } = await readNodeCatalogs()
const ORG = "org-1"

/** A served catalog, changed, installed while `run` runs; the real one after. */
function withCatalog(change, run, { chat = false } = {}) {
  const [served, install] = chat
    ? [CHAT_SERVED, catalog.installChatNodeCatalog]
    : [SERVED, catalog.installNodeCatalog]
  const changed = structuredClone(served)
  change(changed)
  install(changed)
  try {
    run()
  } finally {
    install(served)
  }
}

test("before the catalogs are loaded, reading a kind says so", () => {
  assert.throws(
    () => unloaded.newNode("http"),
    /workflow node catalog isn't loaded/
  )
  assert.throws(
    () => unloadedChat.newNode("memory"),
    /chat agent node catalog isn't loaded/
  )
})

test("every kind served is one the builder sets up, and every one it sets up is served", () => {
  assert.deepEqual(
    catalog.servedKinds(),
    SERVED.kinds.map((kind) => kind.kind)
  )
  assert.deepEqual(
    [...catalog.servedKinds()].sort(),
    [...catalog.KNOWN_KINDS].sort()
  )
  assert.deepEqual(
    agentJsonSchema().$defs.node.properties.kind.enum,
    catalog.KNOWN_KINDS
  )
})

test("the library shows the kinds served, in their order and groups", () => {
  assert.deepEqual(AGENT_ADAPTER.kindList, catalog.servedKinds())
  assert.deepEqual(
    AGENT_ADAPTER.groups.map((group) => group.id),
    ["start", "agents", "people", "actions", "github", "logic", "finish"]
  )
  const http = AGENT_ADAPTER.kinds.http
  assert.equal(http.label, "HTTP request")
  assert.equal(http.group, "actions")
  assert.equal(http.idPrefix, "http")
  assert.ok(http.icon)
  assert.equal(AGENT_ADAPTER.kinds.llm.orb, 0)
  assert.equal(AGENT_ADAPTER.kinds.end.terminal, true)
  assert.equal(AGENT_ADAPTER.kinds.approval.person, true)
  assert.equal(AGENT_ADAPTER.kinds.http.person, undefined)
})

test("a kind the builder can't set up is left out; one no longer served can't be added or read", () => {
  withCatalog(
    (served) => {
      served.kinds = served.kinds.filter((kind) => kind.kind !== "delay")
      served.kinds.push({
        ...served.kinds.find((kind) => kind.kind === "transform"),
        kind: "teleport",
        label: "Teleport",
        icon: "no-such-icon",
      })
    },
    () => {
      assert.equal(AGENT_ADAPTER.kindList.includes("teleport"), false)
      assert.equal(AGENT_ADAPTER.kindList.includes("delay"), false)
      assert.equal(model.isAgentKind("teleport"), false)
      assert.equal(model.isAgentKind("delay"), false)
      assert.throws(() => model.newNode("delay"), /no kind delay/)
      const read = parseAgent(
        {
          format: "forge.agent/v1",
          id: "ag_test",
          name: "Test",
          nodes: [
            { id: "start", kind: "start", name: "Start", config: {} },
            { id: "wait", kind: "delay", name: "Wait", config: {} },
          ],
          edges: [],
        },
        { organizationId: ORG }
      )
      assert.deepEqual(
        read.doc.nodes.map((node) => node.id),
        ["start"]
      )
      assert.deepEqual(read.notes, [
        'nodes[1].kind "delay" isn\'t a node Forge knows; dropped.',
      ])
    }
  )
  // An icon the builder doesn't have is a plain square, not a missing one.
  withCatalog(
    (served) => {
      served.kinds.find((kind) => kind.kind === "http").icon = "no-such-icon"
    },
    () => assert.ok(AGENT_ADAPTER.kinds.http.icon)
  )
})

test("a node's ways out are its kind's, a case or rule each before them", () => {
  const ways = (kind, config = {}) =>
    model.outputsOf({ kind, name: kind, config })
  assert.deepEqual(ways("llm"), [{ id: "next", label: "Next" }])
  assert.deepEqual(ways("end"), [])
  assert.deepEqual(ways("http"), [
    { id: "success", label: "Success", branch: true },
    { id: "error", label: "Error", branch: true, fallback: true },
  ])
  // An HTTP request's status ways, each named by its statuses, before Success and Error.
  assert.deepEqual(
    ways("http", {
      statuses: [
        { id: "gone", codes: "404" },
        { id: "denied", codes: " 401, 403 " },
        { id: "blank", codes: "" },
      ],
    }),
    [
      { id: "gone", label: "404", branch: true },
      { id: "denied", label: "401, 403", branch: true },
      { id: "blank", label: "Status way 3", branch: true },
      { id: "success", label: "Success", branch: true },
      { id: "error", label: "Error", branch: true, fallback: true },
    ]
  )
  assert.deepEqual(ways("loop"), [
    { id: "each", label: "Each item", branch: true, body: true },
    { id: "done", label: "Done", branch: true },
  ])
  assert.deepEqual(
    ways("switch", {
      cases: [
        { id: "a", value: " high " },
        { id: "b", value: "" },
        { value: "no id" },
      ],
    }),
    [
      { id: "a", label: "high", branch: true },
      { id: "b", label: "Empty value", branch: true },
      { id: "default", label: "Default", branch: true, fallback: true },
    ]
  )
  assert.deepEqual(
    ways("match", {
      arms: [
        { id: "x", label: "Urgent" },
        { id: "y", label: "" },
      ],
    }),
    [
      { id: "x", label: "Urgent", branch: true },
      { id: "y", label: "Rule 2", branch: true },
      { id: "otherwise", label: "Otherwise", branch: true, fallback: true },
    ]
  )
})

test("a new node starts with its kind's settings, its own copy of them", () => {
  const first = model.newNode("switch")
  assert.deepEqual(
    first.config,
    SERVED.kinds.find((kind) => kind.kind === "switch").defaults
  )
  first.config.cases.push({ id: "c", value: "x" })
  assert.equal(model.newNode("switch").config.cases.length, 2)
  assert.match(model.nextNodeId("llm", []), /^agent_[a-z0-9]{5}$/)
  // An LLM sub-agent has an LLM node's settings, without what only a node has.
  const sub = model.subAgentDefaults("llm")
  assert.equal("mode" in sub, false)
  assert.equal("inputs" in sub, false)
  assert.equal(sub.include_contents, "default")
})

/* -------------------------------------------------------------------------- */
/* The Agents canvas                                                          */
/* -------------------------------------------------------------------------- */

test("every kind the Agents canvas is served is one it sets up, and every one it sets up is served", () => {
  assert.deepEqual(
    catalog.servedChatKinds(),
    CHAT_SERVED.kinds.map((kind) => kind.kind)
  )
  assert.deepEqual(
    [...catalog.servedChatKinds()].sort(),
    [...catalog.KNOWN_CHAT_KINDS].sort()
  )
  assert.deepEqual(
    chatAgentJsonSchema().$defs.node.properties.kind.enum,
    catalog.KNOWN_CHAT_KINDS
  )
  assert.deepEqual(CHAT_AGENT_ADAPTER.kindList, catalog.servedChatKinds())
  assert.deepEqual(
    CHAT_AGENT_ADAPTER.groups.map((group) => group.id),
    ["agent", "agents", "tools"]
  )
  assert.equal(CHAT_AGENT_ADAPTER.kinds.agent.label, "Chat agent")
  assert.equal(CHAT_AGENT_ADAPTER.kinds.sub_agent.orb, 2)
  assert.ok(CHAT_AGENT_ADAPTER.kinds.mcp.icon)
})

test("an agent's ways out are its tools and who it hands off to; only agents are handed off to", () => {
  const step = (kind) => chatModel.newNode(kind)
  assert.deepEqual(chatModel.outputsOf(step("agent")), [
    { id: "tools", label: "Tools", branch: true },
    { id: "agents", label: "Hands off to", branch: true },
  ])
  assert.deepEqual(chatModel.outputsOf(step("mcp")), [])
  const agent = step("agent")
  assert.equal(chatModel.accepts(agent, "tools", "mcp"), true)
  assert.equal(chatModel.accepts(agent, "tools", "sub_agent"), true)
  assert.equal(chatModel.accepts(agent, "agents", "sub_agent"), true)
  assert.equal(chatModel.accepts(agent, "agents", "saved_agent"), true)
  assert.equal(chatModel.accepts(agent, "agents", "mcp"), false)
  assert.equal(chatModel.accepts(agent, "tools", "agent"), false)
  assert.equal(chatModel.accepts(step("mcp"), "tools", "memory"), false)
  // As the catalog says: a kind it hands off to now can be.
  withCatalog(
    (served) => {
      served.kinds.find((kind) => kind.kind === "mcp").handed_to = true
    },
    () => assert.equal(chatModel.accepts(agent, "agents", "mcp"), true),
    { chat: true }
  )
})

test("a new node on the canvas starts with its kind's settings, its own copy of them", () => {
  const first = chatModel.newNode("http_tool")
  assert.deepEqual(
    first.config,
    CHAT_SERVED.kinds.find((kind) => kind.kind === "http_tool").defaults
  )
  first.config.headers.push({ id: "h", name: "X", value: "1" })
  assert.deepEqual(chatModel.newNode("http_tool").config.headers, [])
  assert.match(chatModel.nextNodeId("mcp", []), /^mcp_[a-z0-9]{5}$/)
})

test("a workflow's LLM agent is offered the tools served for workflows, called as the canvas calls them", () => {
  assert.deepEqual(
    tools.workflowToolKinds(),
    CHAT_SERVED.kinds.filter((kind) => kind.workflow_tool).map((k) => k.kind)
  )
  assert.deepEqual(
    [...tools.workflowToolKinds()].sort(),
    [...model.LLM_TOOL_KINDS].sort()
  )
  assert.equal(tools.workflowToolKinds().includes("memory"), false)
  const tool = tools.newTool("saved_agent", [])
  assert.equal(tool.name, "Saved agent")
  assert.deepEqual(tool.config, { agent: "" })
  // One no longer served for workflows isn't offered, and is dropped when read.
  withCatalog(
    (served) => {
      served.kinds.find((kind) => kind.kind === "openapi").workflow_tool = false
    },
    () => {
      assert.equal(tools.workflowToolKinds().includes("openapi"), false)
      const read = parseAgent(
        {
          format: "forge.agent/v1",
          id: "ag_test",
          name: "Test",
          nodes: [
            { id: "start", kind: "start", name: "Start", config: {} },
            {
              id: "agent",
              kind: "llm",
              name: "Agent",
              config: {
                tools: [
                  { id: "spec", kind: "openapi", name: "Spec", config: {} },
                ],
              },
            },
          ],
          edges: [],
        },
        { organizationId: ORG }
      )
      assert.deepEqual(read.doc.nodes[1].config.tools, [])
      assert.equal(read.notes.length, 1)
      assert.match(read.notes[0], /openapi/)
    },
    { chat: true }
  )
})
