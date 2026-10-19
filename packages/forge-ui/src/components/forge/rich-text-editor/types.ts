import type * as React from "react"
import type { AnyExtension, Editor } from "@tiptap/core"

import type { IconProp } from "../icons"
import type { EditorAIConfig } from "./ai/ai-types.ts"

/**
 * The optional parts of the editor. Each one adds its extension, toolbar and
 * menu entries, `/` commands and Markdown rules; a disabled one is gone from
 * all of them, and Markdown that uses it stays as literal text.
 */
export interface EditorFeatures {
  // Inline formatting
  bold?: boolean
  italic?: boolean
  /** `++underline++` in Markdown. */
  underline?: boolean
  strike?: boolean
  inlineCode?: boolean
  /** `==highlight==` in Markdown. */
  highlight?: boolean
  /** `<sub>` and `<sup>` in Markdown. */
  subscript?: boolean
  superscript?: boolean
  links?: boolean

  // Blocks
  /** H1–H6. */
  headings?: boolean
  blockquote?: boolean
  orderedList?: boolean
  bulletList?: boolean
  /** `- [ ]` checklists. */
  taskList?: boolean
  /** Fenced code blocks. */
  codeBlock?: boolean
  /** Syntax colours and a language picker in code blocks. */
  codeHighlighting?: boolean
  horizontalRule?: boolean
  /** GFM tables (one paragraph per cell, no merged cells). */
  tables?: boolean
  /** `![alt](url)` images by URL, upload, paste or drop. */
  images?: boolean
  /** `$inline$` and `$$block$$` KaTeX formulas. */
  math?: boolean

  // Editing behaviour
  /** Undo and redo. */
  history?: boolean
  /** Typing Markdown (`# `, `- `, `**bold**`) formats as you go. */
  markdownShortcuts?: boolean
  /** The `/` command menu. */
  slashCommands?: boolean
  /** Word and character counts under the text. */
  characterCount?: boolean
  /** `:shortcode:` emoji suggestions (saved as the emoji character). */
  emoji?: boolean
  /** Smart quotes, dashes and arrows as you type. */
  typography?: boolean

  // Tools
  /** The handle beside blocks, for dragging and the block menu. */
  dragHandle?: boolean
  /** ⌘F find and replace. Off by default: it takes over the browser's find. */
  findReplace?: boolean
  /** The outline (table of contents) panel. */
  outline?: boolean
  invisibleCharacters?: boolean
  /** ⌘⇧Enter full-screen editing. */
  fullscreen?: boolean
}

export type ResolvedEditorFeatures = Required<EditorFeatures>

/**
 * Starting points for `features`: `standard` is basic formatting, headings,
 * lists, links, quotes, code and history; `full` turns everything on;
 * `minimal` suits a comment box.
 */
export type EditorPreset = "minimal" | "standard" | "full"

/** Document-model positions (not Markdown character offsets). */
export interface EditorRange {
  from: number
  to: number
}

export interface EditorSelection extends EditorRange {
  /** The selected text, blocks separated by newlines. */
  text: string
  empty: boolean
}

export type EditorErrorCode =
  | "action-failed"
  | "unknown-action"
  | "read-only"
  | "invalid-range"
  | "invalid-props"
  | "unsafe-content"
  | "clipboard"
  | "upload-failed"
  | "ai-failed"
  | "ai-target-changed"

export interface EditorError {
  code: EditorErrorCode
  message: string
  actionId?: string
  cause?: unknown
}

/** Where an action was started from. */
export type ActionSource =
  "toolbar" | "contextMenu" | "bubbleMenu" | "slash" | "shortcut" | "api"

/**
 * An immutable snapshot taken when an action starts (for menus, when the menu
 * opened), so the action works on what the person had selected then.
 */
export interface EditorActionContext {
  editor: EditorController
  selection: EditorSelection
  selectedText: string
  /** Serialized on first read. */
  documentMarkdown: string
  readOnly: boolean
  source: ActionSource
}

/**
 * A command the toolbar, menus, `/` menu, shortcuts and code can all run by
 * id. Registering one with a built-in's id replaces the built-in.
 */
export interface EditorAction {
  id: string
  label: string
  /** A React node, a Forge icon name or a Hugeicons glyph. */
  icon?: React.ReactNode | IconProp
  /** Groups entries in the `/` menu and the toolbar's Insert menu. */
  group?: string
  /** A second line in the `/` menu. */
  description?: string
  /** Extra words the `/` menu matches. */
  keywords?: string[]
  /** A Tiptap key binding (`Mod-Shift-u`): shown in menus and bound in the editor. */
  shortcut?: string
  /** Offer the action in the `/` menu. */
  slashCommand?: boolean
  /** Run it in read-only mode too. Off by default, so read-only rejects it. */
  allowReadOnly?: boolean
  isVisible?: (context: EditorActionContext) => boolean
  isEnabled?: (context: EditorActionContext) => boolean
  /** Shows the action pressed (a toggle), like bold inside bold text. */
  isActive?: (context: EditorActionContext) => boolean
  execute: (context: EditorActionContext) => void | Promise<void>
}

/** Options for the content methods. */
export interface ContentOptions {
  /** Keep the change out of undo history. Default false. */
  skipHistory?: boolean
  /** Apply even in read-only mode. Default false. */
  ignoreReadOnly?: boolean
}

/** A range that follows edits made after it was taken. */
export interface EditorBookmark {
  id: string
  /** The range now, or null when its text changed or was deleted. */
  resolve: () => EditorRange | null
  release: () => void
}

