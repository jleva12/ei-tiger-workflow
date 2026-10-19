import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { TITLE_KEY, titleOf, withSessionTitles } = await server.ssrLoadModule(
  "/src/components/forge/assistant/session-titles.ts"
)
await server.close()

test("a session's title is in its state, when the server kept one", () => {
  assert.equal(
    titleOf({ id: "s1", state: { [TITLE_KEY]: " Ship A-1042 " } }),
    "Ship A-1042"
  )
  assert.equal(titleOf({ id: "s1", state: {} }), undefined)
  assert.equal(titleOf({ id: "s1", state: { [TITLE_KEY]: "  " } }), undefined)
  assert.equal(titleOf({ id: "s1" }), undefined)
  assert.equal(titleOf(null), undefined)
})

test("conversations are listed and fetched with their titles", async () => {
  const asked = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    asked.push({ url, headers: init?.headers })
    if (url.endsWith("/sessions"))
      return Response.json([
        { id: "s2", state: { [TITLE_KEY]: "Where are the stores?" } },
        { id: "s1", state: {} },
      ])
    return Response.json({
      id: "s2",
      state: { [TITLE_KEY]: "Where are the stores?" },
      events: [],
    })
  }
  const kept = { rename: async () => {}, delete: async () => {} }
  try {
    const adapter = withSessionTitles(kept, {
      url: "https://forge.example/runtime/pages/cp_x/",
      appName: "ca_x",
      userId: "visitor_1",
      headers: () => ({ "X-Test": "1" }),
    })
    const { threads } = await adapter.list()
    assert.deepEqual(threads, [
      {
        status: "regular",
        remoteId: "s2",
        externalId: "s2",
        title: "Where are the stores?",
      },
      { status: "regular", remoteId: "s1", externalId: "s1", title: undefined },
    ])
    assert.equal(
      asked[0].url,
      "https://forge.example/runtime/pages/cp_x/apps/ca_x/users/visitor_1/sessions"
    )
    assert.deepEqual(asked[0].headers, { "X-Test": "1" })
    const one = await adapter.fetch("s2")
    assert.equal(one.title, "Where are the stores?")
    // What it doesn't change is the package's own.
    assert.equal(adapter.rename, kept.rename)
    assert.equal(adapter.delete, kept.delete)
  } finally {
    globalThis.fetch = original
  }
})


test("a malformed list or wrong conversation fails instead of silently dropping history", async () => {
  const original = globalThis.fetch
  let response = [{ id: "s1" }, { state: {} }]
  globalThis.fetch = async () => Response.json(response)
  try {
    const adapter = withSessionTitles({}, { url: "https://forge.example", appName: "app", userId: "user", headers: () => ({}) })
    await assert.rejects(adapter.list(), /no ID/)
    response = { id: "someone-else", events: [] }
    await assert.rejects(adapter.fetch("s1"), /Invalid ADK session fetch/)
  } finally {
    globalThis.fetch = original
  }
})
