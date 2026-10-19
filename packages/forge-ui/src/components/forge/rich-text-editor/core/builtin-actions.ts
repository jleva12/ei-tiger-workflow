import {
  CheckListIcon,
  ClipboardIcon,
  CodeSquareIcon,
  Copy01Icon,
  Download01Icon,
  EraserIcon,
  FullScreenIcon,
  GridTableIcon,
  Heading01Icon,
  Heading02Icon,
  Heading03Icon,
  Heading04Icon,
  Heading05Icon,
  Heading06Icon,
  HighlighterIcon,
  Image01Icon,
  LeftToRightListBulletIcon,
  LeftToRightListNumberIcon,
  Link01Icon,
  ListIndentDecreaseIcon,
  ListIndentIncreaseIcon,
  MinusSignIcon,
  ParagraphIcon,
  QuoteDownIcon,
  RedoIcon,
  Scissor01Icon,
  SearchReplaceIcon,
  SigmaIcon,
  SmileIcon,
  SourceCodeIcon,
  SquareRootSquareIcon,
  SubscriptIcon,
  SuperscriptIcon,
  TableOfContentsIcon,
  Target02Icon,
  TextBoldIcon,
  TextItalicIcon,
  TextSelectionIcon,
  TextStrikethroughIcon,
  TextUnderlineIcon,
  UndoIcon,
  Unlink02Icon,
  ViewIcon,
} from "@hugeicons/core-free-icons"
import type { ChainedCommands, Editor } from "@tiptap/core"
// Brings in the commands' types (undo, toggleHighlight, insertTable, …).
import type {} from "@tiptap/starter-kit"
import type {} from "@tiptap/extension-highlight"
import type {} from "@tiptap/extension-list"
import type {} from "@tiptap/extension-mathematics"
import type {} from "@tiptap/extension-subscript"
import type {} from "@tiptap/extension-superscript"
import type {} from "@tiptap/extension-table"
import type {} from "@tiptap/extension-find-and-replace"
import type {} from "@tiptap/extension-invisible-characters"

import type {
  EditorAction,
  EditorActionContext,
  EditorUiHandlers,
  ResolvedEditorFeatures,
} from "../types.ts"
import { canReadClipboard, copySelection, pasteClipboard } from "./clipboard.ts"
import { isMounted } from "./lifecycle.ts"

function tiptap(context: EditorActionContext): Editor {
  const editor = context.editor.getTiptapEditor()
  if (!editor) throw new Error("The editor isn't ready.")
  return editor
}

/** A command chain that returns focus to the text when there's a page. */
function chain(editor: Editor) {
  return isMounted(editor) ? editor.chain().focus() : editor.chain()
}

/** Tiptap state for predicates, or null while the editor is replaced. */
const editorOf = (context: EditorActionContext) =>
  context.editor.getTiptapEditor()

const inCodeBlock = (editor: Editor) => editor.isActive("codeBlock")

/** The item type the list commands act on at the selection. */
const listItemType = (editor: Editor) =>
  editor.isActive("taskItem") && editor.schema.nodes.taskItem
    ? "taskItem"
    : "listItem"

type Command = (chain: ChainedCommands) => ChainedCommands

/**
 * An action built on one Tiptap command: enabled when the command can run
 * here, pressed when `active` says so.
 */
function command(
  spec: Omit<EditorAction, "execute" | "isEnabled" | "isActive"> & {
    run: Command
    active?: (editor: Editor) => boolean
    /** Formatting that doesn't apply inside code blocks. */
    inline?: boolean
  }
): EditorAction {
  const { run, active, inline, ...action } = spec
  return {
    ...action,
    isActive: active
      ? (context) => {
          const editor = editorOf(context)
          return editor ? active(editor) : false
        }
      : undefined,
    isEnabled: (context) => {
      const editor = editorOf(context)
      if (!editor) return false
      if (inline && inCodeBlock(editor)) return false
      return run(editor.can().chain()).run()
    },
    execute: (context) => {
      run(chain(tiptap(context))).run()
    },
  }
}

const HEADING_ICONS = [
  Heading01Icon,
  Heading02Icon,
  Heading03Icon,
  Heading04Icon,
  Heading05Icon,
  Heading06Icon,
]

const HEADING_HINTS = ["Page title", "Section", "Subsection"]

function downloadText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  link.click()
  // Revoking in the same task can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * The editor's own actions for the enabled features. Toolbars, menus, the
 * `/` menu and `executeAction` all run these by id, so each command has one
 * implementation.
 */
