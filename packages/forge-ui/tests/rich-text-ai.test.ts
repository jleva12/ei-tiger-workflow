import assert from "node:assert/strict"
import { describe, test } from "node:test"

import type {
  AIRequest,
  AIStreamEvent,
  EditorAIConfig,
} from "../src/components/forge/rich-text-editor/ai/ai-types.ts"
import { makeHarness, positionOf, tick } from "./rich-text-fixtures.ts"

/** A stream the test feeds by hand, recording whether it was closed early. */
function manualStream() {
  const queue: AIStreamEvent[] = []
  let wake: (() => void) | null = null
  const state = { closed: false, done: false }
  const stream: AsyncIterable<AIStreamEvent> = {
    [Symbol.asyncIterator]() {
      return {
        next: async () => {
          while (!queue.length && !state.done) {
            await new Promise<void>((resolve) => (wake = resolve))
          }
          const value = queue.shift()
          return value
            ? { value, done: false }
            : { value: undefined, done: true }
        },
        return: async () => {
          state.closed = true
          return { value: undefined, done: true }
        },
      }
    },
  }
  return {
    stream,
    state,
    push(event: AIStreamEvent) {
      queue.push(event)
      wake?.()
    },
    end() {
      state.done = true
      wake?.()
    },
  }
}

function select(harness: ReturnType<typeof makeHarness>, text: string) {
  const from = positionOf(harness.editor, text)
  harness.controller.setSelection({ from, to: from + text.length })
}

