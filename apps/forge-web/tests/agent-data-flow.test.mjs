import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// How data flows between a workflow's nodes, as the builder types it: what
// each node is handed and hands on, what its fields complete with, and what
// its checks say. Each case is what a run does (packages/python/adk-workflows).

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const { toDocument, toGraph } = await load("/src/features/adk-workflows/lib/document.ts")
const model = await load("/src/features/adk-workflows/lib/model.ts")
const { validateAgent } = await load("/src/features/adk-workflows/lib/validate.ts")
const { agentTypes } = await load("/src/features/adk-workflows/lib/scope.ts")
const { checkField, completeField, resolveExpression } = await load("/src/features/steps/lib/expressions.ts")
const { typeLabel } = await load("/src/features/steps/lib/types.ts")
await installNodeCatalog(server)
await server.close()

/** A workflow document from nodes `[id, kind, name, config]` and edges `[source, output, target]`. */
function agent(nodes, edges = [], id = "ag_test", name = "Test") {
  const steps = nodes.map(([nodeId, kind, nodeName, config = {}]) => {
    const base = model.newNode(kind, [], nodeName)
    return { id: nodeId, data: { ...base, config: { ...base.config, ...config } }, position: { x: 0, y: 0 } }
  })
  return toDocument(
    { id, name, description: "", organization_id: "org-1", created_at: "", updated_at: "" },
    { steps, connections: edges.map(([source, output, target]) => ({ source, output, target })) }
  )
}

const label = (type) => (type ? typeLabel(type) : "none")
const typeIn = (scope, expression) => label(resolveExpression(expression, scope))
const labels = (scope, text, mode = "expression", check) =>
  completeField(text, text.length, mode, scope, check)?.options.map((o) => o.label) ?? []
const problems = (scope, text, check) => checkField(text, "expression", scope, check).map((d) => d.message)
const issues = (doc, context) => validateAgent(toGraph(doc), context).map((i) => i.id)

const BODY = {
  type: "object",
  properties: {
    data: {
      type: "object",
      properties: {
        items: {
          type: "array",
          items: {
            type: "object",
            properties: { id: { type: "string" }, tags: { type: "array", items: { type: "string" } } },
            required: ["id"],
          },
        },
        total: { type: "integer" },
      },
    },
  },
}
const FETCH = ["fetch", "http", "Fetch", { url: "https://example.test/orders", output_schema: BODY }]
const sub = (id, name, schema = {}) => ({
  id,
  kind: "llm",
  name,
  config: { ...model.subAgentDefaults("llm"), instruction: "Answer.", output_schema: schema },
})

test("a loop goes through a list deep in what came in: its item and its fields complete in its body", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      FETCH,
      ["loop", "loop", "Each order", { items: "previous.body.data.items", item_name: "order" }],
      ["work", "transform", "Work", { expression: '{ "id": order.id, "first": order.tags[0] }' }],
      ["after", "transform", "After", { expression: "previous.results" }],
    ],
    [
      ["start", "next", "fetch"],
      ["fetch", "success", "loop"],
      ["loop", "each", "work"],
      // The way back carries each item's result to the loop's results, not into the loop.
      ["work", "next", "loop"],
      ["loop", "done", "after"],
    ]
  )
  assert.deepEqual(issues(doc), [])
  const { scopeOf, outputOf } = agentTypes(toGraph(doc))
  const loop = scopeOf("loop")
  assert.equal(label(loop.roots.get("previous").type), "object")
  assert.deepEqual(labels(loop, "previous."), ["status", "headers", "body"])
  assert.deepEqual(labels(loop, "previous.body.data."), ["items", "total"])
  // Its body reads the item, typed as the list's items.
  const work = scopeOf("work")
  assert.equal(label(work.roots.get("order").type), "object")
  assert.deepEqual(labels(work, "order."), ["id", "tags"])
  assert.equal(typeIn(work, "order.tags[0]"), "string")
  assert.equal(typeIn(work, "index"), "integer")
  // Its declared fields may have more besides (the schema is open): a slip, likely.
  assert.match(problems(work, "order.nope")[0], /doesn't declare nope/)
  // The loop's results are what its body handed back.
  assert.equal(typeIn(scopeOf("after"), "previous.results[0].first"), "string")
  assert.equal(label(outputOf("loop")), "object")
})

