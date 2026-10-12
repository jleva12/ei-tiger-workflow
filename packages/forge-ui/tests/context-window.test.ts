import assert from "node:assert/strict"
import { test } from "node:test"
import {
  compactTokens,
  contextView,
  currentContext,
  parseAgentContext,
  percent,
  type ContextView,
} from "../src/components/forge/assistant/context-window.ts"

// The meter's numbers for an entry (tests only pass entries that parse).
const viewOf = (raw: unknown, model?: Parameters<typeof contextView>[1]) =>
  contextView(parseAgentContext(raw), model) as ContextView

// An agent's entry as the agent server keeps it (forge_common's ContextWindowPlugin).
const entry = (more: Record<string, unknown> = {}) => ({
  tokens: 152_340,
  limit: 272_000,
  threshold: 217_600,
  window: 400_000,
  reply: 128_000,
  share: 0.8,
  auto: true,
  counted: "provider",
  model: "openai/gpt-5.2",
  updated: 1_000,
  sent_chars: 600_000,
  ...more,
})

const gpt = {
  id: "openai/gpt-5.2",
  name: "GPT-5.2",
  contextWindow: 400_000,
  maxTokens: 128_000,
}
const mini = {
  id: "openai/gpt-5-mini",
  name: "GPT-5 mini",
  contextWindow: 128_000,
  maxTokens: 16_000,
}

test("an entry is read as the server wrote it, and anything else isn't one", () => {
  const parsed = parseAgentContext(
    entry({
      compactions: 2,
      last_compaction: {
        at: 990,
        tokens_before: 230_000,
        tokens_after: 41_000,
      },
    })
  )
  assert.ok(parsed)
  assert.equal(parsed.tokens, 152_340)
  assert.equal(parsed.threshold, 217_600)
  assert.equal(parsed.counted, "provider")
  assert.deepEqual(parsed.lastCompaction, {
    at: 990,
    tokensBefore: 230_000,
    tokensAfter: 41_000,
  })
  assert.equal(parseAgentContext({ tokens: "lots", limit: 1 }), undefined)
  assert.equal(parseAgentContext({ tokens: 1, limit: 0 }), undefined)
  assert.equal(parseAgentContext(null), undefined)
})

test("the meter shows the agent answering, else the one counted last", () => {
  const state = {
    "forge:context:supervisor": entry({ tokens: 10, updated: 5 }),
    "forge:context:researcher": entry({ tokens: 20, updated: 9 }),
    "forge:summaries:researcher": [
      { part: "turn", start: 1, end: 2, text: "…" },
    ],
    model: "openai/gpt-5.2",
  }
  assert.equal(currentContext(state, "supervisor")?.context.tokens, 10)
  assert.equal(currentContext(state)?.agent, "researcher")
  assert.equal(currentContext(state, "nobody")?.agent, "researcher")
  assert.equal(currentContext({ model: "x" }), undefined)
})

test("the meter stays on the conversation's agent while a task works apart", () => {
  const state = {
    "forge:context:forge": entry({ tokens: 3_400, updated: 5 }),
    "forge:context:code": entry({ tokens: 67_800, updated: 9, apart: true }),
  }
  // The task is answering, and counted last: the conversation's agent shows.
  assert.equal(currentContext(state, "code")?.agent, "forge")
  assert.equal(currentContext(state)?.context.tokens, 3_400)
  assert.equal(parseAgentContext(state["forge:context:code"])?.apart, true)
  // Before the conversation's agent is counted, the task is all there is.
  const alone = { "forge:context:code": state["forge:context:code"] }
  assert.equal(currentContext(alone, "code")?.agent, "code")
})

test("used, left and the compaction point, as the server counted them", () => {
  const view = viewOf(entry(), gpt)
  assert.equal(view.used, 152_340)
  assert.equal(view.limit, 272_000)
  assert.equal(view.threshold, 217_600)
  assert.equal(view.left, 119_660)
  assert.equal(view.untilCompaction, 65_260)
  assert.equal(view.phase, "clear")
  assert.equal(view.modelChanged, false)
  // The model that answered, by its name when it's also the one chosen.
  assert.equal(view.model, "GPT-5.2")
  assert.equal(viewOf(entry()).model, "openai/gpt-5.2")
  assert.equal(percent(view.usedShare), "56%")
  assert.equal(percent(view.thresholdShare ?? 0), "80%")
})

test("phases: near within a tenth of compaction, then compacting, then full", () => {
  const at = (tokens: number) => viewOf(entry({ tokens })).phase
  assert.equal(at(190_000), "clear")
  assert.equal(at(200_000), "near")
  assert.equal(at(217_600), "compacting")
  assert.equal(at(272_001), "full")
})

test("a model chosen in the composer sets the window the next request runs on", () => {
  const view = viewOf(entry(), mini)
  assert.equal(view.modelChanged, true)
  assert.equal(view.limit, 112_000)
  // The server compacts at the same share of the new limit.
  assert.equal(view.threshold, 89_600)
  assert.equal(view.phase, "full")
  assert.equal(view.model, "GPT-5 mini")
})

test("before the first message the chosen model's window shows, all of it left", () => {
  const view = contextView(undefined, gpt) as ContextView
  assert.equal(view.empty, true)
  assert.equal(view.used, 0)
  assert.equal(view.left, 272_000)
  assert.equal(view.threshold, undefined)
  assert.equal(contextView(undefined, { id: "x" }), undefined)
  assert.equal(contextView(undefined), undefined)
})

test("an estimate, compaction turned off and an asked-for compaction say so", () => {
  const view = viewOf(
    entry({ counted: "estimate", auto: false, requested: true })
  )
  assert.equal(view.estimated, true)
  assert.equal(view.auto, false)
  assert.equal(view.requested, true)
})

test("token counts read short", () => {
  assert.equal(compactTokens(950), "950")
  assert.equal(compactTokens(152_340), "152.3K")
  assert.equal(compactTokens(1_200_000), "1.2M")
  assert.equal(percent(0), "0%")
  assert.equal(percent(0.001), "1%")
})
