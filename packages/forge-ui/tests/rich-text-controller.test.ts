import assert from "node:assert/strict"
import { describe, test } from "node:test"

import { createActionContext } from "../src/components/forge/rich-text-editor/core/action-registry.ts"
import { createBuiltInActions } from "../src/components/forge/rich-text-editor/core/builtin-actions.ts"
import { resolveFeatures } from "../src/components/forge/rich-text-editor/core/features.ts"
import { MarkdownSync } from "../src/components/forge/rich-text-editor/core/markdown-sync.ts"
import type {
  EditorAction,
  EditorUiHandlers,
} from "../src/components/forge/rich-text-editor/types.ts"
import {
  makeEditor,
  makeHarness,
  positionAfter,
  positionOf,
  tick,
} from "./rich-text-fixtures.ts"

describe("content methods", () => {
  test("insertMarkdown joins a one-paragraph answer to the line", () => {
    const { editor, controller } = makeHarness("Hello world")
    const at = positionAfter(editor, "Hello ")
    controller.setSelection({ from: at, to: at })
    assert.equal(controller.insertMarkdown("**big** "), true)
    assert.equal(controller.getMarkdown(), "Hello **big** world")
  })

  test("replaceSelection swaps the selected text", () => {
    const { editor, controller } = makeHarness("Hello world")
    const from = positionOf(editor, "world")
    controller.setSelection({ from, to: from + 5 })
    controller.replaceSelection("*there*")
    assert.equal(controller.getMarkdown(), "Hello *there*")
  })

  test("several blocks split the paragraph they land in", () => {
    const { editor, controller } = makeHarness("Before after")
    const at = positionAfter(editor, "Before ")
    controller.setSelection({ from: at, to: at })
    controller.insertMarkdown("## Inserted\n\nA paragraph")
    assert.match(controller.getMarkdown(), /## Inserted\n\nA paragraph/)
    assert.match(controller.getMarkdown(), /^Before/)
  })

  test("replaceRange uses document positions", () => {
    const { editor, controller } = makeHarness("# Title\n\nBody text")
    const from = positionOf(editor, "Body")
    controller.replaceRange({ from, to: from + 4 }, "New")
    assert.equal(controller.getMarkdown(), "# Title\n\nNew text")
  })

  test("appendMarkdown fills an empty editor's empty paragraph", () => {
    const { controller } = makeHarness("")
    controller.appendMarkdown("- one\n- two")
    assert.equal(controller.getMarkdown(), "- one\n- two")
  })

  test("appendMarkdown adds blocks at the end", () => {
    const { controller } = makeHarness("First")
    controller.appendMarkdown("Second")
    assert.equal(controller.getMarkdown(), "First\n\nSecond")
  })

  test("setMarkdown and clear replace everything", () => {
    const { controller } = makeHarness("Old")
    controller.setMarkdown("# New")
    assert.equal(controller.getMarkdown(), "# New")
    controller.clear()
    assert.equal(controller.getMarkdown(), "")
    assert.equal(controller.isEmpty(), true)
  })

  test("a range outside the document is refused", () => {
    const { controller, errors } = makeHarness("Short")
    assert.equal(controller.replaceRange({ from: 2, to: 500 }, "x"), false)
    assert.equal(errors[0]?.code, "invalid-range")
    assert.equal(controller.getMarkdown(), "Short")
  })

  test("unsafe URLs in inserted Markdown are removed and reported", () => {
    const { controller, errors } = makeHarness("")
    controller.insertMarkdown("[x](javascript:alert(1))")
    assert.equal(controller.getMarkdown(), "x")
    assert.equal(errors[0]?.code, "unsafe-content")
  })

  test("the initial content is cleaned too", () => {
    const editor = makeEditor("[x](javascript:alert(1)) and ![y](javascript:y)")
    assert.equal(JSON.stringify(editor.getJSON()).includes("javascript"), false)
  })
})

describe("read-only", () => {
  test("rejects editing", () => {
    const { editor, controller, errors } = makeHarness("Locked")
    editor.setEditable(false)
    assert.equal(controller.insertMarkdown("more"), false)
    assert.equal(controller.setMarkdown("gone"), false)
    assert.equal(controller.getMarkdown(), "Locked")
    assert.equal(errors[0]?.code, "read-only")
  })

  test("ignoreReadOnly lets the app write anyway", () => {
    const { editor, controller } = makeHarness("Locked")
    editor.setEditable(false)
    assert.equal(
      controller.appendMarkdown("Note", { ignoreReadOnly: true }),
      true
    )
    assert.equal(controller.getMarkdown(), "Locked\n\nNote")
  })
})

describe("history", () => {
  test("programmatic changes can be undone", () => {
    const { controller } = makeHarness("One")
    controller.appendMarkdown("Two")
    assert.equal(controller.canUndo(), true)
    controller.undo()
    assert.equal(controller.getMarkdown(), "One")
    controller.redo()
    assert.equal(controller.getMarkdown(), "One\n\nTwo")
  })

  test("skipHistory keeps a change out of undo", () => {
    const { controller } = makeHarness("One")
    controller.appendMarkdown("Two", { skipHistory: true })
    assert.equal(controller.canUndo(), false)
  })
})

describe("selection", () => {
  test("getSelection reports document positions and text", () => {
    const { editor, controller } = makeHarness("# Title\n\nBody")
    const from = positionOf(editor, "Title")
    controller.setSelection({ from, to: from + 5 })
    assert.deepEqual(controller.getSelection(), {
      from,
      to: from + 5,
      empty: false,
      text: "Title",
    })
  })

  test("getSelectedMarkdown inside a heading is the inline Markdown", () => {
    const { editor, controller } = makeHarness("# A **bold** title")
    const from = positionOf(editor, "bold")
    controller.setSelection({ from, to: from + 4 })
    assert.equal(controller.getSelectedMarkdown(), "**bold**")
  })

  test("a bookmark follows edits before it", () => {
    const { editor, controller } = makeHarness("Hello world")
    const from = positionOf(editor, "world")
    const bookmark = controller.bookmark({ from, to: from + 5 })
    controller.replaceRange({ from: 1, to: 1 }, "Oh, ")
    const range = bookmark.resolve()
    assert.ok(range)
    assert.equal(editor.state.doc.textBetween(range.from, range.to), "world")
  })

  test("a bookmark is lost when its text changes", () => {
    const { editor, controller } = makeHarness("Hello world")
    const from = positionOf(editor, "world")
    const bookmark = controller.bookmark({ from, to: from + 5 })
    controller.replaceRange({ from: from + 1, to: from + 2 }, "a")
    assert.equal(bookmark.resolve(), null)
  })

  test("a released bookmark resolves to nothing", () => {
    const { controller } = makeHarness("Hello")
    const bookmark = controller.bookmark({ from: 1, to: 3 })
    bookmark.release()
    assert.equal(bookmark.resolve(), null)
  })
})

const noUi: EditorUiHandlers = {
  openDialog: () => {},
  pickImages: () => {},
  toggleFind: () => {},
  toggleOutline: () => {},
  toggleFullscreen: () => {},
  toggleFocusMode: () => {},
  askAI: () => {},
  viewState: () => ({
    find: false,
    outline: false,
    fullscreen: false,
    focusMode: false,
  }),
}

describe("actions", () => {
  test("built-in actions run by id and report their state", async () => {
    const { editor, controller, registry } = makeHarness("Hello world")
    registry.setBuiltIns(
      createBuiltInActions(resolveFeatures("full"), () => noUi)
    )
    const from = positionOf(editor, "world")
    controller.setSelection({ from, to: from + 5 })
    await controller.executeAction("bold")
    assert.equal(controller.getMarkdown(), "Hello **world**")
    const context = createActionContext(controller, "toolbar")
    assert.equal(registry.state("bold", context).active, true)
    await controller.executeAction("heading2")
    assert.equal(controller.getMarkdown(), "## Hello **world**")
  })

  test("disabled features have no actions", () => {
    const ids = createBuiltInActions(
      resolveFeatures("minimal"),
      () => noUi
    ).map((a) => a.id)
    assert.equal(ids.includes("table"), false)
    assert.equal(ids.includes("heading1"), false)
    assert.equal(ids.includes("bold"), true)
  })

  test("a custom action replaces the built-in with its id", async () => {
    const { controller, registry } = makeHarness("text")
    registry.setBuiltIns(
      createBuiltInActions(resolveFeatures("full"), () => noUi)
    )
    let ran = 0
    registry.setCustom([
      { id: "bold", label: "My bold", execute: () => void (ran += 1) },
    ])
    await controller.executeAction("bold")
    assert.equal(ran, 1)
    assert.equal(registry.get("bold")?.label, "My bold")
  })

  test("the spec's uppercase example", async () => {
    const { editor, controller, registry } = makeHarness("make me loud")
    const uppercase: EditorAction = {
      id: "custom.uppercase",
      label: "Convert to uppercase",
      isEnabled: (ctx) => ctx.selection.from !== ctx.selection.to,
      execute: (ctx) => {
        ctx.editor.replaceSelection(ctx.selectedText.toUpperCase())
      },
    }
    registry.setCustom([uppercase])
    await controller.executeAction("custom.uppercase")
    assert.equal(
      controller.getMarkdown(),
      "make me loud",
      "disabled without a selection"
    )
    const from = positionOf(editor, "loud")
    controller.setSelection({ from, to: from + 4 })
    await controller.executeAction("custom.uppercase")
    assert.equal(controller.getMarkdown(), "make me LOUD")
  })

  test("an action can't run twice at once", async () => {
    const { controller, registry } = makeHarness("x")
    let runs = 0
    let finish: () => void = () => {}
    registry.register({
      id: "slow",
      label: "Slow",
      execute: () =>
        new Promise<void>((resolve) => {
          runs += 1
          finish = resolve
        }),
    })
    const first = controller.executeAction("slow")
    await tick()
    assert.equal(registry.isRunning("slow"), true)
    await controller.executeAction("slow")
    finish()
    await first
    assert.equal(runs, 1)
    assert.equal(registry.isRunning("slow"), false)
  })

  test("failures go to onError", async () => {
    const { controller, registry, errors } = makeHarness("x")
    registry.register({
      id: "broken",
      label: "Broken",
      execute: async () => {
        throw new Error("Nope")
      },
    })
    await controller.executeAction("broken")
    await controller.executeAction("missing")
    assert.deepEqual(
      errors.map((error) => [error.code, error.actionId, error.message]),
      [
        ["action-failed", "broken", "Nope"],
        ["unknown-action", "missing", `There's no editor action "missing".`],
      ]
    )
  })

  test("read-only rejects actions unless they allow it", async () => {
    const { editor, controller, registry, errors } = makeHarness("x")
    editor.setEditable(false)
    const ran: string[] = []
    registry.setCustom([
      { id: "edit", label: "Edit", execute: () => void ran.push("edit") },
      {
        id: "look",
        label: "Look",
        allowReadOnly: true,
        execute: () => void ran.push("look"),
      },
    ])
    await controller.executeAction("edit")
    await controller.executeAction("look")
    assert.deepEqual(ran, ["look"])
    assert.equal(errors[0]?.code, "read-only")
  })

  test("contexts are frozen and serialize the document lazily", () => {
    const { controller } = makeHarness("# Doc")
    let reads = 0
    const counting = {
      ...controller,
      getMarkdown: () => {
        reads += 1
        return controller.getMarkdown()
      },
    }
    const context = createActionContext(counting, "api")
    assert.equal(Object.isFrozen(context), true)
    assert.equal(reads, 0)
    assert.equal(context.documentMarkdown, "# Doc")
    assert.equal(context.documentMarkdown, "# Doc")
    assert.equal(reads, 1)
  })
})

describe("MarkdownSync (controlled value)", () => {
  function sync(initial: string, delay = 0) {
    let doc = initial
    const emitted: string[] = []
    const instance = new MarkdownSync({
      initial,
      read: () => doc,
      emit: (markdown) => emitted.push(markdown),
      delay: () => delay,
    })
    return {
      instance,
      emitted,
      edit: (next: string) => (doc = next),
      doc: () => doc,
    }
  }

  test("edits emit; no-op updates don't", () => {
    const { instance, emitted, edit } = sync("a")
    instance.changed()
    assert.deepEqual(emitted, [])
    edit("ab")
    instance.changed()
    instance.changed()
    assert.deepEqual(emitted, ["ab"])
  })

  test("the parent echoing our value replaces nothing", () => {
    const { instance, edit } = sync("a")
    edit("ab")
    instance.changed()
    assert.equal(instance.received("ab"), false)
  })

  test("a new value from outside replaces the document", () => {
    const { instance } = sync("a")
    assert.equal(instance.received("from server"), true)
  })

  test("a value equal to the document needs no replacing", () => {
    const { instance, edit } = sync("a")
    edit("same")
    assert.equal(instance.received("same"), false)
  })

  test("debounced edits emit once, and flush on demand", async () => {
    const { instance, emitted, edit } = sync("a", 20)
    edit("ab")
    instance.changed()
    edit("abc")
    instance.changed()
    assert.deepEqual(emitted, [])
    await new Promise((resolve) => setTimeout(resolve, 40))
    assert.deepEqual(emitted, ["abc"])
    edit("abcd")
    instance.changed()
    instance.flush()
    assert.deepEqual(emitted, ["abc", "abcd"])
    instance.dispose()
  })

  test("an outside value wins over a pending edit", async () => {
    const { instance, emitted, edit } = sync("a", 20)
    edit("typed")
    instance.changed()
    assert.equal(instance.received("server"), true)
    await new Promise((resolve) => setTimeout(resolve, 40))
    assert.deepEqual(emitted, [])
  })
})
