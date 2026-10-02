import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// The agent builder's model without a browser: its document, its checks,
// what its expressions read, how Tidy up lays it out, and its store.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const { toDocument, toGraph, parseAgent, usesOf } = await load("/src/lib/agents/document.ts")
const { exampleAgent } = await load("/src/lib/agents/example.ts")
const model = await load("/src/lib/agents/model.ts")
const { validateAgent } = await load("/src/lib/agents/validate.ts")
const { agentTypes, stateType } = await load("/src/lib/agents/scope.ts")
const { resolveExpression } = await load("/src/lib/steps/expressions.ts")
const { typeLabel } = await load("/src/lib/steps/types.ts")
const { AGENT_ADAPTER } = await load("/src/lib/agents/adapter.ts")
const { createBuilderStore, documentOf } = await load("/src/components/builder/store.ts")
await server.close()

const ORG = "org-1"
const example = () => exampleAgent(ORG)
const meta = (doc) => ({ ...doc })
const issuesOf = (doc, context = {}) => validateAgent(toGraph(doc), context).map((i) => i.id)

/** A graph document from nodes `[id, kind, name, config]` and edges `[source, output, target]`. */
function agent(nodes, edges = []) {
  const steps = nodes.map(([id, kind, name, config = {}]) => {
    const base = model.newNode(kind, [], name)
    return { id, data: { ...base, config: { ...base.config, ...config } }, position: { x: 0, y: 0 } }
  })
  return toDocument(
    { id: "ag_test", name: "Test", description: "", organization_id: ORG, created_at: "", updated_at: "" },
    { steps, connections: edges.map(([source, output, target]) => ({ source, output, target })) }
  )
}

test("a document goes to the builder's graph and back unchanged", () => {
  const doc = example()
  const again = toDocument(meta(doc), toGraph(doc))
  assert.deepEqual({ ...again, layout: {} }, { ...doc, layout: {} })
  assert.deepEqual(usesOf(doc), [])
})

