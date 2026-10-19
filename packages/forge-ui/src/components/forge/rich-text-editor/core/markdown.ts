import {
  Extension,
  Mark,
  type AnyExtension,
  type Editor,
  type JSONContent,
  type MarkdownParseHelpers,
  type MarkdownRendererHelpers,
  type MarkdownToken,
  type MarkdownTokenizer,
} from "@tiptap/core"
import { Emoji } from "@tiptap/extension-emoji"
import { Subscript } from "@tiptap/extension-subscript"
import { Superscript } from "@tiptap/extension-superscript"
// Brings in the Markdown extension's `editor.markdown` and `getMarkdown` types.
import type {} from "@tiptap/markdown"

import type { ResolvedEditorFeatures, UrlPolicy } from "../types.ts"
import { defaultUrlPolicy, sanitizeContent } from "./security.ts"

/**
 * Markdown the editor has no feature for, kept verbatim. It's a code mark to
 * the serializer, so its text goes out exactly as it came in (no escaping),
 * and the next load reads the same syntax back into the same literal.
 */
export const MarkdownLiteral = Mark.create({
  name: "markdownLiteral",
  code: true,
  excludes: "_",
  inclusive: false,

  parseHTML() {
    return [{ tag: "span[data-markdown-literal]" }]
  },

  renderHTML() {
    return [
      "span",
      {
        "data-markdown-literal": "",
        class: "markdown-literal",
        title: "Markdown this editor doesn't format",
      },
      0,
    ]
  },

  renderMarkdown: (node, h) => h.renderChildren(node),
})

const literalText = (text: string): JSONContent => ({
  type: "text",
  text,
  marks: [{ type: "markdownLiteral" }],
})

/** A token's source as a literal block or inline run. */
function literalFor(token: MarkdownToken, level: "block" | "inline") {
  const raw = String(token.raw ?? "").replace(/\n+$/, "")
  if (!raw) return []
  return level === "block"
    ? { type: "paragraph", content: [literalText(raw)] }
    : literalText(raw)
}

/** A parse handler that keeps a token's source as literal text. */
function literalFallback(tokenName: string, level: "block" | "inline") {
  return Extension.create({
    name: `markdownLiteral_${tokenName}`,
    // After every real handler, so it only sees tokens they turned down.
    priority: 1,
    markdownTokenName: tokenName,
    parseMarkdown: (token: MarkdownToken) => literalFor(token, level),
  })
}

const TASK_ITEM = /^\s*[-+*]\s+\[[ xX]\]\s/

/**
 * Without checklists, a `- [ ]` list would otherwise become a plain bulleted
 * list and lose its checkboxes; this claims it first and keeps it literal.
 */
const TaskListLiteral = Extension.create({
  name: "markdownLiteral_taskList",
  priority: 1000,
  markdownTokenName: "list",
  parseMarkdown: (token: MarkdownToken) => {
    const items = (token.items ?? []) as MarkdownToken[]
    const tasks = items.some((item) => TASK_ITEM.test(String(item.raw ?? "")))
    return tasks ? literalFor(token, "block") : []
  },
})

/**
 * Literal fallbacks for the syntax of disabled features: a table in an
 * editor without tables stays as its Markdown source instead of vanishing.
 */
export function markdownFallbacks(
  features: ResolvedEditorFeatures
): AnyExtension[] {
  const fallbacks: [boolean, string, "block" | "inline"][] = [
    [features.headings, "heading", "block"],
    [features.blockquote, "blockquote", "block"],
    [features.codeBlock, "code", "block"],
    [features.bulletList && features.orderedList, "list", "block"],
    [features.horizontalRule, "hr", "block"],
    [features.tables, "table", "block"],
    [features.images, "image", "inline"],
    [features.bold, "strong", "inline"],
    [features.italic, "em", "inline"],
    [features.strike, "del", "inline"],
    [features.inlineCode, "codespan", "inline"],
    [features.links, "link", "inline"],
  ]
  const extensions = fallbacks
    .filter(([enabled]) => !enabled)
    .map(([, token, level]) => literalFallback(token, level))
  if (!features.taskList) extensions.push(TaskListLiteral)
  return extensions
}