test("a list field offers every list it can go through, however deep", () => {
  const doc = agent(
    [
      ["start", "start", "Start", { input_schema: { type: "object", properties: { labels: { type: "array", items: { type: "string" } } } } }],
      FETCH,
      ["each", "for_each", "Each", { items: "" }],
      ["work", "transform", "Work", { expression: "item" }],
    ],
    [["start", "next", "fetch"], ["fetch", "success", "each"], ["each", "each", "work"]]
  )
  const scope = agentTypes(toGraph(doc)).scopeOf("each")
  const found = completeField("", 0, "expression", scope, { expect: "list" })
  assert.equal(found.filtered, true)
  const lists = found.options.filter((o) => o.section?.name === "Lists to go through")
  assert.deepEqual(
    lists.map((o) => o.label),
    ["input.labels", "previous.body.data.items", "steps.fetch.output.body.data.items"]
  )
  // A step's list shows the step, then the rest of its path.
  assert.equal(lists[2].display, "Fetch › output.body.data.items")
  assert.equal(lists[2].detail, "array<object>")
  // Then where a path can start.
  assert.ok(found.options.some((o) => o.label === "previous" && o.section?.name === "Start from"))
})

test("typing a field's name finds it anywhere, written whole", () => {
  const doc = agent(
    [["start", "start", "Start"], FETCH, ["use", "transform", "Use", { expression: "" }]],
    [["start", "next", "fetch"], ["fetch", "success", "use"]]
  )
  const scope = agentTypes(toGraph(doc)).scopeOf("use")
  const found = labels(scope, "tot")
  assert.deepEqual(found.slice(0, 2), ["previous.body.data.total", "steps.fetch.output.body.data.total"])
  // A root that matches is still there, after the fields.
  assert.ok(labels(scope, "prev").includes("previous"))
  // Inside a template's {{ }} too.
  assert.ok(labels(scope, "Total: {{ tot", "template").includes("previous.body.data.total"))
})

test("a step with several ways in reads what any of them hands on", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["if", "if", "If", { condition: "true" }],
      ["a", "transform", "A", { expression: '{ "x": 1, "y": "a" }' }],
      ["b", "transform", "B", { expression: '{ "x": 2 }' }],
      ["c", "transform", "C", { expression: '"text"' }],
      ["joined", "transform", "Joined", { expression: "previous.x" }],
      ["mixed", "transform", "Mixed", { expression: "previous" }],
    ],
    [
      ["start", "next", "if"],
      ["if", "true", "a"],
      ["if", "false", "b"],
      ["a", "next", "joined"],
      ["b", "next", "joined"],
      ["joined", "next", "c"],
      ["a", "next", "mixed"],
      ["c", "next", "mixed"],
    ]
  )
  const { scopeOf } = agentTypes(toGraph(doc))
  // Objects merge: the fields both have are typed, the others may be missing.
  assert.equal(typeIn(scopeOf("joined"), "previous.x"), "integer")
  assert.equal(typeIn(scopeOf("joined"), "previous.y"), "string")
  // Unlike things: anything.
  assert.equal(label(scopeOf("mixed").roots.get("previous").type), "any")
})

test("an Error way hands on nothing: why is in the step's error", () => {
  const doc = agent(
    [["start", "start", "Start"], FETCH, ["oops", "transform", "Oops", { expression: "steps.fetch.error.body" }]],
    [["start", "next", "fetch"], ["fetch", "error", "oops"]]
  )
  assert.deepEqual(issues(doc), [])
  const scope = agentTypes(toGraph(doc)).scopeOf("oops")
  assert.equal(label(scope.roots.get("previous").type), "null")
  assert.match(scope.roots.get("previous").type.description, /steps\.fetch\.error/)
  assert.deepEqual(labels(scope, "steps.fetch.error."), ["message", "status", "body", "expression"])
  assert.match(problems(scope, "steps.fetch.error.reason")[0], /has no reason/)
  assert.match(problems(scope, "previous.status")[0], /null/)
})