test("the example reads back whole, and is ready to build", () => {
  const doc = example()
  const read = parseAgent(JSON.stringify(doc), { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [])
  assert.equal(read.needsLayout, true)
  assert.deepEqual(read.doc.nodes, doc.nodes)
  assert.deepEqual(issuesOf(doc), [])
})

test("importing drops what doesn't fit and says so", () => {
  const doc = example()
  doc.nodes.push({ id: "odd", kind: "teleport", name: "Odd", config: {}, outputs: ["next"] })
  const team = doc.nodes.find((n) => n.id === "bug_team")
  team.config.sub_agents.push({ id: "x", kind: "wizard", name: "X", config: {} })
  team.config.sub_agents[0].config.max_output_tokens = "lots"
  // Settings since retired go without a word.
  team.config.sub_agents[1].config.output_key = "reply"
  team.config.sub_agents[1].config.temperature = 0.2
  doc.nodes.find((n) => n.id === "customer").config.method = "FETCH"
  doc.edges.push({ id: "e", source: "triage", source_output: "next", target: "start" })
  doc.edges.push({ id: "f", source: "route", source_output: "nowhere", target: "billing" })
  const read = parseAgent(doc, { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [
    'nodes[1].config.method should be one of GET, POST, PUT, PATCH, DELETE.',
    'nodes[6].config.sub_agents[0].config.max_output_tokens should be a number, or null.',
    'nodes[6].config.sub_agents[2].kind "wizard" isn\'t a sub-agent Forge knows; dropped.',
    'nodes[23].kind "teleport" isn\'t a node Forge knows; dropped.',
    "edges[25] leads into the start; dropped.",
    'edges[26].source_output "nowhere" isn\'t an output of route; dropped.',
  ])
  assert.equal(read.doc.nodes.find((n) => n.id === "customer").config.method, "GET")
  const kept = read.doc.nodes.find((n) => n.id === "bug_team")
  assert.equal(kept.config.sub_agents.length, 2)
  assert.equal(kept.config.sub_agents[0].config.max_output_tokens, null)
  assert.equal("output_key" in kept.config.sub_agents[1].config, false)
  assert.equal(parseAgent("{", { organizationId: ORG }).ok, false)
  assert.match(parseAgent({ format: "forge.workflow/v1", nodes: [] }, { organizationId: ORG }).error, /isn't a Forge ADK workflow/)
})

test("names are ADK names, kept apart across nodes and sub-agents", () => {
  assert.equal(model.adkName("Billing specialist"), "billing_specialist")
  assert.equal(model.adkName("2nd opinion!"), "nd_opinion")
  const steps = toGraph(example()).steps.map((s) => s.data)
  assert.equal(model.newNode("llm", steps, "Writer").name, "Writer 2")
  assert.equal(model.newNode("llm", steps).name, "LLM agent")
  const copy = model.copyNode(steps.find((s) => s.name === "Bug team"), steps)
  assert.equal(copy.name, "Bug team copy")
  assert.deepEqual(
    copy.config.sub_agents.map((a) => a.name),
    ["Reproducer copy", "Fix drafter copy"]
  )
  assert.notEqual(copy.config.sub_agents[0].id, "reproduce")
})

test("the checks find what ADK would refuse and what wouldn't run", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["a", "llm", "Writer", { instruction: "Write." }],
      ["b", "sequential", "Team", {
        sub_agents: [{ id: "s1", kind: "llm", name: "writer", config: { ...model.subAgentDefaults("llm"), instruction: "" } }],
      }],
      ["c", "llm", "User", { instruction: "Hi." }],
      ["d", "parallel", "Empty"],
      ["e", "switch", "Route", { value: "previous", cases: [{ id: "r1", value: "A" }, { id: "r2", value: "A" }] }],
      ["f", "merge", "Merge", { mode: "any" }],
      ["g", "transform", "Lonely", { expression: "previous" }],
      ["h", "transform", "Around", { expression: "previous" }],
      // Forge's steps are checked as a workflow checks them.
      ["i", "http", "Fetch", { url: "" }],
      ["j", "loop", "Each", { items: "[1, 2]", item_name: "an item" }],
    ],
    [
      ["start", "next", "a"],
      ["a", "next", "b"],
      ["b", "next", "c"],
      ["c", "next", "d"],
      ["d", "next", "e"],
      ["e", "r1", "f"],
      ["f", "next", "h"],
      ["h", "next", "f"],
      ["e", "default", "i"],
      ["i", "success", "j"],
      ["j", "each", "g"],
      ["g", "next", "j"],
    ]
  )
  assert.deepEqual(issuesOf(doc), [
    "a:same-writer",
    "b:same-writer-s1",
    "b:s1:instruction",
    "c:name",
    "d:team",
    "e:case-twice",
    "i:url",
    "j:item-name",
    // Warnings after errors, in the order of the nodes.
    "e:open-r2",
    // A circle with no way out never ends; a loop's body is left by Done.
    "f:cycle",
    "h:cycle",
  ])
  // Each issue about a setting names it, for its field in the settings to show.
  const fields = Object.fromEntries(validateAgent(toGraph(doc)).map((i) => [i.id, i.field]))
  assert.deepEqual(
    [
      "a:same-writer",
      "b:same-writer-s1",
      "b:s1:instruction",
      "c:name",
      "d:team",
      "e:case-twice",
      "i:url",
      "j:item-name",
      "e:open-r2",
      "f:cycle",
    ].map((id) => fields[id]),
    ["name", "agents.s1.name", "agents.s1.instruction", "name", "sub_agents", "cases", "url", "item_name", undefined, undefined]
  )
  // ADK builds only what the start reaches, and nothing leads back to it.
  doc.edges = doc.edges.filter((e) => e.target !== "i")
  doc.edges.push({ id: "back", source: "c", source_output: "next", target: "start" })
  const ids = issuesOf(doc)
  assert.ok(ids.includes("i:unreached") && ids.includes("j:unreached") && ids.includes("start:start-in"))
})

