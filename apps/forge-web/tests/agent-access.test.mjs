import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { after, test } from "node:test"
import { createServer } from "vite"

// Who may use an agent on the canvas: the rule on its node, read from a
// document, said in words as the runtime's refusals say it, and edited as
// a draft of nested groups.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
})
after(() => server.close())
const access = await server.ssrLoadModule("/src/features/agents/lib/access.ts")

const NAMES = { 1: "invoices:approve", 2: "finance:read", 3: "finance:write" }
const name = (id) => NAMES[id]
const BILLING = {
  any: [{ permission: 1 }, { all: [{ permission: 2 }, { permission: 3 }] }],
}

test("a rule is read from what a document holds, or refused", () => {
  assert.deepEqual(access.readAccessRule(BILLING), BILLING)
  assert.equal(access.readAccessRule(null), null)
  assert.equal(access.readAccessRule(undefined), null)
  for (const wrong of [
    { any: [] },
    { permission: 0 },
    { permission: "1" },
    { none: [{ permission: 1 }] },
    { any: [{ permission: 1 }], all: [{ permission: 2 }] },
    { any: [{ all: [{ any: [{ all: [{ permission: 1 }] }] }] }] },
    { any: Array.from({ length: 51 }, (_, i) => ({ permission: i + 1 })) },
  ])
    assert.equal(access.readAccessRule(wrong), undefined, JSON.stringify(wrong))
})

test("a rule is said as the runtime says it", () => {
  assert.equal(
    access.ruleWords(BILLING, name),
    "invoices:approve or both finance:read and finance:write"
  )
  assert.equal(
    access.ruleWords(
      { all: [{ permission: 1 }, { permission: 2 }, { permission: 9 }] },
      name
    ),
    "invoices:approve, finance:read, and a deleted permission (#9)"
  )
  assert.deepEqual(access.rulePermissions(BILLING), [1, 2, 3])
})

test("the editor's draft says the same rule, leaving out what isn't picked", () => {
  const draft = access.toDraft(BILLING)
  assert.equal(draft.type, "group")
  assert.equal(access.draftDepth(draft), 2)
  assert.deepEqual(access.fromDraft(draft), BILLING)

  const empty = { ...access.emptyGroup("all"), items: [] }
  assert.equal(access.fromDraft(empty), null)
  const unpicked = {
    ...access.emptyGroup("any"),
    items: [
      { id: "a", type: "permission", permission: null },
      { ...access.emptyGroup("all"), items: [] },
      { id: "b", type: "permission", permission: 2 },
    ],
  }
  assert.deepEqual(access.fromDraft(unpicked), { any: [{ permission: 2 }] })
  // One permission alone is a group of it, as the editor shows it.
  assert.deepEqual(access.fromDraft(access.toDraft({ permission: 3 })), {
    any: [{ permission: 3 }],
  })
  assert.equal(access.fromDraft(access.toDraft(null)), null)
})
