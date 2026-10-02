import type { Editor, JSONContent } from "@tiptap/react"

/**
 * How the editor reads and writes its value. `html` and `markdown` are
 * strings; `json` is Tiptap's document JSON, the lossless format (HTML keeps
 * everything too; Markdown drops styling Markdown can't express, such as
 * colours, alignment and font sizes).
 */
export type RichTextFormat = "html" | "json" | "markdown"

export type RichTextValue = string | JSONContent

/**
 * The optional parts of the editor. Every feature is on in the `full`
 * preset; `standard` and `minimal` switch groups off for lighter fields
 * (comments, descriptions), and `features` overrides any preset.
 */
export interface RichTextFeatures {
  /** Headings H1–H6 (H1–H3 in the menus; ⌥⌘1–6 reach all six). */
  headings: boolean
  /** Text colour, highlight colours, font family, font size, line height. */
  textStyle: boolean
  /** Left, centre, right and justified paragraphs and headings. */
  alignment: boolean
  /** Subscript and superscript. */
  scripts: boolean
  /** Checklists with nested items. */
  taskLists: boolean
  /** Resizable tables with header rows/columns and merged cells. */
  tables: boolean
  /** Images by upload, paste, drop or URL; resizable. */
  images: boolean
  /** YouTube, Twitch and audio embeds. */
  media: boolean
  /** Syntax-highlighted code blocks with a language picker. */
  codeHighlighting: boolean
  /** Inline and block KaTeX formulas. */
  math: boolean
  /** Collapsible sections (details / summary). */
  details: boolean
  /** `:shortcode:` emoji with suggestions, and emoticons like `:)`. */
  emoji: boolean
  /** Smart quotes, dashes, arrows, fractions and other typography rules. */
  typography: boolean
  /** The `/` command menu and the `+` menu on empty lines. */
  slashCommands: boolean
  /** The block handle for dragging blocks and the block menu. */
  dragHandle: boolean
  /** ⌘F find and replace, with regex, case and whole-word options. */
  findReplace: boolean
  /** The outline (table of contents) panel. */
  outline: boolean
  /** Show spaces, paragraph ends and line breaks on demand. */
  invisibleCharacters: boolean
  /** Full-screen editing. */
  fullscreen: boolean
  /** Stable `data-id` attributes on blocks, for linking to or diffing them. */
  uniqueIds: boolean
}

export type RichTextPreset = "full" | "standard" | "minimal"

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
 * URL to reference. Without it, files are embedded as data URLs.
 */
export type UploadHandler = (file: File) => Promise<string>

/** Imperative access to a mounted editor, through `ref`. */
export interface RichTextEditorHandle {
  /** The Tiptap editor, for anything the component doesn't cover. */
  editor: Editor | null
  focus: (position?: "start" | "end" | "all") => void
  clear: () => void
  /** The current content in any format, whatever `format` the field uses. */
  getValue: <F extends RichTextFormat>(
    format?: F
  ) => F extends "json" ? JSONContent : string
  /** Replaces the content without firing `onChange`. */
  setValue: (value: RichTextValue, format?: RichTextFormat) => void
}

/** The dialogs the editor opens to collect a URL, formula or file. */
export type InsertDialogKind =
  "link" | "image" | "youtube" | "twitch" | "audio" | "inlineMath" | "blockMath"

export interface InsertDialogRequest {
  kind: InsertDialogKind
  /** For math: the node being edited, so saving updates it in place. */
  pos?: number
  initial?: string
}

/** Editor-level actions the toolbar, menus and `/` commands share. */
export interface RichTextActions {
  openDialog: (request: InsertDialogRequest) => void
  /** Opens the file picker and inserts the chosen images or audio. */
  pickFiles: (accept: "image" | "audio") => void
  toggleFind: (open?: boolean) => void
  toggleOutline: (open?: boolean) => void
  toggleFullscreen: (open?: boolean) => void
}