test("a merge that waits for all can't wait on ways a run takes only one of", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["h", "http", "Fetch", { url: "https://example.com" }],
      ["ok", "transform", "Fine", { expression: "previous" }],
      ["bad", "transform", "Broken", { expression: "1" }],
      ["m", "merge", "Merge", { mode: "all" }],
      ["side", "transform", "Aside", { expression: "1" }],
      ["n", "merge", "Both", { mode: "all" }],
      ["i", "if", "Big?", { condition: "true" }],
      ["x", "transform", "Either", { expression: "1" }],
      ["o", "merge", "Joined", { mode: "all" }],
    ],
    [
      ["start", "next", "h"],
      ["h", "success", "ok"],
      ["h", "error", "bad"],
      ["ok", "next", "m"],
      ["bad", "next", "m"],
      // Ways that both come: the start's, into a merge that waits for all.
      ["start", "next", "side"],
      ["side", "next", "n"],
      ["m", "next", "n"],
      // Both ways of an If lead on to one step: it comes either way.
      ["start", "next", "i"],
      ["i", "true", "x"],
      ["i", "false", "x"],
      ["x", "next", "o"],
      ["side", "next", "o"],
    ]
  )
  assert.deepEqual(issuesOf(doc), ["m:merge-exclusive-h"])
  assert.match(validateAgent(toGraph(doc))[0].message, /different ways out of Fetch/)
  doc.nodes.find((n) => n.id === "m").config.mode = "any"
  assert.deepEqual(issuesOf(doc), [])
})

test("documents saved before Forge's steps joined read as them", () => {
  const sub = (id, kind, config) => ({ id, kind, name: id, config: { ...model.subAgentDefaults("llm"), ...config } })
  const old = {
    format: "forge.agent/v1",
    id: "ag_old",
    name: "Old",
    nodes: [
      { id: "start", kind: "start", name: "Start", config: { input_schema: {} }, outputs: ["next"] },
      { id: "fn", kind: "function", name: "Fn", config: { expression: "input" }, outputs: ["next"] },
      {
        id: "rt",
        kind: "router",
        name: "Rt",
        config: { route_on: "input.x", routes: [{ id: "a", value: "A" }] },
        outputs: ["a", "default"],
      },
      { id: "jn", kind: "join", name: "Jn", config: {}, outputs: ["next"] },
      {
        id: "lp",
        kind: "loop",
        name: "Lp",
        config: {
          description: "",
          max_iterations: 2,
          sub_agents: [
            { id: "inner", kind: "loop", name: "Inner", config: { description: "", max_iterations: 1, sub_agents: [sub("w", "llm", { instruction: "Go." })] } },
          ],
        },
        outputs: ["next"],
      },
    ],
    edges: [
      { id: "1", source: "start", source_output: "next", target: "fn" },
      { id: "2", source: "fn", source_output: "next", target: "rt" },
      { id: "3", source: "rt", source_output: "a", target: "jn" },
      { id: "4", source: "rt", source_output: "default", target: "lp" },
    ],
  }
  const read = parseAgent(old, { organizationId: ORG })
  assert.equal(read.ok, true)
  assert.deepEqual(read.notes, [])
  const byId = Object.fromEntries(read.doc.nodes.map((n) => [n.id, n]))
  assert.deepEqual(
    read.doc.nodes.map((n) => n.kind),
    ["start", "transform", "switch", "merge", "loop_agent"]
  )
  assert.deepEqual(byId.fn.config, { expression: "input", output_schema: {} })
  assert.deepEqual(byId.rt.config, { value: "input.x", cases: [{ id: "a", value: "A" }] })
  assert.deepEqual(byId.rt.outputs, ["a", "default"])
  assert.equal(byId.jn.config.mode, "all")
  assert.equal(byId.lp.config.sub_agents[0].kind, "loop_agent")
  assert.equal(read.doc.edges.length, 4)
})

test("every kind of node goes to a document and back", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["wait", "delay", "Wait", { amount: 2, unit: "hours" }],
      ["rules", "match", "Rules", { arms: [{ id: "big", label: "Big", condition: "input.n > 9" }] }],
      ["other", "saved", "Other", { agent: "ag_b" }],
      ["ask", "human_input", "Ask", { message: "Go on?" }],
      ["ok", "approval", "OK?", { timeout_hours: 24 }],
      ["done", "end", "Done", { outcome: "failed", result: "previous" }],
    ],
    [
      ["start", "next", "wait"],
      ["wait", "next", "rules"],
      ["rules", "big", "other"],
      ["rules", "otherwise", "ask"],
      ["ask", "next", "ok"],
      ["ok", "approved", "done"],
    ]
  )
  assert.deepEqual(toDocument(meta(doc), toGraph(doc)), doc)
  const read = parseAgent(JSON.stringify(doc), { organizationId: ORG })
  assert.deepEqual(read.notes, [])
  assert.deepEqual(read.doc.nodes, doc.nodes)
  assert.deepEqual(usesOf(doc), ["ag_b"])
  assert.deepEqual(
    doc.nodes.map((n) => n.outputs),
    [["next"], ["next"], ["big", "otherwise"], ["next"], ["next"], ["approved", "rejected"], []]
  )
})

