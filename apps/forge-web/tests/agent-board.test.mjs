import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// A workflow's Decision board: choices people accept or reject each on its
// own, each with an Accepted and a Rejected way of its own, then Done.

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
const { toDocument, toGraph, parseAgent } = await load(
  "/src/features/adk-workflows/lib/document.ts"
)
const model = await load("/src/features/adk-workflows/lib/model.ts")
const { validateAgent } = await load(
  "/src/features/adk-workflows/lib/validate.ts"
)
const { agentTypes } = await load("/src/features/adk-workflows/lib/scope.ts")
const { resolveExpression } = await load(
  "/src/features/steps/lib/expressions.ts"
)
const { expressionSetting } = await load("/src/features/steps/lib/fields.ts")
const { typeLabel } = await load("/src/features/steps/lib/types.ts")
const { toneOf } = await load("/src/features/steps/lib/connections.ts")
const { stepDetail, stepSummary } = await load(
  "/src/features/adk-workflows/components/step-lines.ts"
)
const { openGatesOf, gateGroups } = await load("/src/features/runs/lib/runs.ts")
const { kindInfo } = await installNodeCatalog(server)
await server.close()

const CHOICES = [
  {
    id: "ship",
    label: "Ship it",
    question: "Ship {{ input.version }}?",
    ask_when: "",
  },
  { id: "tell", label: "Tell the team", question: "", ask_when: "input.risky" },
]

/** Start → board → a step per way, and a summary at Done. */
function release(choices = CHOICES) {
  const nodes = [
    ["start", "start", "Start"],
    [
      "board",
      "decision_board",
      "Release",
      { message: "What next for {{ input.version }}?", choices },
    ],
    [
      "shipped",
      "transform",
      "Shipped",
      { expression: "previous.decided.label" },
    ],
    ["held", "transform", "Held", { expression: "previous.decided.comment" }],
    ["summary", "end", "Summary", { result: "previous.accepted" }],
  ]
  const steps = nodes.map(([id, kind, name, config = {}]) => {
    const base = model.newNode(kind, [], name)
    return {
      id,
      data: { ...base, config: { ...base.config, ...config } },
      position: { x: 0, y: 0 },
    }
  })
  const edges = [
    ["start", "next", "board"],
    ["board", "ship:accepted", "shipped"],
    ["board", "ship:rejected", "held"],
    ["board", "done", "summary"],
  ]
  return toDocument(
    {
      id: "ag_test",
      name: "Test",
      description: "",
      organization_id: "org-1",
      created_at: "",
      updated_at: "",
    },
    {
      steps,
      connections: edges.map(([source, output, target]) => ({
        source,
        output,
        target,
      })),
    }
  )
}

test("a board is a person's step whose choices each have an Accepted and a Rejected way, then Done", () => {
  const info = kindInfo("decision_board")
  assert.equal(info.group, "people")
  assert.ok(info.person)
  const node = model.newNode("decision_board")
  const [first] = node.config.choices
  assert.deepEqual(
    model.outputsOf(node).map((o) => o.id),
    [`${first.id}:accepted`, `${first.id}:rejected`, "done"]
  )
  const board = { ...node, config: { ...node.config, choices: CHOICES } }
  const ways = model.outputsOf(board)
  assert.deepEqual(
    ways.map((o) => o.label),
    [
      "Accepted · Ship it",
      "Rejected · Ship it",
      "Accepted · Tell the team",
      "Rejected · Tell the team",
      "Done",
    ]
  )
  assert.deepEqual(
    ways.map((o, index) => toneOf(board, o, index)),
    ["positive", "negative", "positive", "negative", "neutral"]
  )
  assert.ok(model.PAUSING_KINDS.has("decision_board"))
})

test("a board goes to a document and back, and is ready to run", () => {
  const doc = release()
  assert.deepEqual(toDocument({ ...doc }, toGraph(doc)), doc)
  const read = parseAgent(JSON.stringify(doc), { organizationId: "org-1" })
  assert.deepEqual(read.notes, [])
  assert.deepEqual(
    validateAgent(toGraph(doc), {})
      .filter((i) => i.level === "error")
      .map((i) => i.id),
    []
  )
})

