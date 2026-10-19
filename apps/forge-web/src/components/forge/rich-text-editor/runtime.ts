import type { Editor } from "@tiptap/core"
import { TextSelection } from "@tiptap/pm/state"

import { AIController } from "./ai/ai-controller"
import { ActionRegistry } from "./core/action-registry"
import { createEditorController } from "./core/editor-controller"
import { isAlive } from "./core/lifecycle"
import { parseMarkdown, serializeMarkdown } from "./core/markdown"
import { MarkdownSync } from "./core/markdown-sync"
import { defaultUrlPolicy } from "./core/security"
import type { EditorHandlers } from "./extensions"
import { insertFiles } from "./files"
import type {
  EditorController,
  EditorError,
  EditorUiHandlers,
  UrlPolicy,
} from "./types"
import type { UseRichTextEditorOptions } from "./use-rich-text-editor"

const DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024

const unsafeMessage = (removed: number) =>
  `Removed ${removed} link or image URL${removed === 1 ? "" : "s"} the editor doesn't allow.`

/**
 * What outlives renders and rebuilt Tiptap instances: the controller, the
 * action registry, AI and the controlled-value sync. Every callback reads
 * the options of the latest render, so nothing is rebuilt for a new prop.
 */
export class EditorRuntime {
  private options: UseRichTextEditorOptions
  private editor: Editor | null = null
  private readyNotified = false
  readonly registry: ActionRegistry
  readonly controller: EditorController
  readonly ai: AIController
  readonly sync: MarkdownSync
  readonly handlers: EditorHandlers

  constructor(options: UseRichTextEditorOptions) {
    this.options = options
    this.registry = new ActionRegistry(this.onError)
    this.controller = createEditorController({
      getEditor: () => this.editor,
      registry: this.registry,
      getUrlPolicy: () => this.options.urlPolicy,
      getAI: () => (this.options.ai?.enabled ? this.ai : null),
      onError: this.onError,
    })
    this.ai = new AIController({
      controller: this.controller,
      getEditor: () => this.editor,
      getConfig: () => this.options.ai,
      onError: this.onError,
    })
    this.sync = new MarkdownSync({
      initial: options.value ?? options.defaultValue ?? "",
      read: () => this.currentContent(""),
      emit: (markdown) => this.options.onChange?.(markdown),
      delay: () => this.options.onChangeDebounce ?? 0,
    })
    this.handlers = {
      ui: this.ui,
      registry: this.registry,
      controller: this.controller,
      insertFiles: (editor, files, pos) =>
        void insertFiles(editor, files, {
          upload: this.options.onUpload,
          maxFileSize: this.options.maxFileSize ?? DEFAULT_MAX_FILE_SIZE,
          pos,
          urlPolicy: this.policy(),
          onError: (message) =>
            this.onError({ code: "upload-failed", message }),
        }),
      placeholder: () => this.options.placeholder ?? "Write something…",
      mentions: (query, signal) => this.options.mentions?.(query, signal) ?? [],
      urlPolicy: () => this.options.urlPolicy,
      onUnsafeContent: (removed) =>
        this.onError({
          code: "unsafe-content",
          message: unsafeMessage(removed),
        }),
    }
  }

  /** Called after every render with that render's options. */
  setOptions(options: UseRichTextEditorOptions) {
    this.options = options
  }

  /** Called as each Tiptap instance is created. */
  setEditor(editor: Editor) {
    this.editor = editor
  }

  onError = (error: EditorError) => {
    if (this.options.onError) this.options.onError(error)
    else console.warn(`[RichTextEditor] ${error.message}`)
  }

  policy = (): UrlPolicy => this.options.urlPolicy ?? defaultUrlPolicy

  /** The component's UI, with simple fallbacks for a headless editor. */
  ui = (): EditorUiHandlers => ({
    openDialog: (request) => {
      // Without dialogs, a link falls back to the browser's prompt.
      const editor = this.editor
      if (request.kind !== "link" || !editor || typeof window === "undefined") {
        return
      }
      const current = String(editor.getAttributes("link").href ?? "")
      const href = window.prompt("Link URL", current)
      if (href === null) return
      const chain = editor.chain().focus().extendMarkRange("link")
      if (href.trim()) chain.setLink({ href: href.trim() }).run()
      else chain.unsetLink().run()
    },
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
    ...this.options.ui,
  })

  /** The document now, or `fallback` before there's an editor. */
  currentContent(fallback: string) {
    return isAlive(this.editor) ? serializeMarkdown(this.editor) : fallback
  }

  /** `onReady`, once per component however often the editor is rebuilt. */
  notifyReady() {
    if (this.readyNotified) return
    this.readyNotified = true
    this.options.onReady?.(this.controller)
  }

  focused() {
    this.options.onFocus?.()
  }

  blurred() {
    this.sync.flush()
    this.options.onBlur?.()
  }

  selectionChanged() {
    this.options.onSelectionChange?.(this.controller.getSelection())
  }

  /**
   * A controlled `value` arrived: unless it's our own echo, it replaces the
   * document, outside undo history and without firing `onChange`, keeping
   * the caret where the new text allows.
   */
  receive(editor: Editor, markdown: string) {
    if (!this.sync.received(markdown)) return
    const { doc, removed } = parseMarkdown(editor, markdown, this.policy())
    if (removed) {
      this.onError({ code: "unsafe-content", message: unsafeMessage(removed) })
    }
    let node
    try {
      node = editor.schema.nodeFromJSON(doc)
    } catch (cause) {
      this.onError({
        code: "invalid-props",
        message: "The value doesn't fit this editor's features.",
        cause,
      })
      return
    }
    const { from, to } = editor.state.selection
    const tr = editor.state.tr.replaceWith(
      0,
      editor.state.doc.content.size,
      node.content
    )
    const size = tr.doc.content.size
    tr.setSelection(
      TextSelection.between(
        tr.doc.resolve(Math.min(from, size)),
        tr.doc.resolve(Math.min(to, size))
      )
    )
    tr.setMeta("addToHistory", false).setMeta("preventUpdate", true)
    editor.view.dispatch(tr)
  }

  /** Unmounting: send an edit still waiting in the debounce, stop AI. */
  dispose() {
    this.sync.flush()
    this.sync.dispose()
    this.ai.cancel()
  }
}
