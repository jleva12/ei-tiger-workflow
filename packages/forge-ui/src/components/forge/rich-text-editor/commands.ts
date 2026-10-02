import {
  CheckListIcon,
  CodeSquareIcon,
  GridTableIcon,
  Heading01Icon,
  Heading02Icon,
  Heading03Icon,
  Image01Icon,
  LeftToRightListBulletIcon,
  LeftToRightListNumberIcon,
  ListCollapseIcon,
  MinusSignIcon,
  MusicNote01Icon,
  ParagraphIcon,
  QuoteDownIcon,
  SigmaIcon,
  SmileIcon,
  SquareRootSquareIcon,
  TwitchIcon,
  YoutubeIcon,
} from "@hugeicons/core-free-icons"
import type { Editor } from "@tiptap/react"

import type { IconProp } from "../icons"
import type { RichTextActions } from "./types"

/**
 * A block the `/` menu, the `+` menu and the toolbar's Insert menu can add or
 * turn the current block into. `requires` names the extension it needs, so
 * a command disappears with its feature.
 */
export interface BlockCommand {
  id: string
  label: string
  description: string
  icon: IconProp
  group: "Basic" | "Lists" | "Media" | "Advanced"
  keywords: string[]
  shortcut?: string
  requires?: string
  run: (editor: Editor, actions: RichTextActions) => void
}

