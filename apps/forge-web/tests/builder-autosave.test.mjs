import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { runInNewContext } from "node:vm"
import ts from "typescript"

// Execute the actual hook with controlled effects, browser events, and saves.
// Delaying a committed response reproduces the unload/revision race without
// depending on network timing or a browser's confirmation-dialog policy.
const source = readFileSync(
  new URL("../src/features/builder/components/autosave.ts", import.meta.url),
  "utf8"
)
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

const settle = async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve()
}

function harness(save) {
  const effects = []
  const listeners = new Map()
  const subscribers = new Set()
  const timers = new Set()
  const states = []
  let state = {
    nodes: [{ text: "initial" }], edges: [],
    meta: { created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z" },
  }
  const api = {
    getState: () => state,
    setState: (patch) => {
      const previous = state
      state = { ...state, ...patch }
      for (const listener of subscribers) listener(state, previous)
    },
    subscribe: (listener) => {
      subscribers.add(listener)
      return () => subscribers.delete(listener)
    },
  }
  const documentOf = (value) => ({ ...value.meta, nodes: value.nodes, edges: value.edges })
  const exports = {}
  runInNewContext(compiled, {
    exports,
    require: (name) => {
      if (name === "react") return {
        useRef: (value) => ({ current: value }),
        useState: (value) => {
          const index = states.length
          states.push(typeof value === "function" ? value() : value)
          return [states[index], (next) => { states[index] = next }]
        },
        useEffect: (effect) => effects.push(effect),
        useMemo: (factory) => factory(),
      }
      if (name === "./store") return { documentOf, useBuilderApi: () => api }
      if (name === "@/lib/api/index") return { toApiError: (error) => error }
      throw new Error(`Unexpected import: ${name}`)
    },
    setTimeout: (callback) => { timers.add(callback); return callback },
    clearTimeout: (callback) => timers.delete(callback),
    window: {
      addEventListener: (name, handler) => listeners.set(name, handler),
      removeEventListener: (name) => listeners.delete(name),
    },
  })
  const initial = {
    id: "agent-1", revision: 1, updated_at: "2026-10-16T00:00:00Z",
    document: documentOf(state),
  }
  const controls = exports.useBuilderAutosave(initial, {
    save,
    cache: () => {},
    fetch: async () => initial,
  })
  const cleanups = effects.map((effect) => effect()).filter(Boolean)
  return {
    controls,
    edit: (text) => api.setState({ nodes: [{ text }] }),
    status: () => states[0],
    warns: () => {
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
      listeners.get("beforeunload")(event)
      return event.defaultPrevented
    },
    hide: () => listeners.get("pagehide")(),
    close: () => cleanups.forEach((cleanup) => cleanup()),
  }
}

function revisionStore() {
  const calls = []
  let revision = 1
  let persisted = "initial"
  let release
  return {
    calls,
    persisted: () => persisted,
    release: () => release(),
    save: async (id, body, options) => {
      calls.push({ revision: body.revision, text: body.document.nodes[0].text, ...options })
      if (body.revision !== revision) throw { status: 409, message: "Conflict" }
      revision += 1
      persisted = body.document.nodes[0].text
      const record = { id, revision, document: body.document, updated_at: "2026-10-16T01:00:00Z" }
      if (calls.length === 1) await new Promise((resolve) => { release = resolve })
      return record
    },
  }
}

test("unload warns and serializes newer edits behind a committed, unanswered save", async () => {
  const store = revisionStore()
  const app = harness(store.save)
  app.edit("first")
  app.controls.saveNow()
  assert.equal(store.persisted(), "first")
  app.edit("latest")
  assert.equal(app.warns(), true)
  app.hide()
  app.hide()
  assert.equal(store.calls.length, 1, "pagehide must not send the stale revision")
  store.release()
  await settle()
  assert.deepEqual(store.calls, [
    { revision: 1, text: "first", keepalive: false },
    { revision: 2, text: "latest", keepalive: true },
  ])
  assert.equal(store.persisted(), "latest")
  assert.equal(app.status().kind, "saved")
  assert.equal(app.warns(), false)
  app.close()
})

test("reverting during an in-flight save still warns and persists the reversion", async () => {
  const store = revisionStore()
  const app = harness(store.save)
  app.edit("first")
  app.controls.saveNow()
  app.edit("initial")
  assert.equal(app.warns(), true)
  app.hide()
  store.release()
  await settle()
  assert.equal(store.persisted(), "initial")
  assert.equal(store.calls[1].revision, 2)
  assert.equal(app.warns(), false)
  app.close()
})

test("debounced edits warn before unloading and pagehide can save an idle document", async () => {
  const calls = []
  const app = harness(async (id, body, options) => {
    calls.push(options)
    return { id, ...body, revision: 2, updated_at: "2026-10-16T01:00:00Z" }
  })
  assert.equal(app.warns(), false)
  app.edit("latest")
  assert.equal(app.warns(), true)
  app.hide()
  await settle()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].keepalive, true)
  assert.equal(app.warns(), false)
  app.close()
})

for (const status of [400, 403, 422]) {
  test(`a ${status} refusal stays dirty without automatic retries`, async () => {
    let calls = 0
    const app = harness(async (id, body) => {
      calls += 1
      if (calls === 1) throw { status, message: "Refused" }
      return { id, ...body, revision: 2, updated_at: "2026-10-16T01:00:00Z" }
    })
    app.edit("unsaved")
    app.controls.saveNow()
    await settle()
    assert.equal(app.status().kind, "refused")
    assert.equal(app.warns(), true)
    app.hide()
    await settle()
    assert.equal(calls, 1)
    app.controls.saveNow()
    await settle()
    assert.equal(calls, 2)
    assert.equal(app.warns(), false)
    app.close()
  })
}