test("every choice needs a name, and a board a choice", () => {
  const unnamed = validateAgent(
    toGraph(release([{ ...CHOICES[0], label: " " }])),
    {}
  ).map((i) => i.id)
  assert.ok(
    unnamed.some((id) => id.includes("choice-label")),
    unnamed.join(", ")
  )
  const none = validateAgent(toGraph(release([])), {}).map((i) => i.id)
  assert.ok(
    none.some((id) => id.includes(":choices")),
    none.join(", ")
  )
})

test("its questions and conditions read the run's data, and later steps read the board as it stands", () => {
  const graph = toGraph(release())
  const board = graph.steps.find((s) => s.id === "board").data
  assert.equal(expressionSetting(board, "message").mode, "template")
  assert.equal(expressionSetting(board, "choices.ship").mode, "template")
  assert.equal(
    expressionSetting(board, "choices.tell.ask_when").mode,
    "expression"
  )
  const { scopeOf } = agentTypes(graph)
  const type = (expression, at = "shipped") =>
    typeLabel(resolveExpression(expression, scopeOf(at)))
  assert.equal(type("previous.decided.approved"), "boolean")
  assert.equal(type("previous.decided.decided_by"), "string")
  assert.equal(type("previous.accepted"), "array<string>")
  assert.equal(type("steps.board.output.decisions.tell.comment"), "string")
  assert.equal(type("previous.open", "summary"), "array<string>")
})

test("its card says how many choices, who decides and what it's about", () => {
  const board = toGraph(release()).steps.find((s) => s.id === "board").data
  assert.equal(stepDetail(board), "2 choices · admins decide")
  assert.deepEqual(stepSummary(board), {
    text: "What next for {{ input.version }}?",
  })
})

test("a Merge waiting for two of its choices is refused: either may be rejected", () => {
  const doc = release()
  const held = doc.nodes.find((n) => n.id === "held")
  doc.nodes.push({
    ...held,
    id: "both",
    kind: "merge",
    name: "Both",
    config: { mode: "all" },
  })
  doc.edges = doc.edges.filter((e) => e.target !== "held")
  doc.edges.push(
    {
      id: "e1",
      source: "board",
      source_output: "tell:accepted",
      target: "held",
    },
    { id: "e2", source: "shipped", source_output: "next", target: "both" },
    { id: "e3", source: "held", source_output: "next", target: "both" }
  )
  const found = validateAgent(toGraph(doc), {}).find((i) =>
    i.id.includes("merge-exclusive-board")
  )
  assert.ok(found, "refused")
  assert.match(found.message, /different choices of Release/)
})

test("a run's page shows a board's open choices together, and every other gate on its own", () => {
  const gate = (id, details = {}) => ({
    id,
    kind: "approval",
    reason: `${id}?`,
    details: { kind: "approval", ...details },
    requested_at: null,
    deadline: null,
  })
  const board = (step, choice) => ({
    step,
    board: { name: step, message: "", choice, label: choice, choices: 3 },
  })
  const gates = [
    gate("legal", { step: "legal" }),
    gate("a", board("release", "ship")),
    gate("b", board("release", "tell")),
    gate("c", board("cleanup", "drop")),
  ]
  assert.deepEqual(
    gateGroups(gates).map((g) =>
      g.kind === "board" ? `${g.step}:${g.gates.map((x) => x.id)}` : g.gate.id
    ),
    ["legal", "release:a,b", "cleanup:c"]
  )
  // Open while it carries on; none once it's finished; an older API's pause alone.
  assert.equal(openGatesOf({ status: "running", pause: null, gates }).length, 4)
  assert.deepEqual(openGatesOf({ status: "succeeded", pause: null, gates }), [])
  assert.deepEqual(openGatesOf({ status: "paused", pause: gates[0] }), [
    gates[0],
  ])
})