test("a saved agent can't be itself, gone, or run this one back", () => {
  const doc = agent([["start", "start", "Start"], ["s", "saved", "Other", { agent: "ag_b" }]], [["start", "next", "s"]])
  const agents = new Map([
    ["ag_b", { name: "B", uses: ["ag_c"] }],
    ["ag_c", { name: "C", uses: ["ag_test"] }],
  ])
  assert.deepEqual(issuesOf(doc, { selfId: "ag_test", agents }), ["s:agent"])
  assert.match(validateAgent(toGraph(doc), { selfId: "ag_test", agents })[0].message, /B runs this ADK workflow/)
  assert.deepEqual(issuesOf(doc, { selfId: "ag_test", agents: new Map() }), ["s:agent"])
  assert.deepEqual(issuesOf(doc, { selfId: "ag_b" }), ["s:agent"])
  assert.deepEqual(issuesOf(doc, { selfId: "ag_test", agents: new Map([["ag_b", { name: "B", uses: [] }]]) }), [])
})

test("expressions read Forge's data: input, previous, steps, a loop's item, and the state", () => {
  const graph = toGraph(example())
  const { scopeOf, outputOf } = agentTypes(graph)
  const type = (expression, at) => typeLabel(resolveExpression(expression, scopeOf(at)))
  // The switch reads what the merge handed on: each agent's answer by step ID.
  assert.equal(type("previous.triage.category", "route"), '"BUG" | "BILLING" | "ELSE"')
  assert.equal(type("previous.sentiment.mood", "route"), '"calm" | "annoyed" | "angry"')
  assert.equal(type("steps.triage.output.summary", "route"), "string")
  // input is the run's, everywhere.
  assert.equal(type("input.customer_id", "sum_up"), "string")
  // An HTTP step's body as declared, and why it failed on its error way.
  assert.equal(type("steps.customer.output.body.plan", "route"), '"free" | "pro" | "enterprise"')
  assert.equal(type("steps.customer.error.message", "unknown_customer"), "string")
  // A branching step hands on what came in, and keeps the way it took.
  assert.equal(type("previous.send", "send_it"), "boolean")
  assert.deepEqual(Object.keys(outputOf("route").properties), ["branch"])
  // In a loop's body: its item and where it is.
  assert.equal(type("contact", "email"), "string")
  assert.equal(type("index", "email"), "integer")
  // State: the run's input, and each LLM sub-agent's answer under its ID.
  assert.deepEqual(Object.keys(stateType(graph).properties).sort(), [
    "critic",
    "docs",
    "draft_fix",
    "forum",
    "input",
    "reproduce",
    "writer",
  ])
  assert.equal(type("state.docs", "sum_up"), "string")
  // A transform's output is what it declares.
  assert.deepEqual(Object.keys(outputOf("sum_up").properties), ["docs", "forum"])
  // A path that leads nowhere is an issue.
  const doc = example()
  doc.nodes.find((n) => n.id === "route").config.value = "previous.triage.catgory"
  assert.deepEqual(issuesOf(doc), ["route:field:value:0"])
})

