import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// Workflows with versions, and outside callers: which version a run ran
// and how it started, what an example call sends, where each version
// picker comes from, and where the runtime is.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const load = (path) => server.ssrLoadModule(path)
const runs = await load("/src/features/runs/lib/runs.ts")
const { sampleInput } = await load("/src/features/adk-workflows/lib/samples.ts")
const { versionChoices } = await load("/src/features/adk-workflows/lib/api.ts")
const { expiryOf } = await load("/src/features/api-keys/lib/api.ts")
const { readSettings } = await load("/src/features/agents/lib/document.ts")
await server.close()

test("a run says which version it ran", () => {
  assert.equal(runs.versionLabel({ version: 3, revision: 9 }), "v3")
  assert.equal(runs.versionLabel({ version: "draft", revision: 17 }), "Draft · revision 17")
  // From before workflows had versions.
  assert.equal(runs.versionLabel({ version: null, revision: 4 }), "Revision 4")
})

test("a run says how it started", () => {
  assert.equal(runs.triggerLabel({ type: "runtime", protocol: "a2a" }), "Over A2A")
  assert.equal(runs.triggerLabel({ type: "runtime", protocol: "rest" }), "Over the API")
  assert.equal(runs.triggerLabel({ type: "manual", by: "u" }), "From the console")
  assert.equal(runs.triggerLabel({ type: "chat_agent" }), "By a chat agent")
  assert.equal(runs.triggerLabel(null), undefined)
})

test("an example call sends an input of the start's shape", () => {
  const schema = {
    type: "object",
    properties: {
      key: { type: "string" },
      count: { type: "integer" },
      tier: { type: "string", enum: ["free", "pro"] },
      tags: { type: "array" },
      urgent: { type: "boolean" },
    },
  }
  assert.deepEqual(sampleInput(schema), {
    key: "…",
    count: 0,
    tier: "free",
    tags: [],
    urgent: false,
  })
  assert.deepEqual(sampleInput({}), {})
})

test("version pickers offer each workflow's versions", () => {
  const choices = versionChoices([
    { id: "ag_a", published_version: 2, has_draft: true, draft_version: 3 },
    { id: "ag_b", published_version: null, has_draft: true, draft_version: 1 },
  ])
  assert.deepEqual(choices.ag_a, { published: 2, hasDraft: true, draftVersion: 3 })
  assert.deepEqual(choices.ag_b, { published: null, hasDraft: true, draftVersion: 1 })
})

test("a workflow tool keeps the version it pins", () => {
  const notes = []
  assert.deepEqual(
    readSettings("adk_workflow", { workflow: "ag_a", version: 3 }, "config", notes),
    { workflow: "ag_a", version: 3 }
  )
  // Saved before versions: the latest published.
  assert.deepEqual(readSettings("adk_workflow", { workflow: "ag_a" }, "config", notes), {
    workflow: "ag_a",
    version: null,
  })
  assert.deepEqual(notes, [])
  readSettings("adk_workflow", { workflow: "ag_a", version: "latest" }, "config", notes)
  assert.equal(notes.length, 1)
})

test("a key expires as picked, or never", () => {
  const now = new Date("2026-10-05T12:00:00Z")
  assert.equal(expiryOf("30", now), "2026-11-04T12:00:00.000Z")
  assert.equal(expiryOf("never", now), null)
})