/**
 * Imperative access to the editor, through `ref` or `onReady`. Content goes
 * in and out as Markdown; ranges are document positions. The editing methods
 * return false (and report `read-only` through `onError`) in read-only mode.
 */
export interface EditorController {
  getMarkdown(): string
  setMarkdown(markdown: string, options?: ContentOptions): boolean
  clear(options?: ContentOptions): boolean

  /** At the caret, replacing the selection if there is one. */
  insertMarkdown(markdown: string, options?: ContentOptions): boolean
  /** As new blocks at the end of the document. */
  appendMarkdown(markdown: string, options?: ContentOptions): boolean
  replaceSelection(markdown: string, options?: ContentOptions): boolean
  replaceRange(
    range: EditorRange,
    markdown: string,
    options?: ContentOptions
  ): boolean

  getSelection(): EditorSelection
  getSelectedText(): string
  getSelectedMarkdown(): string
  setSelection(range: EditorRange): void
  /** A range that follows later edits, for async work such as AI. */
  bookmark(range?: EditorRange): EditorBookmark

  /** Focuses the text: at `position`, or back at the selection. */
  focus(position?: "start" | "end" | "all" | number): void
  blur(): void
  undo(): boolean
  redo(): boolean
  canUndo(): boolean
  canRedo(): boolean
  isEmpty(): boolean
  isReadOnly(): boolean

  executeAction(id: string, source?: ActionSource): Promise<void>
  /** Adds an action at runtime; returns a function that removes it. */
  registerAction(action: EditorAction): () => void
  getActions(): EditorAction[]

  /** Runs an AI action (`rewrite` or `ai.rewrite`). */
  runAI(actionId: string, options?: { instruction?: string }): Promise<void>
  cancelAI(): void

  /** The Tiptap editor, for anything the controller doesn't cover. */
  getTiptapEditor(): Editor | null
}

/** A toolbar or bubble menu entry. */
export type ToolbarItem =
  /**
   * An action id (`"bold"`, `"ai.rewrite"`, a custom one), `"separator"`,
   * `"spacer"` (pushes what follows to the end), or a built-in control:
   * `"heading"` (block type), `"insert"`, `"ai"` (AI menu), `"more"`.
   */
  | string
  /** A labelled cluster of items. */
  | { type: "group"; label: string; items: ToolbarItem[] }
  /** A dropdown of actions. */
  | {
      type: "menu"
      label: string
      icon?: React.ReactNode | IconProp
      items: MenuItem[]
    }

export interface ToolbarConfig {
  /** Defaults to on while the editor is editable. */
  enabled?: boolean
  position?: "top" | "bottom"
  /** Defaults to a set that follows the enabled features. */
  items?: ToolbarItem[]
  className?: string
}

/** A context menu entry. */
export type MenuItem =
  /** An action id, `"separator"`, or `"ai"` (a submenu of AI actions). */
  | string
  | {
      type: "submenu"
      label: string
      icon?: React.ReactNode | IconProp
      items: MenuItem[]
    }
  | { type: "group"; label: string; items: MenuItem[] }

export interface ContextMenuConfig {
  /** Defaults to on; off leaves the browser's own menu. */
  enabled?: boolean
  items?: MenuItem[]
}

export interface BubbleMenuConfig {
  /** The selection, link, table and image menus. Defaults to on. */
  enabled?: boolean
  /** What the selection menu shows. */
  items?: ToolbarItem[]
}

/** A person, agent or thing that can be @-mentioned. */
export interface MentionItem {
  id: string
  label: string
  /** A secondary line in the suggestion list (email, role, …). */
  description?: string
}

/** Returns the mention candidates for a query (what follows the `@`). */
export type MentionSource = (
  query: string,
  signal: AbortSignal
) => MentionItem[] | Promise<MentionItem[]>

/**
 * Stores a file the person added (picked, pasted or dropped) and returns the
 * URL to reference. Without it, images are embedded as data URLs.
 */
export type UploadHandler = (file: File) => Promise<string>

/** Which URLs links and images may use. */
export type UrlPolicy = (url: string, kind: "link" | "image") => boolean

/** The dialogs the editor opens to collect a URL or formula. */
export type InsertDialogKind = "link" | "image" | "inlineMath" | "blockMath"

export interface InsertDialogRequest {
  kind: InsertDialogKind
  /** The node being edited, so saving updates it in place. */
  pos?: number
  initial?: string
}

/**
 * The editor's own UI that actions open. The component supplies these; a
 * headless editor gets simple fallbacks.
 */
export interface EditorUiHandlers {
  openDialog: (request: InsertDialogRequest) => void
  /** Opens the file picker and inserts the chosen images. */
  pickImages: () => void
  toggleFind: (open?: boolean) => void
  toggleOutline: (open?: boolean) => void
  toggleFullscreen: (open?: boolean) => void
  /** Dims every block but the one being edited. */
  toggleFocusMode: (on?: boolean) => void
  /** Opens the AI panel's prompt box for "Ask AI…". */
  askAI: () => void
  /** Which panels and views are on, for the toolbar's pressed states. */
  viewState: () => EditorViewState
}

export interface EditorViewState {
  find: boolean
  outline: boolean
  fullscreen: boolean
  focusMode: boolean
}

/** Props every replaceable UI area (`components`) receives. */
export interface EditorSlotProps {
  controller: EditorController
  editor: Editor
}

export type { AnyExtension, EditorAIConfig }
