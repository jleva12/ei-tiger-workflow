import assert from "node:assert/strict"
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// The builder's store, driven the way the canvas, the library and the step
// dialog drive it, against a record of what it did (UPDATE_GOLDEN=1 writes
// the record again). It pins the workflow builder's behaviour while its
// pieces are shared with other builders.

// Step IDs and case IDs are random: the same sequence every run.
let seed = 7
Object.defineProperty(globalThis.crypto, "getRandomValues", {
  configurable: true,
  value: (array) => {
    for (let i = 0; i < array.length; i += 1) {
      seed = (seed * 1103515245 + 12345) % 2147483648
      array[i] = seed % 256
    }
    return array
  },
})
let clock = Date.parse("2026-09-28T12:00:00.000Z")
Date.now = () => clock

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const { createWorkflowBuilderStore, documentOf } = await server.ssrLoadModule(
  "/src/components/workflows/builder-store.ts"
)
const { exampleWorkflow } = await server.ssrLoadModule("/src/lib/workflows/example.ts")
await server.close()

const GOLDEN = fileURLToPath(new URL("./fixtures/workflow-builder-store.golden.json", import.meta.url))

// Every step 240 wide; a 64px header, and a 28px row per branch.
const measure = {
  size: () => ({ width: 240, height: 96 }),
  port: (_id, _handle, type) => (type === "target" ? 28 : 40),
}

function run() {
  const doc = {
    ...exampleWorkflow("org-1"),
    id: "wf_golden",
    created_at: "2026-09-28T00:00:00.000Z",
    updated_at: "2026-09-28T00:00:00.000Z",
  }
  const store = createWorkflowBuilderStore({ doc, context: { selfId: "wf_golden" } })
  const s = () => store.getState()
  const record = []
  const note = (label) => {
    const state = s()
    record.push({
      label,
      document: documentOf(state),
      past: state.past.length,
      future: state.future.length,
      editing: state.editing,
      selected: state.nodes.filter((n) => n.selected).map((n) => n.id),
      issues: state.issues.map((i) => i.id),
    })
  }
  note("opened")

  // Dropped on a connection: the new step goes in between.
  const split = s().edges.find((e) => e.source === "confirm").id
  const inserted = s().addStep("transform", { x: 400, y: 200 }, { edge: split })
  note("split-insert")

  // Off a branch, from the library or the picker.
  const delay = s().addStep("delay", { x: 100, y: 500 }, { source: "vendor", output: "error" })
  note("branch-add")

  s().connect(delay, "next", "declined")
  s().connect(delay, "next", "declined")
  note("connect")

  const copy = s().duplicateStep("assess")
  note("duplicate")

  // Typing in one field is one step of undo.
  s().updateStep(copy, (step) => ({ ...step, name: "Assess again" }), "name")
  clock += 400
  s().updateStep(copy, (step) => ({ ...step, name: "Assess it again" }), "name")
  clock += 5000
  s().updateStep(copy, (step) => ({ ...step, name: "Assess once more" }), "name")
  note("rename")

  s().undo()
  note("undo")
  s().redo()
  note("redo")

  // A case removed takes its connection with it.
  s().updateStep("route", (step) => ({
    ...step,
    config: { ...step.config, arms: step.config.arms.filter((a) => a.id !== "risky") },
  }))
  note("case-removed")

  s().edit(inserted)
  s().removeSteps([inserted, "window"])
  note("remove")

  s().updateMeta({ name: "Renamed" }, "name")
  note("meta")

  s().arrange(measure)
  note("arrange")

  s().replaceDocument({ ...doc, name: "Replaced", edges: doc.edges.slice(0, 3) })
  note("replace")
  s().undo()
  note("undo-replace")
  return record
}

test("the builder store edits a workflow as it always has", () => {
  const record = run()
  if (process.env.UPDATE_GOLDEN || !existsSync(GOLDEN)) {
    mkdirSync(new URL("./fixtures/", import.meta.url), { recursive: true })
    writeFileSync(GOLDEN, `${JSON.stringify(record, null, 2)}\n`)
  }
  const golden = JSON.parse(readFileSync(GOLDEN, "utf8"))
  assert.equal(record.length, golden.length)
  for (let i = 0; i < golden.length; i += 1) {
    assert.deepEqual(record[i], golden[i], `after ${golden[i].label}`)
  }
})
