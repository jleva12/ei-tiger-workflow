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
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const {
  FOLLOW_UPS_REQUEST_KEY,
  FOLLOW_UPS_STATE_KEY,
  followUpsOf,
  followUpsRequest,
} = await server.ssrLoadModule("/src/components/forge/assistant/follow-ups.ts")
await server.close()

test("a run asks for follow-ups only when they're on, with a count it may ask for", () => {
  assert.equal(followUpsRequest(undefined), undefined)
  assert.equal(followUpsRequest(false), undefined)
  assert.deepEqual(followUpsRequest(true), { [FOLLOW_UPS_REQUEST_KEY]: true })
  assert.deepEqual(followUpsRequest({ count: 2 }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 2 },
  })
  assert.deepEqual(followUpsRequest({ count: 12 }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 5 },
  })
})

test("the last reply's follow-ups are read from the session's state", () => {
  assert.deepEqual(
    followUpsOf({
      [FOLLOW_UPS_STATE_KEY]: { suggestions: ["Roll it back", "", 3] },
    }),
    ["Roll it back"]
  )
  assert.deepEqual(followUpsOf({ [FOLLOW_UPS_STATE_KEY]: null }), [])
  assert.deepEqual(followUpsOf({}), [])
})
