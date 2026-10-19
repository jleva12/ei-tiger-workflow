import { Extension } from "@tiptap/core"
import { Plugin, PluginKey } from "@tiptap/pm/state"

import { insertableContent, parseMarkdown } from "./core/markdown"
import type { UrlPolicy } from "./types"

// Block syntax at a line start, or inline bold, links and code spans.
const MARKDOWN_HINT =
  /^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|\|.*\|\s*$|- \[[ x]\]\s)|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\s]+\)|`[^`\n]+`/m

export interface MarkdownPasteOptions {
  getUrlPolicy: () => UrlPolicy | undefined
  /** Told how many unsafe links and images a paste lost. */
  onUnsafe: (removed: number) => void
}

/**
 * Pasting plain text that looks like Markdown (from an editor, a terminal or
 * a chat) converts it to rich content, through the same parser and URL
 * policy as every other Markdown. Copies that carry HTML paste as HTML,
 * and code blocks keep the text as typed.
 */
export const MarkdownPaste = Extension.create<MarkdownPasteOptions>({
  name: "markdownPaste",

  addOptions() {
    return { getUrlPolicy: () => undefined, onUnsafe: () => {} }
  },

  addProseMirrorPlugins() {
    const editor = this.editor
    const { getUrlPolicy, onUnsafe } = this.options
    return [
      new Plugin({
        key: new PluginKey("markdownPaste"),
        props: {
          handlePaste: (_view, event) => {
            const data = event.clipboardData
            if (
              !data ||
              data.files.length ||
              data.types.includes("text/html")
            ) {
              return false
            }
            const text = data.getData("text/plain")
            if (!text || !MARKDOWN_HINT.test(text)) return false
            if (editor.isActive("codeBlock") || editor.isActive("code")) {
              return false
            }
            const { doc, removed } = parseMarkdown(editor, text, getUrlPolicy())
            if (removed) onUnsafe(removed)
            const { $from, $to } = editor.state.selection
            const inline = $from.parent.isTextblock && $to.parent.isTextblock
            return editor.commands.insertContent(insertableContent(doc, inline))
          },
        },
      }),
    ]
  },
})
