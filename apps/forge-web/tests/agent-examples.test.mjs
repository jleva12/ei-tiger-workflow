import assert from "node:assert/strict"
import { readdirSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// The example workflows (examples/workflows at the repository's root) import
// into the builder as they are: nothing dropped, nothing to fix.

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
const { parseAgent, toGraph } = await load(
  "/src/features/adk-workflows/lib/document.ts"
)
const { validateAgent } = await load(
  "/src/features/adk-workflows/lib/validate.ts"
)
const { agentTypes } = await load("/src/features/adk-workflows/lib/scope.ts")
const { resolveExpression } = await load(
  "/src/features/steps/lib/expressions.ts"
)
const { typeLabel } = await load("/src/features/steps/lib/types.ts")
await server.close()

const EXAMPLES = new URL("../../../examples/workflows/", import.meta.url)
const examples = readdirSync(EXAMPLES).filter((name) =>
  name.endsWith(".agent.json")
)

for (const name of examples) {
  test(`${name} imports with nothing dropped and nothing to fix`, () => {
    const text = readFileSync(new URL(name, EXAMPLES), "utf8")
    const read = parseAgent(text, { organizationId: "org-1" })
    assert.equal(read.ok, true)
    assert.deepEqual(read.notes, [])
    assert.deepEqual(
      validateAgent(toGraph(read.doc), {}).map((i) => `${i.id}: ${i.message}`),
      []
    )
  })
}

test("the repository change scout's merge reads each Pi agent's typed findings", () => {
  const text = readFileSync(
    new URL("repo-change-scout.agent.json", EXAMPLES),
    "utf8"
  )
  const graph = toGraph(parseAgent(text, { organizationId: "org-1" }).doc)
  const scopeOf = agentTypes(graph).scopeOf
  const type = (expression, at) =>
    typeLabel(resolveExpression(expression, scopeOf(at)))
  // In the loop's body: the clone's folder, and the Pi agent's answer, held to its schema.
  assert.equal(type("steps.clone.output.cwd", "explore"), "string")
  assert.equal(type("previous.answer.needs_change", "found"), "boolean")
  assert.equal(type("previous.answer.changes[0].file", "found"), "string")
  // After it: every repository's findings.
  assert.equal(type("previous.count", "merge"), "integer")
})