export const BLOCK_COMMANDS: BlockCommand[] = [
  {
    id: "paragraph",
    label: "Text",
    description: "Plain paragraph",
    icon: ParagraphIcon,
    group: "Basic",
    keywords: ["paragraph", "body", "normal"],
    shortcut: "Mod-Alt-0",
    run: (editor) => editor.chain().focus().setParagraph().run(),
  },
  ...([1, 2, 3] as const).map((level): BlockCommand => ({
    id: `heading-${level}`,
    label: `Heading ${level}`,
    description: ["Page title", "Section", "Subsection"][level - 1],
    icon: [Heading01Icon, Heading02Icon, Heading03Icon][level - 1],
    group: "Basic",
    keywords: ["heading", "title", `h${level}`],
    shortcut: `Mod-Alt-${level}`,
    requires: "heading",
    run: (editor) => editor.chain().focus().setHeading({ level }).run(),
  })),
  {
    id: "quote",
    label: "Quote",
    description: "Set text apart as a quotation",
    icon: QuoteDownIcon,
    group: "Basic",
    keywords: ["blockquote", "citation"],
    shortcut: "Mod-Shift-b",
    requires: "blockquote",
    run: (editor) => editor.chain().focus().toggleBlockquote().run(),
  },
  {
    id: "code-block",
    label: "Code block",
    description: "Code with syntax highlighting",
    icon: CodeSquareIcon,
    group: "Basic",
    keywords: ["code", "snippet", "pre", "syntax"],
    shortcut: "Mod-Alt-c",
    requires: "codeBlock",
    run: (editor) => editor.chain().focus().toggleCodeBlock().run(),
  },
  {
    id: "divider",
    label: "Divider",
    description: "A horizontal rule between sections",
    icon: MinusSignIcon,
    group: "Basic",
    keywords: ["horizontal rule", "hr", "separator", "line"],
    requires: "horizontalRule",
    run: (editor) => editor.chain().focus().setHorizontalRule().run(),
  },
  {
    id: "bullet-list",
    label: "Bulleted list",
    description: "A simple list",
    icon: LeftToRightListBulletIcon,
    group: "Lists",
    keywords: ["unordered", "ul", "bullets"],
    shortcut: "Mod-Shift-8",
    requires: "bulletList",
    run: (editor) => editor.chain().focus().toggleBulletList().run(),
  },
  {
    id: "ordered-list",
    label: "Numbered list",
    description: "A list with numbers",
    icon: LeftToRightListNumberIcon,
    group: "Lists",
    keywords: ["ordered", "ol", "numbers"],
    shortcut: "Mod-Shift-7",
    requires: "orderedList",
    run: (editor) => editor.chain().focus().toggleOrderedList().run(),
  },
  {
    id: "task-list",
    label: "Checklist",
    description: "Track tasks with checkboxes",
    icon: CheckListIcon,
    group: "Lists",
    keywords: ["todo", "task", "checkbox"],
    shortcut: "Mod-Shift-9",
    requires: "taskList",
    run: (editor) => editor.chain().focus().toggleTaskList().run(),
  },
  {
    id: "image",
    label: "Image",
    description: "Upload or link an image",
    icon: Image01Icon,
    group: "Media",
    keywords: ["picture", "photo", "upload", "img"],
    requires: "image",
    run: (_editor, actions) => actions.openDialog({ kind: "image" }),
  },
  {
    id: "youtube",
    label: "YouTube",
    description: "Embed a YouTube video",
    icon: YoutubeIcon,
    group: "Media",
    keywords: ["video", "embed"],
    requires: "youtube",
    run: (_editor, actions) => actions.openDialog({ kind: "youtube" }),
  },
  {
    id: "twitch",
    label: "Twitch",
    description: "Embed a Twitch stream, video or clip",
    icon: TwitchIcon,
    group: "Media",
    keywords: ["video", "stream", "embed"],
    requires: "twitch",
    run: (_editor, actions) => actions.openDialog({ kind: "twitch" }),
  },
  {
    id: "audio",
    label: "Audio",
    description: "Upload or link an audio file",
    icon: MusicNote01Icon,
    group: "Media",
    keywords: ["sound", "music", "podcast", "mp3"],
    requires: "audio",
    run: (_editor, actions) => actions.openDialog({ kind: "audio" }),
  },
  {
    id: "table",
    label: "Table",
    description: "A 3 × 3 table with a header row",
    icon: GridTableIcon,
    group: "Advanced",
    keywords: ["grid", "rows", "columns", "spreadsheet"],
    requires: "table",
    run: (editor) =>
      editor
        .chain()
        .focus()
        .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
        .run(),
  },
  {
    id: "details",
    label: "Toggle section",
    description: "Content that expands and collapses",
    icon: ListCollapseIcon,
    group: "Advanced",
    keywords: ["details", "collapsible", "accordion", "summary", "expand"],
    requires: "details",
    run: (editor) => editor.chain().focus().setDetails().run(),
  },
  {
    id: "block-math",
    label: "Formula block",
    description: "A centred LaTeX equation",
    icon: SigmaIcon,
    group: "Advanced",
    keywords: ["math", "latex", "katex", "equation"],
    requires: "blockMath",
    run: (_editor, actions) => actions.openDialog({ kind: "blockMath" }),
  },
  {
    id: "inline-math",
    label: "Inline formula",
    description: "LaTeX inside a line of text",
    icon: SquareRootSquareIcon,
    group: "Advanced",
    keywords: ["math", "latex", "katex", "equation"],
    requires: "inlineMath",
    run: (_editor, actions) => actions.openDialog({ kind: "inlineMath" }),
  },
  {
    id: "emoji",
    label: "Emoji",
    description: "Search emoji by name",
    icon: SmileIcon,
    group: "Advanced",
    keywords: ["emoticon", "smiley"],
    requires: "emoji",
    run: (editor) => editor.chain().focus().insertContent(":").run(),
  },
]

/** Whether the editor has the extension or node a command relies on. */
export function hasExtension(editor: Editor, name: string) {
  if (editor.isDestroyed) return false
  return editor.extensionManager.extensions.some(
    (extension) => extension.name === name
  )
}

export function availableCommands(editor: Editor) {
  return BLOCK_COMMANDS.filter(
    (command) => !command.requires || hasExtension(editor, command.requires)
  )
}

/** Matches a `/` query against labels and keywords, best matches first. */
export function filterCommands(commands: BlockCommand[], query: string) {
  const q = query.trim().toLowerCase()
  if (!q) return commands
  const score = (command: BlockCommand) => {
    const label = command.label.toLowerCase()
    if (label.startsWith(q)) return 0
    if (label.split(/\s+/).some((word) => word.startsWith(q))) return 1
    if (command.keywords.some((keyword) => keyword.startsWith(q))) return 2
    if (label.includes(q)) return 3
    return -1
  }
  return commands
    .map((command) => ({ command, score: score(command) }))
    .filter(({ score }) => score >= 0)
    .sort((a, b) => a.score - b.score)
    .map(({ command }) => command)
}
