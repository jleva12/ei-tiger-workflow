import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import React from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { useExternalStoreRuntime } from "@assistant-ui/react"
import { adkExtras } from "../node_modules/@assistant-ui/react-google-adk/dist/adkExtras.js"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { AssistantProvider, AssistantThread } = await server.ssrLoadModule("/src/components/forge/assistant/assistant-provider.tsx")
const { createHistoryFeedback, registerHistoryFeedback } = await server.ssrLoadModule("/src/components/forge/assistant/history-feedback.ts")
await server.close()

async function render({ failed = false, refresh = false, listError = false } = {}) {
  const feedback = createHistoryFeedback()
  if (refresh) await feedback.wrapLoad(async () => ({ messages: [] }))("s1")
  if (failed) await feedback.wrapLoad(async () => { throw new Error("load: 503") })("s1").catch(() => {})
  let cleanup
  function Fixture() {
    const runtime = useExternalStoreRuntime({
      isRunning: false,
      messages: refresh ? [{ id: "reply", role: "assistant", createdAt: new Date(0), content: [{ type: "text", text: "Previously loaded reply" }], status: { type: "complete", reason: "stop" }, metadata: { custom: {}, unstable_annotations: [], unstable_data: [], unstable_state: null, steps: [] } }] : [],
      extras: adkExtras.provide({ stateDelta: {}, longRunningToolIds: [], artifactDelta: {}, authRequests: [], toolConfirmations: [], messageMetadata: {} }),
      onNew: async () => {},
      adapters: { threadList: { threadId: "s1", threads: [{ id: "s1", remoteId: "s1", externalId: "s1", status: "regular" }], onSwitchToThread: async () => {}, onSwitchToNewThread: async () => {} } },
    })
    if (listError) {
      const getState = runtime.threads.getState.bind(runtime.threads)
      runtime.threads.getState = () => ({ ...getState(), loadError: new Error("list: 401") })
    }
    cleanup = registerHistoryFeedback(runtime, feedback)
    return React.createElement(AssistantProvider, { runtime }, React.createElement(AssistantThread))
  }
  try { return renderToStaticMarkup(React.createElement(Fixture)) }
  finally { cleanup?.() }
}

test("an initial history failure renders retry and new-conversation actions instead of a blank welcome/composer", async () => {
  const html = await render({ failed: true })
  assert.match(html, /assistant-history-error/)
  assert.match(html, /Conversation couldn&#x27;t load/)
  assert.match(html, /Retry/)
  assert.match(html, /New conversation/)
  assert.doesNotMatch(html, /assistant-welcome|aui-composer-input/)
})

test("a failed refresh keeps loaded messages visible alongside retry feedback", async () => {
  const html = await render({ failed: true, refresh: true })
  assert.match(html, /Previously loaded reply/)
  assert.match(html, /assistant-history-error/)
})

test("a conversation-list failure is visible without relying on the sidebar", async () => {
  const html = await render({ listError: true })
  assert.match(html, /Conversation list couldn&#x27;t load/)
  assert.match(html, /Sign in again/)
  assert.match(html, /Retry/)
})