test("instructions are templates, checked against what the node can read", () => {
  const doc = example()
  const triage = doc.nodes.find((n) => n.id === "triage")
  const team = doc.nodes.find((n) => n.id === "bug_team")
  triage.config.instruction = "Read {{ input.mesage }}."
  team.config.sub_agents[1].config.instruction = "Fix {{ state.reproduce }} for {{ steps.nowhere.output }}."
  // A step that isn't there is an error; a field the input may not have, a warning.
  assert.deepEqual(issuesOf(doc), ["bug_team:field:agents.draft_fix.instruction:0", "triage:field:instruction:0"])
  assert.deepEqual(
    validateAgent(toGraph(doc)).map((i) => i.field),
    ["agents.draft_fix.instruction", "instruction"]
  )
  // ADK's {key} isn't filled in any more: a warning says how to write it.
  triage.config.instruction = "Read {triage} and {{ input.message }}."
  team.config.sub_agents[1].config.instruction = "Fix it."
  const found = validateAgent(toGraph(doc))
  assert.deepEqual(found.map((i) => i.id), ["triage:instruction-braces"])
  assert.match(found[0].message, /\{\{ state\.triage \}\}/)
  // A sub-agent's ID is where its answer is kept: once each, and never input.
  team.config.sub_agents[1].id = "input"
  assert.ok(issuesOf(doc).includes("bug_team:state-input"))
})

test("a switch's cases must be values what it switches on can have", () => {
  const doc = example()
  const route = doc.nodes.find((n) => n.id === "route")
  // Triage's schema says a category is BUG, BILLING or ELSE.
  route.config.cases[0].value = "BUGS"
  assert.deepEqual(issuesOf(doc), ["route:case-value-bug"])
  assert.equal(validateAgent(toGraph(doc))[0].field, "cases.bug")
  assert.match(validateAgent(toGraph(doc))[0].message, /BUG, BILLING or ELSE/)
  route.config.cases[0].value = "BUG"
  assert.deepEqual(issuesOf(doc), [])
})

test("Tidy up lays the graph out from its start, a way back pushing nothing right", () => {
  const graph = toGraph(example())
  const measure = { size: () => ({ width: 240, height: 64 }), port: () => 28 }
  const at = AGENT_ADAPTER.tidy(graph, measure)
  const x = (id) => at.get(id).x
  assert.equal(x("start"), 0)
  assert.equal(x("triage"), x("sentiment"))
  assert.ok(x("read") > x("triage") && x("route") > x("read"))
  assert.ok(x("check_fix") > x("bug_team"))
  assert.ok(x("customer") > x("start") && x("triage") > x("customer"))
  graph.connections.push({ source: "polish", output: "next", target: "route" })
  const again = AGENT_ADAPTER.tidy(graph, measure)
  assert.equal(again.get("route").x, x("route"))
})

test("the builder store edits an agent: new names, copies, and a sub-agent edit undone", () => {
  let clock = 0
  const now = Date.now
  Date.now = () => clock
  try {
    const store = createBuilderStore({ adapter: AGENT_ADAPTER, doc: example(), context: {} })
    const s = () => store.getState()
    const added = s().addStep("llm", { x: 0, y: 0 }, { source: "sum_up", output: "next" })
    assert.equal(s().nodes.find((n) => n.id === added).data.name, "LLM agent")
    const again = s().addStep("llm", { x: 0, y: 0 })
    assert.equal(s().nodes.find((n) => n.id === again).data.name, "LLM agent 2")

    const copy = s().duplicateStep("polish")
    assert.deepEqual(
      s().nodes.find((n) => n.id === copy).data.config.sub_agents.map((a) => a.name),
      ["Writer copy", "Critic copy"]
    )

    // Typing in a sub-agent's instruction is one step of undo.
    const type = (text) =>
      s().updateStep(
        "polish",
        (step) => model.updateSubAgent(step, ["writer"], (a) => ({ ...a, config: { ...a.config, instruction: text } })),
        "writer:instruction"
      )
    const before = s().past.length
    type("Write it")
    clock += 300
    type("Write it well")
    assert.equal(s().past.length, before + 1)
    const writer = () => model.subAgentAt(s().nodes.find((n) => n.id === "polish").data, ["writer"])
    assert.equal(writer().config.instruction, "Write it well")
    s().undo()
    assert.equal(
      writer().config.instruction,
      "Write the reply to the customer from this answer: {{ steps.billing.output.answer }}\n\nTake in any critique: {{ state.critic }}"
    )

    // A case removed takes its edge with it.
    s().updateStep("route", (step) => ({
      ...step,
      config: { ...step.config, cases: step.config.cases.filter((c) => c.id !== "bug") },
    }))
    assert.equal(documentOf(s()).edges.some((e) => e.source_output === "bug"), false)
  } finally {
    Date.now = now
  }
})
