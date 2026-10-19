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
const { isPresentEcho } = await server.ssrLoadModule(
  "/src/components/forge/assistant/present-echo.ts"
)
await server.close()

test("present's answer or composition quoted back as a reply is an echo", () => {
  // As a model wrote it: wrapped in braces of its own.
  assert.equal(
    isPresentEcho(
      '{{"shown":false,"problems":["The Card \\"Acme — Catering\\"\'s cancel button does nothing: give it an _action {x}."]}}'
    ),
    true
  )
  assert.equal(isPresentEcho('{"shown": true}'), true)
  // Or the composition it sent, drawn above it already.
  assert.equal(
    isPresentEcho(
      '{"_type":"Card","title":"Catering","asForm":true,"children":[{"_type":"Input","name":"guests"}]}'
    ),
    true
  )
  assert.equal(isPresentEcho('  ```json\n{"shown": true}\n```  '), true)
  // While it streams in.
  assert.equal(isPresentEcho('{"shown": false, "problems": ["The ro'), true)
})

test("a reply that only mentions it, or says more after it, is shown", () => {
  assert.equal(isPresentEcho("Here are the details you asked for."), false)
  assert.equal(isPresentEcho('The tool said {"shown": true}.'), false)
  assert.equal(isPresentEcho('{"shown": true} Pick a date above.'), false)
  assert.equal(isPresentEcho('{"total": 3}'), false)
  assert.equal(isPresentEcho(""), false)
})