export function createBuiltInActions(
  features: ResolvedEditorFeatures,
  ui: () => EditorUiHandlers
): EditorAction[] {
  const actions: EditorAction[] = []
  const add = (enabled: boolean, ...items: EditorAction[]) => {
    if (enabled) actions.push(...items)
  }

  add(
    features.history,
    {
      id: "undo",
      label: "Undo",
      icon: UndoIcon,
      group: "History",
      shortcut: "Mod-z",
      isEnabled: (context) => context.editor.canUndo(),
      execute: (context) => {
        context.editor.undo()
      },
    },
    {
      id: "redo",
      label: "Redo",
      icon: RedoIcon,
      group: "History",
      shortcut: "Mod-Shift-z",
      isEnabled: (context) => context.editor.canRedo(),
      execute: (context) => {
        context.editor.redo()
      },
    }
  )

  // Inline formatting.
  const mark = (
    enabled: boolean,
    id: string,
    label: string,
    icon: EditorAction["icon"],
    shortcut: string,
    name: string,
    run: Command
  ) =>
    add(
      enabled,
      command({
        id,
        label,
        icon,
        shortcut,
        group: "Format",
        inline: true,
        active: (editor) => editor.isActive(name),
        run,
      })
    )
  mark(features.bold, "bold", "Bold", TextBoldIcon, "Mod-b", "bold", (c) =>
    c.toggleBold()
  )
  mark(
    features.italic,
    "italic",
    "Italic",
    TextItalicIcon,
    "Mod-i",
    "italic",
    (c) => c.toggleItalic()
  )
  mark(
    features.underline,
    "underline",
    "Underline",
    TextUnderlineIcon,
    "Mod-u",
    "underline",
    (c) => c.toggleUnderline()
  )
  mark(
    features.strike,
    "strike",
    "Strikethrough",
    TextStrikethroughIcon,
    "Mod-Shift-s",
    "strike",
    (c) => c.toggleStrike()
  )
  mark(
    features.inlineCode,
    "inlineCode",
    "Inline code",
    SourceCodeIcon,
    "Mod-e",
    "code",
    (c) => c.toggleCode()
  )
  mark(
    features.highlight,
    "highlight",
    "Highlight",
    HighlighterIcon,
    "Mod-Shift-h",
    "highlight",
    (c) => c.toggleHighlight()
  )
  mark(
    features.subscript,
    "subscript",
    "Subscript",
    SubscriptIcon,
    "Mod-,",
    "subscript",
    (c) => c.toggleSubscript()
  )
  mark(
    features.superscript,
    "superscript",
    "Superscript",
    SuperscriptIcon,
    "Mod-.",
    "superscript",
    (c) => c.toggleSuperscript()
  )

  add(
    features.links,
    {
      id: "link",
      label: "Link",
      icon: Link01Icon,
      group: "Format",
      shortcut: "Mod-k",
      isActive: (context) => editorOf(context)?.isActive("link") ?? false,
      isEnabled: (context) => {
        const editor = editorOf(context)
        return Boolean(editor && !inCodeBlock(editor))
      },
      execute: () => ui().openDialog({ kind: "link" }),
    },
    command({
      id: "unlink",
      label: "Remove link",
      icon: Unlink02Icon,
      group: "Format",
      isVisible: (context) => editorOf(context)?.isActive("link") ?? false,
      run: (c) => c.extendMarkRange("link").unsetLink(),
    })
  )
  add(
    true,
    command({
      id: "clearFormatting",
      label: "Clear formatting",
      icon: EraserIcon,
      group: "Format",
      run: (c) => c.unsetAllMarks().clearNodes(),
    })
  )

  // Blocks: what the `/` menu adds and "Turn into" converts.
  add(
    true,
    command({
      id: "paragraph",
      label: "Text",
      icon: ParagraphIcon,
      group: "Text",
      description: "Plain paragraph",
      keywords: ["paragraph", "body", "normal"],
      shortcut: "Mod-Alt-0",
      slashCommand: true,
      active: (editor) => editor.isActive("paragraph"),
      run: (c) => c.clearNodes().setParagraph(),
    })
  )
  if (features.headings) {
    for (const level of [1, 2, 3, 4, 5, 6] as const) {
      actions.push(
        command({
          id: `heading${level}`,
          label: `Heading ${level}`,
          icon: HEADING_ICONS[level - 1],
          group: "Text",
          description: HEADING_HINTS[level - 1] ?? `Level ${level} heading`,
          keywords: ["heading", "title", `h${level}`],
          shortcut: `Mod-Alt-${level}`,
          slashCommand: level <= 3,
          active: (editor) => editor.isActive("heading", { level }),
          run: (c) => c.toggleHeading({ level }),
        })
      )
    }
  }
  add(
    features.blockquote,
    command({
      id: "blockquote",
      label: "Quote",
      icon: QuoteDownIcon,
      group: "Text",
      description: "Set text apart as a quotation",
      keywords: ["blockquote", "citation"],
      shortcut: "Mod-Shift-b",
      slashCommand: true,
      active: (editor) => editor.isActive("blockquote"),
      run: (c) => c.toggleBlockquote(),
    })
  )
  add(
    features.codeBlock,
    command({
      id: "codeBlock",
      label: "Code block",
      icon: CodeSquareIcon,
      group: "Text",
      description: features.codeHighlighting
        ? "Code with syntax highlighting"
        : "A fenced block of code",
      keywords: ["code", "snippet", "pre", "fence"],
      shortcut: "Mod-Alt-c",
      slashCommand: true,
      active: (editor) => editor.isActive("codeBlock"),
      run: (c) => c.toggleCodeBlock(),
    })
  )

  add(
    features.bulletList,
    command({
      id: "bulletList",
      label: "Bulleted list",
      icon: LeftToRightListBulletIcon,
      group: "Lists",
      description: "A simple list",
      keywords: ["unordered", "ul", "bullets"],
      shortcut: "Mod-Shift-8",
      slashCommand: true,
      active: (editor) => editor.isActive("bulletList"),
      run: (c) => c.toggleBulletList(),
    })
  )
  add(
    features.orderedList,
    command({
      id: "orderedList",
      label: "Numbered list",
      icon: LeftToRightListNumberIcon,
      group: "Lists",
      description: "A list with numbers",
      keywords: ["ordered", "ol", "numbers"],
      shortcut: "Mod-Shift-7",
      slashCommand: true,
      active: (editor) => editor.isActive("orderedList"),
      run: (c) => c.toggleOrderedList(),
    })
  )
  add(
    features.taskList,
    command({
      id: "taskList",
      label: "Checklist",
      icon: CheckListIcon,
      group: "Lists",
      description: "Track tasks with checkboxes",
      keywords: ["todo", "task", "checkbox"],
      shortcut: "Mod-Shift-9",
      slashCommand: true,
      active: (editor) => editor.isActive("taskList"),
      run: (c) => c.toggleTaskList(),
    })
  )
  if (features.bulletList || features.orderedList || features.taskList) {
    const listCommand = (
      id: string,
      label: string,
      icon: EditorAction["icon"],
      shortcut: string,
      sink: boolean
    ): EditorAction => ({
      id,
      label,
      icon,
      shortcut,
      group: "Lists",
      isEnabled: (context) => {
        const editor = editorOf(context)
        if (!editor) return false
        const type = listItemType(editor)
        return sink
          ? editor.can().sinkListItem(type)
          : editor.can().liftListItem(type)
      },
      execute: (context) => {
        const editor = tiptap(context)
        const type = listItemType(editor)
        const c = chain(editor)
        ;(sink ? c.sinkListItem(type) : c.liftListItem(type)).run()
      },
    })
    actions.push(
      listCommand("indent", "Indent", ListIndentIncreaseIcon, "Tab", true),
      listCommand(
        "outdent",
        "Outdent",
        ListIndentDecreaseIcon,
        "Shift-Tab",
        false
      )
    )
  }

  add(
    features.horizontalRule,
    command({
      id: "horizontalRule",
      label: "Divider",
      icon: MinusSignIcon,
      group: "Insert",
      description: "A horizontal rule between sections",
      keywords: ["horizontal rule", "hr", "separator", "line"],
      slashCommand: true,
      run: (c) => c.setHorizontalRule(),
    })
  )
  add(
    features.tables,
    command({
      id: "table",
      label: "Table",
      icon: GridTableIcon,
      group: "Insert",
      description: "A 3 × 3 table with a header row",
      keywords: ["grid", "rows", "columns", "spreadsheet"],
      slashCommand: true,
      run: (c) => c.insertTable({ rows: 3, cols: 3, withHeaderRow: true }),
    })
  )
  add(features.images, {
    id: "image",
    label: "Image",
    icon: Image01Icon,
    group: "Insert",
    description: "Upload or link an image",
    keywords: ["picture", "photo", "upload", "img"],
    slashCommand: true,
    execute: () => ui().openDialog({ kind: "image" }),
  })
  add(
    features.math,
    {
      id: "blockMath",
      label: "Formula block",
      icon: SigmaIcon,
      group: "Insert",
      description: "A centred LaTeX equation",
      keywords: ["math", "latex", "katex", "equation"],
      slashCommand: true,
      execute: () => ui().openDialog({ kind: "blockMath" }),
    },
    {
      id: "inlineMath",
      label: "Inline formula",
      icon: SquareRootSquareIcon,
      group: "Insert",
      description: "LaTeX inside a line of text",
      keywords: ["math", "latex", "katex", "equation"],
      slashCommand: true,
      execute: () => ui().openDialog({ kind: "inlineMath" }),
    }
  )
  add(
    features.emoji,
    command({
      id: "emoji",
      label: "Emoji",
      icon: SmileIcon,
      group: "Insert",
      description: "Search emoji by name",
      keywords: ["emoticon", "smiley"],
      slashCommand: true,
      run: (c) => c.insertContent(":"),
    })
  )

  // Clipboard, for the context menu: the keys work without these.
  const hasSelection = (context: EditorActionContext) =>
    !context.selection.empty
  actions.push(
    {
      id: "cut",
      label: "Cut",
      icon: Scissor01Icon,
      group: "Clipboard",
      shortcut: "Mod-x",
      isEnabled: hasSelection,
      execute: async (context) => {
        const editor = tiptap(context)
        await copySelection(editor, context.editor)
        chain(editor).deleteSelection().run()
      },
    },
    {
      id: "copy",
      label: "Copy",
      icon: Copy01Icon,
      group: "Clipboard",
      shortcut: "Mod-c",
      allowReadOnly: true,
      isEnabled: hasSelection,
      execute: (context) => copySelection(tiptap(context), context.editor),
    },
    {
      id: "paste",
      label: "Paste",
      icon: ClipboardIcon,
      group: "Clipboard",
      shortcut: "Mod-v",
      isEnabled: () => canReadClipboard(),
      execute: (context) => pasteClipboard(tiptap(context), context.editor),
    },
    {
      id: "selectAll",
      label: "Select all",
      icon: TextSelectionIcon,
      group: "Clipboard",
      shortcut: "Mod-a",
      allowReadOnly: true,
      execute: (context) => {
        chain(tiptap(context)).selectAll().run()
      },
    }
  )

  // Panels and views.
  add(features.findReplace, {
    id: "find",
    label: "Find and replace",
    icon: SearchReplaceIcon,
    group: "Tools",
    shortcut: "Mod-f",
    allowReadOnly: true,
    isActive: () => ui().viewState().find,
    execute: () => ui().toggleFind(),
  })
  add(features.outline, {
    id: "outline",
    label: "Outline",
    icon: TableOfContentsIcon,
    group: "Tools",
    allowReadOnly: true,
    isActive: () => ui().viewState().outline,
    execute: () => ui().toggleOutline(),
  })
  add(features.fullscreen, {
    id: "fullscreen",
    label: "Full screen",
    icon: FullScreenIcon,
    group: "Tools",
    shortcut: "Mod-Shift-Enter",
    allowReadOnly: true,
    isActive: () => ui().viewState().fullscreen,
    execute: () => ui().toggleFullscreen(),
  })
  actions.push({
    id: "focusMode",
    label: "Focus mode",
    icon: Target02Icon,
    group: "Tools",
    allowReadOnly: true,
    isActive: () => ui().viewState().focusMode,
    execute: () => ui().toggleFocusMode(),
  })
  add(features.invisibleCharacters, {
    id: "invisibleCharacters",
    label: "Invisible characters",
    icon: ViewIcon,
    group: "Tools",
    allowReadOnly: true,
    isActive: (context) =>
      Boolean(editorOf(context)?.storage.invisibleCharacters?.visibility()),
    execute: (context) => {
      tiptap(context).commands.toggleInvisibleCharacters()
    },
  })
  actions.push(
    {
      id: "copyMarkdown",
      label: "Copy as Markdown",
      icon: Copy01Icon,
      group: "Export",
      allowReadOnly: true,
      execute: async (context) => {
        await navigator.clipboard.writeText(context.documentMarkdown)
      },
    },
    {
      id: "downloadMarkdown",
      label: "Download Markdown",
      icon: Download01Icon,
      group: "Export",
      allowReadOnly: true,
      execute: (context) =>
        downloadText(context.documentMarkdown, "document.md", "text/markdown"),
    }
  )

  return actions
}