test("a merge holds what each way in handed on: an If's, what came into it", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["t", "transform", "T", { expression: '{ "a": 1 }' }],
      ["if", "if", "If", { condition: "previous.a > 0" }],
      ["u", "transform", "U", { expression: '{ "b": "x" }' }],
      ["m", "merge", "M", { mode: "any" }],
      ["after", "transform", "After", { expression: "previous" }],
    ],
    [
      ["start", "next", "t"],
      ["t", "next", "if"],
      ["if", "true", "m"],
      ["if", "false", "u"],
      ["u", "next", "m"],
      ["m", "next", "after"],
    ]
  )
  const scope = agentTypes(toGraph(doc)).scopeOf("after")
  assert.equal(typeIn(scope, "previous.if.a"), "integer")
  assert.equal(typeIn(scope, "previous.u.b"), "string")
  // The If itself records the way it took.
  assert.equal(typeIn(scope, "steps.if.output.branch"), "string")
})

test("a team agent hands on its sub-agents' answers, typed by what they declare", () => {
  const verdict = { type: "object", properties: { verdict: { type: "string", enum: ["ok", "bad"] } }, required: ["verdict"] }
  const score = { type: "object", properties: { score: { type: "number" } } }
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["seq", "sequential", "Review", { sub_agents: [sub("draft", "Drafter"), sub("judge", "Judge", verdict)] }],
      ["par", "parallel", "Panel", { sub_agents: [sub("p1", "Writer"), sub("p2", "Scorer", score)] }],
      ["again", "loop_agent", "Refine", { sub_agents: [sub("l1", "Critic", verdict), sub("l2", "Fixer", verdict)] }],
      ["end", "end", "End", { result: "previous" }],
    ],
    [["start", "next", "seq"], ["seq", "next", "par"], ["par", "next", "again"], ["again", "next", "end"]]
  )
  const { scopeOf, outputOf } = agentTypes(toGraph(doc))
  const end = scopeOf("end")
  // A sequential agent: its last sub-agent's answer.
  assert.equal(typeIn(end, "steps.seq.output.verdict"), '"ok" | "bad"')
  assert.match(outputOf("seq").description, /Judge/)
  // A parallel agent: each sub-agent's answer, by its ID.
  assert.equal(typeIn(end, "steps.par.output.p1"), "string")
  assert.equal(typeIn(end, "steps.par.output.p2.score"), "number")
  // A loop agent: the answer of whichever answered last.
  assert.equal(typeIn(end, "previous.verdict"), '"ok" | "bad"')
})

test("a saved workflow hands back its End's result, and is handed what fits its start", () => {
  const other = agent(
    [
      ["start", "start", "Start", { input_schema: { type: "object", properties: { email: { type: "string" } }, required: ["email"] } }],
      ["look", "transform", "Look", { expression: '{ "name": "Ada", "score": 3 }' }],
      ["end", "end", "End", { result: '{ "who": previous.name, "score": previous.score }' }],
    ],
    [["start", "next", "look"], ["look", "next", "end"]],
    "ag_other",
    "Lookup"
  )
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["shape", "transform", "Shape", { expression: '{ "mail": "a@b.c" }' }],
      ["run", "saved", "Run lookup", { agent: "ag_other" }],
      ["use", "transform", "Use", { expression: "previous.who" }],
    ],
    [["start", "next", "shape"], ["shape", "next", "run"], ["run", "next", "use"]]
  )
  const context = {
    selfId: "ag_test",
    agents: new Map([
      ["ag_other", { name: "Lookup", uses: [], document: other }],
      ["ag_test", { name: "Test", uses: ["ag_other"], document: doc }],
    ]),
  }
  const { scopeOf } = agentTypes(toGraph(doc), { workflow: (id) => context.agents.get(id)?.document, within: ["ag_test"] })
  assert.equal(typeIn(scopeOf("use"), "previous.who"), "string")
  assert.equal(typeIn(scopeOf("use"), "steps.run.output.score"), "integer")
  // What comes in lacks the email its start requires.
  const found = validateAgent(toGraph(doc), context).find((i) => i.id === "run:input-fit")
  assert.equal(found.field, "agent")
  assert.match(found.message, /has no email, which is required/)
  // Without its documents, it isn't typed, and nothing loops.
  assert.equal(label(agentTypes(toGraph(doc)).outputOf("run")), "any")
  const self = agent([["start", "start", "Start"], ["run", "saved", "Again", { agent: "ag_test" }]], [["start", "next", "run"]])
  const loops = new Map([["ag_test", { name: "Test", uses: ["ag_test"], document: self }]])
  assert.equal(label(agentTypes(toGraph(self), { workflow: (id) => loops.get(id)?.document, within: [] }).outputOf("run")), "any")
})