/** `<sub>` / `<sup>` in Markdown, which has no syntax of its own for them. */
function htmlTagMarkdown(tag: "sub" | "sup", name: string) {
  const pattern = new RegExp(`^<${tag}>([\\s\\S]+?)</${tag}>`)
  const tokenizer: MarkdownTokenizer = {
    name,
    level: "inline",
    start: (src) => src.indexOf(`<${tag}>`),
    tokenize: (src, _tokens, lexer) => {
      const match = pattern.exec(src)
      if (!match) return undefined
      return {
        type: name,
        raw: match[0],
        text: match[1],
        tokens: lexer.inlineTokens(match[1]),
      }
    },
  }
  return {
    renderMarkdown: (node: JSONContent, h: MarkdownRendererHelpers) =>
      `<${tag}>${h.renderChildren(node)}</${tag}>`,
    parseMarkdown: (token: MarkdownToken, h: MarkdownParseHelpers) =>
      h.applyMark(name, h.parseInline(token.tokens ?? [])),
    markdownTokenizer: tokenizer,
  }
}

export const SubscriptMarkdown = Subscript.extend(
  htmlTagMarkdown("sub", "subscript")
)
export const SuperscriptMarkdown = Superscript.extend(
  htmlTagMarkdown("sup", "superscript")
)

/**
 * Emoji are saved as the emoji character, which every Markdown renderer
 * shows, rather than as a `:shortcode:` only this editor understands.
 */
export const EmojiMarkdown = Emoji.extend({
  renderMarkdown(node: JSONContent) {
    const name = String(node.attrs?.name ?? "")
    const item = this.options.emojis.find((emoji) => emoji.name === name)
    return item?.emoji ?? (name ? `:${name}:` : "")
  },
})

/**
 * Parses Markdown into document JSON for this editor's schema, with links
 * and images the URL policy refuses taken out.
 */
export function parseMarkdown(
  editor: Editor,
  markdown: string,
  policy: UrlPolicy = defaultUrlPolicy
): { doc: JSONContent; removed: number } {
  const manager = editor.markdown
  if (!manager) throw new Error("The editor has no Markdown extension.")
  const parsed = markdown.trim()
    ? manager.parse(markdown)
    : { type: "doc", content: [] }
  const { content, removed } = sanitizeContent(parsed, policy)
  return { doc: content, removed }
}

/**
 * Empty paragraphs serialize as extra blank lines (or `&nbsp;` lines), which
 * Markdown can't hold: they're gone on the next load. So runs of blank lines
 * become one, and blank lines at either end go. Code fences and `$$` blocks
 * keep theirs.
 */
export function tidyBlankLines(markdown: string): string {
  const out: string[] = []
  let fence: string | null = null
  let math = false
  for (const line of markdown.split("\n")) {
    const trimmed = line.trim()
    const marker = /^(`{3,}|~{3,})/.exec(trimmed)?.[1]
    if (fence) {
      if (marker && marker[0] === fence[0] && marker.length >= fence.length) {
        fence = null
      }
    } else if (marker) {
      fence = marker
    } else if (trimmed === "$$") {
      math = !math
    } else if (!math && (trimmed === "" || trimmed === "&nbsp;")) {
      if (out.length === 0 || out[out.length - 1] === "") continue
      out.push("")
      continue
    }
    out.push(line)
  }
  return out.join("\n").replace(/\s+$/, "")
}

/** The document as Markdown; an empty document is `""`. */
export function serializeMarkdown(editor: Editor): string {
  if (editor.isEmpty) return ""
  return tidyBlankLines(editor.getMarkdown())
}

/** Markdown for some blocks (a selection's slice, say). */
export function serializeContent(
  editor: Editor,
  content: JSONContent[]
): string {
  const manager = editor.markdown
  if (!manager || content.length === 0) return ""
  return manager.serialize({ type: "doc", content })
}

/**
 * What to insert for parsed Markdown: a lone paragraph becomes its inline
 * content (so it joins the line it's inserted into), anything else stays
 * blocks.
 */
export function insertableContent(
  doc: JSONContent,
  inline: boolean
): JSONContent[] {
  const blocks = doc.content ?? []
  if (inline && blocks.length === 1 && blocks[0].type === "paragraph") {
    return blocks[0].content ?? []
  }
  return blocks
}

/**
 * Closes what a half-streamed Markdown answer leaves open (code fences and
 * `$$` blocks), so a preview of it renders the way the finished one will.
 */
export function closeOpenMarkdown(markdown: string): string {
  let fence: string | null = null
  let math = false
  for (const line of markdown.split("\n")) {
    const trimmed = line.trim()
    const marker = /^(`{3,}|~{3,})/.exec(trimmed)?.[1]
    if (fence) {
      if (marker && marker[0] === fence[0] && marker.length >= fence.length) {
        fence = null
      }
    } else if (marker) {
      fence = marker
    } else if (trimmed === "$$") {
      math = !math
    }
  }
  if (fence) return `${markdown}\n${fence}`
  if (math) return `${markdown}\n$$`
  return markdown
}
