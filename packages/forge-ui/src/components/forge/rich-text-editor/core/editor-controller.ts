import type { Editor, JSONContent } from "@tiptap/core"

import type { AIController } from "../ai/ai-controller.ts"
import type {
  ContentOptions,
  EditorController,
  EditorError,
  EditorRange,
  EditorSelection,
  UrlPolicy,
} from "../types.ts"
import { ActionRegistry, createActionContext } from "./action-registry.ts"
import {
  insertableContent,
  parseMarkdown,
  serializeContent,
  serializeMarkdown,
} from "./markdown.ts"
import { isAlive, isMounted } from "./lifecycle.ts"
import { createBookmark, rangeText } from "./selection-manager.ts"

/** What the controller reads from the component that owns it. */
export interface ControllerHost {
  /** The current editor; it's replaced when the features change. */
  getEditor: () => Editor | null
  registry: ActionRegistry
  getUrlPolicy: () => UrlPolicy | undefined
  getAI: () => AIController | null
  onError: (error: EditorError) => void
}

/**
 * The editor's imperative API. It outlives any one Tiptap instance: every
 * call reads the current editor, so a `ref` stays valid when features change.
 */
export function createEditorController(host: ControllerHost): EditorController {
  const live = () => {
    const editor = host.getEditor()
    return isAlive(editor) ? editor : null
  }

  /** The editor if it may be changed now; reports why not otherwise. */
  const writable = (options?: ContentOptions) => {
    const editor = live()
    if (!editor) return null
    if (!editor.isEditable && !options?.ignoreReadOnly) {
      host.onError({
        code: "read-only",
        message: "The editor is read-only.",
      })
      return null
    }
    return editor
  }

  const parse = (editor: Editor, markdown: string) => {
    const { doc, removed } = parseMarkdown(
      editor,
      markdown,
      host.getUrlPolicy()
    )
    if (removed > 0) {
      host.onError({
        code: "unsafe-content",
        message: `Removed ${removed} link${removed === 1 ? "" : "s"} or image${removed === 1 ? "" : "s"} with a URL the editor doesn't allow.`,
      })
    }
    return doc
  }

  const validRange = (editor: Editor, range: EditorRange) => {
    const size = editor.state.doc.content.size
    const ok =
      Number.isInteger(range.from) &&
      Number.isInteger(range.to) &&
      range.from >= 0 &&
      range.from <= range.to &&
      range.to <= size
    if (!ok) {
      host.onError({
        code: "invalid-range",
        message: `${range.from}–${range.to} isn't a range in this document (0–${size}).`,
      })
    }
    return ok
  }

  /** Runs `change` as one transaction, kept out of history if asked. */
  const transact = (
    editor: Editor,
    options: ContentOptions | undefined,
    change: (chain: ReturnType<Editor["chain"]>) => ReturnType<Editor["chain"]>
  ) => {
    try {
      let chain = editor.chain()
      if (options?.skipHistory) {
        chain = chain.command(({ tr }) => {
          tr.setMeta("addToHistory", false)
          return true
        })
      }
      return change(chain).run()
    } catch (cause) {
      host.onError({
        code: "action-failed",
        message:
          cause instanceof Error ? cause.message : "The content didn't fit.",
        cause,
      })
      return false
    }
  }

  const replaceRange = (
    range: EditorRange,
    markdown: string,
    options?: ContentOptions
  ) => {
    const editor = writable(options)
    if (!editor || !validRange(editor, range)) return false
    const { doc } = editor.state
    const $from = doc.resolve(range.from)
    const $to = doc.resolve(range.to)
    // Inside a line of text a one-paragraph answer joins that line; at a
    // block boundary it stays a paragraph of its own.
    const inline = $from.parent.isTextblock && $to.parent.isTextblock
    const content = insertableContent(parse(editor, markdown), inline)
    return transact(editor, options, (chain) =>
      content.length
        ? chain.insertContentAt(range, content, { updateSelection: true })
        : chain.deleteRange(range)
    )
  }

  const getSelection = (): EditorSelection => {
    const editor = live()
    if (!editor) return { from: 0, to: 0, text: "", empty: true }
    const { from, to, empty } = editor.state.selection
    return { from, to, empty, text: rangeText(editor.state.doc, from, to) }
  }

  const controller: EditorController = {
    getMarkdown: () => {
      const editor = live()
      return editor ? serializeMarkdown(editor) : ""
    },

    setMarkdown: (markdown, options) => {
      const editor = writable(options)
      if (!editor) return false
      const doc = parse(editor, markdown)
      return transact(editor, options, (chain) =>
        chain.setContent(doc as JSONContent, { emitUpdate: true })
      )
    },

    clear: (options) => controller.setMarkdown("", options),

    insertMarkdown: (markdown, options) => {
      const { from, to } = getSelection()
      return replaceRange({ from, to }, markdown, options)
    },

    appendMarkdown: (markdown, options) => {
      const editor = writable(options)
      if (!editor) return false
      const blocks = parse(editor, markdown).content ?? []
      if (!blocks.length) return false
      const { doc } = editor.state
      const last = doc.lastChild
      const end = doc.content.size
      // An empty last paragraph (the one an empty editor has) is replaced.
      const range =
        last && last.type.name === "paragraph" && last.content.size === 0
          ? { from: end - last.nodeSize, to: end }
          : { from: end, to: end }
      return transact(editor, options, (chain) =>
        chain.insertContentAt(range, blocks, { updateSelection: false })
      )
    },

    replaceSelection: (markdown, options) =>
      controller.insertMarkdown(markdown, options),

    replaceRange,

    getSelection,

    getSelectedText: () => getSelection().text,

    getSelectedMarkdown: () => {
      const editor = live()
      if (!editor) return ""
      const { from, to, empty, $from, $to } = editor.state.selection
      if (empty) return ""
      // Within one line, the selection is its inline Markdown alone, not the
      // heading, list item or quote around it.
      if ($from.sameParent($to) && $from.parent.isTextblock) {
        const inline = $from.parent.content.cut(
          $from.parentOffset,
          $to.parentOffset
        )
        return serializeContent(editor, [
          { type: "paragraph", content: inline.toJSON() ?? [] },
        ])
      }
      return serializeContent(
        editor,
        editor.state.doc.slice(from, to).content.toJSON() ?? []
      )
    },

    setSelection: (range) => {
      const editor = live()
      if (!editor || !validRange(editor, range)) return
      editor.commands.setTextSelection(range)
    },

    bookmark: (range) => {
      const editor = live()
      if (!editor) {
        return { id: "", resolve: () => null, release: () => {} }
      }
      return createBookmark(editor, range ?? getSelection())
    },

    focus: (position) => {
      const editor = live()
      // Without a position, focus returns to the selection as it was.
      if (editor && isMounted(editor)) editor.commands.focus(position ?? null)
    },

    blur: () => {
      const editor = live()
      if (editor && isMounted(editor)) editor.commands.blur()
    },

    undo: () => {
      const editor = writable()
      return Boolean(editor?.can().undo?.() && editor.commands.undo())
    },

    redo: () => {
      const editor = writable()
      return Boolean(editor?.can().redo?.() && editor.commands.redo())
    },

    canUndo: () => {
      const editor = live()
      return Boolean(editor?.isEditable && editor.can().undo?.())
    },

    canRedo: () => {
      const editor = live()
      return Boolean(editor?.isEditable && editor.can().redo?.())
    },

    isEmpty: () => live()?.isEmpty ?? true,

    isReadOnly: () => !(live()?.isEditable ?? false),

    executeAction: async (id, source = "api") => {
      await host.registry.run(id, createActionContext(controller, source))
    },

    registerAction: (action) => host.registry.register(action),

    getActions: () => host.registry.list(),

    runAI: async (actionId, options) => {
      const ai = host.getAI()
      if (!ai) {
        host.onError({
          code: "unknown-action",
          message: "AI isn't enabled for this editor.",
          actionId,
        })
        return
      }
      await ai.start(actionId, options)
    },

    cancelAI: () => host.getAI()?.cancel(),

    getTiptapEditor: () => live(),
  }

  return controller
}
