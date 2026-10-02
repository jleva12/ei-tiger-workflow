import { Extension } from "@tiptap/core"
import { Plugin, PluginKey } from "@tiptap/pm/state"

// Block syntax at a line start, or inline bold, links and code spans.
const MARKDOWN_HINT =
  /^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|\|.*\|\s*$|- \[[ x]\]\s)|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\s]+\)|`[^`\n]+`/m

/**
 * Pasting plain text that looks like Markdown (from an editor, a terminal or
 * a chat) converts it to rich content. Copies that carry HTML paste as HTML,
 * and code blocks keep the text as typed.
 */
export const MarkdownPaste = Extension.create({
  name: "markdownPaste",

  addProseMirrorPlugins() {
    const editor = this.editor
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
            return editor.commands.insertContent(text, {
              contentType: "markdown",
            })
          },
        },
      }),
    ]
  },
})
