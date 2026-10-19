import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import React from "react"
import { renderToStaticMarkup } from "react-dom/server"
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
} from "@assistant-ui/react"
import { adkExtras } from "../node_modules/@assistant-ui/react-google-adk/dist/adkExtras.js"
import { createServer } from "vite"
import { CLIENT_TURN_STATE_KEY } from "../src/components/forge/assistant/turn-lifecycle.ts"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { Thread } = await server.ssrLoadModule(
  "/src/components/assistant-ui/elements/thread.aui.tsx"
)
const { AdkTurnStatus } = await server.ssrLoadModule(
  "/src/components/forge/assistant/turn-status.tsx"
)
await server.close()

function Fixture({
  running = false,
  status = { type: "complete", reason: "stop" },
  outcome,
}) {
  const runtime = useExternalStoreRuntime({
    isRunning: running,
    messages: [
      {
        id: "reply",
        role: "assistant",
        createdAt: new Date(0),
        content: [{ type: "text", text: "Retained reply text" }],
        status,
        metadata: {
          custom: {},
          unstable_annotations: [],
          unstable_data: [],
          unstable_state: null,
          steps: [],
        },
      },
    ],
    extras: adkExtras.provide({
      stateDelta: { [CLIENT_TURN_STATE_KEY]: outcome },
    }),
    onNew: async () => {},
  })
  return React.createElement(
    AssistantRuntimeProvider,
    { runtime },
    React.createElement(Thread, {
      autoFocus: false,
      components: { RunStatus: AdkTurnStatus },
    })
  )
}
const render = (props) =>
  renderToStaticMarkup(React.createElement(Fixture, props))

test("working remains visible after a child message finishes until the whole run settles", () => {
  assert.match(render({ running: true }), /data-slot="aui_thread-working"/)
  assert.doesNotMatch(
    render({ running: false }),
    /data-slot="aui_thread-working"/
  )
})

test("a failed invocation is visibly failed even if the SDK merged its text into a complete message", () => {
  const html = render({
    outcome: { status: "failed", errorMessage: "The model is unavailable." },
  })
  assert.match(html, /Retained reply text/)
  assert.match(html, /role="alert" data-slot="assistant-turn-error"/)
  assert.match(html, /The model is unavailable/)
  assert.doesNotMatch(html, /data-slot="aui_thread-working"/)
})

test("historical output-limit and safety stops have an explanation without local lifecycle state", () => {
  assert.match(
    render({ status: { type: "incomplete", reason: "length" } }),
    /output limit before it finished/
  )
  assert.match(
    render({ status: { type: "incomplete", reason: "content-filter" } }),
    /safety filters stopped this reply/
  )
})

test("server cancellation is visible and settled while an intentional Stop has no failure banner", () => {
  assert.match(
    render({
      outcome: { status: "cancelled", errorMessage: "The reply was stopped." },
    }),
    /Reply stopped/
  )
  assert.doesNotMatch(
    render({ status: { type: "incomplete", reason: "cancelled" } }),
    /assistant-turn-error/
  )
})