test("JSONata's list functions keep the list's items typed, and type the functions they're given", () => {
  const doc = agent(
    [["start", "start", "Start"], FETCH, ["use", "transform", "Use", { expression: "" }]],
    [["start", "next", "fetch"], ["fetch", "success", "use"]]
  )
  const scope = agentTypes(toGraph(doc)).scopeOf("use")
  const items = "previous.body.data.items"
  assert.equal(typeIn(scope, `$filter(${items}, function($o){ $count($o.tags) > 0 })[0].id`), "string")
  assert.equal(typeIn(scope, `$sort(${items}, function($a, $b){ $a.id > $b.id })`), "array<object>")
  assert.equal(typeIn(scope, `$map(${items}, function($o){ { "key": $o.id } })[0].key`), "string")
  assert.equal(typeIn(scope, `${items} ~> $filter(function($o){ $o.id = "x" })`), "array<object>")
  assert.equal(typeIn(scope, `$single(${items}, function($o){ $o.id = "x" }).tags`), "array<string>")
  assert.equal(typeIn(scope, `$append(${items}, ${items})[0].id`), "string")
  // A function's parameter is checked as the item it's handed.
  assert.match(problems(scope, `$map(${items}, function($o){ $o.nope })`)[0], /\$o doesn't declare nope/)
  // A loop over a filtered list reads its items typed.
  const loop = agent(
    [
      ["start", "start", "Start"],
      FETCH,
      ["each", "for_each", "Each", { items: `$filter(${items}, function($o){ $o.id != "" })` }],
      ["work", "transform", "Work", { expression: "item.id" }],
    ],
    [["start", "next", "fetch"], ["fetch", "success", "each"], ["each", "each", "work"]]
  )
  assert.equal(typeIn(agentTypes(toGraph(loop)).scopeOf("work"), "item.id"), "string")
})

test("a condition that's never true is flagged: only true holds", () => {
  const doc = agent(
    [
      ["start", "start", "Start", { input_schema: { type: "object", properties: { status: { type: "string" }, ok: { type: "boolean" } } } }],
      ["text", "if", "Text", { condition: "input.status" }],
      ["flag", "if", "Flag", { condition: "input.ok" }],
      ["compare", "if", "Compare", { condition: 'input.status = "open"' }],
    ],
    [["start", "next", "text"], ["text", "true", "flag"], ["flag", "true", "compare"]]
  )
  const found = validateAgent(toGraph(doc)).filter((i) => i.id.includes(":field:condition"))
  assert.deepEqual(found.map((i) => i.step), ["text"])
  assert.match(found[0].message, /text, never true/)
})

test("an item can't take a name every step reads, and a loop inside one with its item's name is flagged", () => {
  const doc = agent(
    [
      ["start", "start", "Start", { input_schema: { type: "object", properties: { xs: { type: "array", items: { type: "array", items: { type: "number" } } } } } }],
      ["outer", "for_each", "Outer", { items: "input.xs" }],
      ["inner", "for_each", "Inner", { items: "item" }],
      ["bad", "for_each", "Bad", { items: "input.xs", item_name: "input" }],
      ["work", "transform", "Work", { expression: "item" }],
    ],
    [["start", "next", "outer"], ["outer", "each", "inner"], ["inner", "each", "work"], ["outer", "done", "bad"]]
  )
  const found = validateAgent(toGraph(doc))
  const named = found.find((i) => i.id === "bad:item-name")
  assert.equal(named.level, "error")
  assert.match(named.message, /the run's input/)
  const shadow = found.find((i) => i.id === "inner:item-shadows-outer")
  assert.equal(shadow.level, "warning")
  assert.equal(shadow.field, "item_name")
})

test("a saved workflow started with its inputs is held to its start's fields, not what comes in", () => {
  const other = agent(
    [
      [
        "start",
        "start",
        "Start",
        {
          input_schema: {
            type: "object",
            properties: { email: { type: "string" }, limit: { type: "integer" } },
            required: ["email"],
          },
        },
      ],
    ],
    [],
    "ag_other",
    "Lookup"
  )
  const run = (inputs) =>
    agent(
      [
        ["start", "start", "Start", { input_schema: { type: "object", properties: { mail: { type: "string" } } } }],
        ["shape", "transform", "Shape", { expression: '{ "mail": "a@b.c" }' }],
        ["run", "saved", "Run lookup", { agent: "ag_other", inputs }],
      ],
      [["start", "next", "shape"], ["shape", "next", "run"]]
    )
  const context = { selfId: "ag_test", agents: new Map([["ag_other", { name: "Lookup", uses: [], document: other }]]) }
  const of = (inputs) => validateAgent(toGraph(run(inputs)), context).filter((i) => i.step === "run")

  // None filled in: what comes in is its input, and is checked against its start.
  assert.deepEqual(of({}).map((i) => i.id), ["run:input-fit"])
  // Any filled in: they're its input. Each required one must be said, each
  // is held to its field's type, and one it no longer takes is flagged.
  const found = of({ limit: "input.mail", gone: "input.mail" })
  assert.deepEqual(found.map((i) => [i.id, i.field]), [
    ["run:input-email", "inputs.email"],
    ["run:input-gone", "inputs.gone"],
    ["run:field:inputs.limit:0", "inputs.limit"],
  ])
  assert.match(found[0].message, /Lookup needs its input email/)
  assert.match(found[2].message, /text, not a whole number/)
  assert.deepEqual(of({ email: "input.mail", limit: "" }), [])
  // Its fields complete with the run's data, as any expression does.
  const { scopeOf } = agentTypes(toGraph(run({})))
  assert.ok(labels(scopeOf("run"), "input.").includes("mail"))
})

test("an agent from the Agents page is given inputs that fit what it declares", () => {
  const doc = agent(
    [
      ["start", "start", "Start", { input_schema: { type: "object", properties: { tier: { type: "string" } } } }],
      ["ask", "llm", "Ask", { source: "agent", agent: "ca_1", inputs: { limit: "input.tier", tier: "input.tier" } }],
    ],
    [["start", "next", "ask"]]
  )
  const chatAgents = new Map([
    ["ca_1", {
      name: "Helper",
      published: 1,
      hasDraft: false,
      input: { type: "object", properties: { limit: { type: "integer" }, tier: { type: "string" } } },
    }],
  ])
  const found = validateAgent(toGraph(doc), { chatAgents }).filter((i) => i.id.startsWith("ask:field:"))
  assert.deepEqual(found.map((i) => i.field), ["inputs.limit"])
  assert.match(found[0].message, /text, not a whole number/)
})

test("an instruction may put whole objects in, for the model; a person's message is told it reads as JSON", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      FETCH,
      ["write", "llm", "Writer", { instruction: "Summarize {{ previous.body }}" }],
      ["ask", "human_input", "Ask", { message: "Is this right? {{ steps.fetch.output.body }}" }],
    ],
    [["start", "next", "fetch"], ["fetch", "success", "write"], ["write", "next", "ask"]]
  )
  assert.deepEqual(
    validateAgent(toGraph(doc)).filter((i) => i.id.includes(":field:")).map((i) => i.step),
    ["ask"]
  )
})

test("a transform's expression is held to what it declares it makes", () => {
  const doc = agent(
    [
      ["start", "start", "Start"],
      ["t", "transform", "Shape", {
        expression: '{ "count": "three" }',
        output_schema: { type: "object", properties: { count: { type: "integer" } }, required: ["count"] },
      }],
    ],
    [["start", "next", "t"]]
  )
  const found = validateAgent(toGraph(doc)).find((i) => i.id === "t:output-fit")
  assert.equal(found.field, "expression")
  assert.match(found.message, /count is text, not a whole number/)
})