describe("AI actions", () => {
  test("a Promise result waits for approval, then replaces the selection", async () => {
    const requests: AIRequest[] = []
    const ai: EditorAIConfig = {
      enabled: true,
      execute: async (request) => {
        requests.push(request)
        return { markdown: "**fresh** words" }
      },
    }
    const harness = makeHarness("Some stale words here", { ai })
    select(harness, "stale words")
    await harness.ai.start("ai.rewrite")

    assert.equal(requests[0].actionId, "rewrite")
    assert.equal(requests[0].selectedText, "stale words")
    assert.equal(requests[0].documentMarkdown, "Some stale words here")
    assert.match(requests[0].instruction ?? "", /Rewrite/)
    assert.equal(harness.ai.getState().status, "ready")
    assert.equal(
      harness.controller.getMarkdown(),
      "Some stale words here",
      "untouched until accepted"
    )

    assert.equal(harness.ai.accept(), true)
    assert.equal(harness.controller.getMarkdown(), "Some **fresh** words here")
    assert.equal(harness.ai.getState().status, "idle")
  })

  test("rejecting leaves the document alone", async () => {
    const harness = makeHarness("Keep me", {
      ai: { enabled: true, execute: async () => ({ markdown: "Replaced" }) },
    })
    select(harness, "Keep me")
    await harness.ai.start("rewrite")
    harness.ai.reject()
    assert.equal(harness.controller.getMarkdown(), "Keep me")
    assert.equal(harness.ai.getState().status, "idle")
  })

  test("a stream fills the buffer and completes", async () => {
    const feed = manualStream()
    const harness = makeHarness("Start.", {
      ai: { enabled: true, execute: () => feed.stream },
    })
    harness.controller.setSelection({ from: 7, to: 7 })
    const run = harness.ai.start("continue")
    await tick()
    assert.equal(harness.ai.getState().status, "running")
    feed.push({ type: "delta", text: "More " })
    feed.push({ type: "delta", text: "text." })
    await new Promise((resolve) => setTimeout(resolve, 80))
    assert.equal(harness.ai.getState().markdown, "More text.")
    assert.equal(
      harness.controller.getMarkdown(),
      "Start.",
      "streaming never edits the document"
    )
    feed.push({ type: "complete" })
    await run
    assert.equal(harness.ai.getState().status, "ready")
    harness.ai.accept()
    assert.equal(harness.controller.getMarkdown(), "Start.More text.")
  })

  test("cancelling closes the stream and changes nothing", async () => {
    const feed = manualStream()
    const signals: AbortSignal[] = []
    const harness = makeHarness("Text", {
      ai: {
        enabled: true,
        execute: (_request, { signal }) => {
          signals.push(signal)
          return feed.stream
        },
      },
    })
    const run = harness.ai.start("continue")
    await tick()
    feed.push({ type: "delta", text: "partial" })
    await tick()
    harness.ai.cancel()
    feed.push({ type: "delta", text: " more" })
    await run
    assert.equal(signals[0].aborted, true)
    assert.equal(feed.state.closed, true)
    assert.equal(harness.ai.getState().status, "idle")
    assert.equal(harness.controller.getMarkdown(), "Text")
  })

  test("an error event fails the run and reports it", async () => {
    const feed = manualStream()
    const harness = makeHarness("Text", {
      ai: { enabled: true, execute: () => feed.stream },
    })
    const run = harness.ai.start("continue")
    await tick()
    feed.push({ type: "error", message: "Rate limited" })
    await run
    assert.equal(harness.ai.getState().status, "error")
    assert.equal(harness.ai.getState().error, "Rate limited")
    assert.equal(harness.errors.at(-1)?.code, "ai-failed")
  })

  test("a thrown error is reported the same way", async () => {
    const harness = makeHarness("Text", {
      ai: {
        enabled: true,
        execute: async () => {
          throw new Error("AI request failed")
        },
      },
    })
    await harness.ai.start("continue")
    assert.equal(harness.ai.getState().error, "AI request failed")
  })

  test("without approval the result goes straight in", async () => {
    const harness = makeHarness("Hello", {
      ai: {
        enabled: true,
        requireApproval: false,
        execute: async () => ({ markdown: "Goodbye" }),
      },
    })
    select(harness, "Hello")
    await harness.ai.start("rewrite")
    assert.equal(harness.controller.getMarkdown(), "Goodbye")
  })

  test("edits before the target don't stop it", async () => {
    let finish: (markdown: string) => void = () => {}
    const harness = makeHarness("Intro. Target text.", {
      ai: {
        enabled: true,
        execute: () =>
          new Promise(
            (resolve) => (finish = (markdown) => resolve({ markdown }))
          ),
      },
    })
    select(harness, "Target text.")
    const run = harness.ai.start("rewrite")
    await tick()
    harness.controller.replaceRange({ from: 1, to: 1 }, "Edited ")
    finish("New text.")
    await run
    assert.equal(harness.ai.accept(), true)
    assert.equal(harness.controller.getMarkdown(), "Edited Intro. New text.")
  })

  test("a changed target becomes a conflict, resolved at the caret", async () => {
    let finish: (markdown: string) => void = () => {}
    const harness = makeHarness("Keep this. Target text.", {
      ai: {
        enabled: true,
        execute: () =>
          new Promise(
            (resolve) => (finish = (markdown) => resolve({ markdown }))
          ),
      },
    })
    select(harness, "Target text.")
    const run = harness.ai.start("rewrite")
    await tick()
    // Someone edits the very text the AI is rewriting.
    const from = positionOf(harness.editor, "Target")
    harness.controller.replaceRange({ from, to: from + 6 }, "Changed")
    finish("Rewritten.")
    await run

    assert.equal(harness.ai.accept(), false)
    assert.equal(harness.ai.getState().status, "conflict")
    assert.equal(harness.errors.at(-1)?.code, "ai-target-changed")
    assert.equal(
      harness.controller.getMarkdown(),
      "Keep this. Changed text.",
      "nothing overwritten"
    )

    harness.controller.setSelection({ from: 1, to: 1 })
    assert.equal(harness.ai.accept("cursor"), true)
    assert.equal(
      harness.controller.getMarkdown(),
      "Rewritten.Keep this. Changed text."
    )
  })

  test("insert mode adds its blocks after the selection's paragraph", async () => {
    const harness = makeHarness("First paragraph.\n\nSecond.", {
      ai: {
        enabled: true,
        execute: async () => ({ markdown: "More detail." }),
      },
    })
    select(harness, "First")
    await harness.ai.start("expand")
    harness.ai.accept()
    assert.equal(
      harness.controller.getMarkdown(),
      "First paragraph.\n\nMore detail.\n\nSecond."
    )
  })

  test("preview mode lets the person choose", async () => {
    const harness = makeHarness("Long text.", {
      ai: { enabled: true, execute: async () => ({ markdown: "Short." }) },
    })
    await harness.ai.start("summarize")
    assert.equal(harness.ai.getState().mode, "preview")
    harness.ai.accept("append")
    assert.equal(harness.controller.getMarkdown(), "Long text.\n\nShort.")
  })

  test("actions that need a selection refuse without one", async () => {
    let calls = 0
    const harness = makeHarness("Text", {
      ai: {
        enabled: true,
        execute: async () => ((calls += 1), { markdown: "x" }),
      },
    })
    await harness.ai.start("rewrite")
    assert.equal(calls, 0)
    assert.equal(harness.errors.at(-1)?.code, "action-failed")
  })

  test("Ask AI sends the typed instruction for the selection it opened on", async () => {
    const requests: AIRequest[] = []
    const harness = makeHarness("Alpha beta", {
      ai: {
        enabled: true,
        context: (actionId) => ({ actionId, tone: "formal" }),
        execute: async (request) => (
          requests.push(request),
          { markdown: "Gamma" }
        ),
      },
    })
    select(harness, "beta")
    harness.ai.openPrompt()
    harness.controller.setSelection({ from: 1, to: 1 })
    await harness.ai.start("ask", { instruction: "Translate to Greek" })
    assert.equal(requests[0].instruction, "Translate to Greek")
    assert.equal(requests[0].selectedText, "beta")
    assert.deepEqual(requests[0].context, { actionId: "ask", tone: "formal" })
    harness.ai.accept()
    assert.equal(harness.controller.getMarkdown(), "Alpha Gamma")
  })

  test("an action that asks sends what's typed after its own prompt", async () => {
    const requests: AIRequest[] = []
    const harness = makeHarness("Keep it short.", {
      ai: {
        enabled: true,
        allowCustomPrompts: false,
        actions: [
          {
            id: "edit",
            label: "Rewrite with AI…",
            prompt: "Rewrite the selected passage.",
            requiresSelection: true,
            resultMode: "replace",
            askInstruction: true,
          },
        ],
        execute: async (request) => (
          requests.push(request),
          { markdown: "Keep it **brief**." }
        ),
      },
    })
    select(harness, "Keep it short.")
    harness.ai.openPrompt(undefined, "edit")
    assert.equal(harness.ai.getState().status, "prompt")
    assert.equal(harness.ai.getState().action?.id, "edit")
    await harness.ai.submitPrompt("Use bold for the key word")
    assert.equal(requests[0].actionId, "edit")
    assert.equal(
      requests[0].instruction,
      "Rewrite the selected passage.\n\nUse bold for the key word"
    )
    // The panel shows what the person typed, not the action's own prompt.
    assert.equal(harness.ai.getState().instruction, "Use bold for the key word")
    harness.ai.accept()
    assert.equal(harness.controller.getMarkdown(), "Keep it **brief**.")
  })

  test("an empty prompt runs nothing", async () => {
    let calls = 0
    const harness = makeHarness("Text", {
      ai: {
        enabled: true,
        execute: async () => ((calls += 1), { markdown: "x" }),
      },
    })
    harness.ai.openPrompt()
    await harness.ai.submitPrompt("   ")
    assert.equal(calls, 0)
    assert.equal(harness.ai.getState().status, "prompt")
  })

  test("regenerate asks again", async () => {
    let calls = 0
    const harness = makeHarness("Text", {
      ai: {
        enabled: true,
        execute: async () => ({ markdown: `Try ${(calls += 1)}` }),
      },
    })
    await harness.ai.start("continue")
    await harness.ai.regenerate()
    assert.equal(harness.ai.getState().markdown, "Try 2")
  })
})
