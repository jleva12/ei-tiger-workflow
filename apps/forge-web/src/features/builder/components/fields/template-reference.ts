import {
  InputRule,
  Node,
  mergeAttributes,
  type JSONContent,
  type MarkdownToken,
} from "@tiptap/core"
import type { Node as PMNode } from "@tiptap/pm/model"
import { NodeSelection, Plugin, PluginKey } from "@tiptap/pm/state"

/*
 * A template's `{{ expression }}` in the rich text editor: one chip, kept
 * out of Markdown's escaping, so `{{ state.user_name }}` is saved exactly as
 * written (never `user\_name`) and the run fills it in as before.
 */

export type ReferenceRequest = {
  /** Where it goes, or where the chip being edited is. */
  pos: number
  /** The chip's expression, when one is being edited. */
  expression?: string
}

export interface TemplateReferenceOptions {
  /** Asks for an expression: `{{` was typed, or a chip was clicked. */
  onPick: (request: ReferenceRequest) => void
}

const REFERENCE = /\{\{([\s\S]*?)\}\}/g

/** Whether a text node is code, where `{{` stays characters. */
function inCode(node: PMNode, parent: PMNode | null) {
  if (parent?.type.spec.code) return true
  return node.marks.some((mark) => mark.type.spec.code)
}

export const TemplateReference = Node.create<TemplateReferenceOptions>({
  name: "templateReference",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addOptions() {
    return { onPick: () => {} }
  },

  addAttributes() {
    return {
      // What's between the braces, spaces included, so it's written back as it came.
      expression: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-expression") ?? "",
        renderHTML: (attributes) => ({
          "data-expression": attributes.expression,
        }),
      },
    }
  },

  parseHTML() {
    return [{ tag: "span[data-template-reference]" }]
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        "data-template-reference": "",
        class: "template-reference",
      }),
      `{{ ${String(node.attrs.expression).trim()} }}`,
    ]
  },

  renderText({ node }) {
    return `{{${node.attrs.expression}}}`
  },

  markdownTokenizer: {
    name: "templateReference",
    level: "inline",
    start: (src: string) => src.indexOf("{{"),
    tokenize: (src: string) => {
      const match = /^\{\{([\s\S]*?)\}\}/.exec(src)
      if (!match) return undefined
      return { type: "templateReference", raw: match[0], text: match[1] }
    },
  },

  parseMarkdown: (token: MarkdownToken): JSONContent => ({
    type: "templateReference",
    attrs: { expression: String(token.text ?? "") },
  }),

  renderMarkdown: (node: JSONContent) => `{{${node.attrs?.expression ?? ""}}}`,

  addInputRules() {
    const { onPick } = this.options
    return [
      // `{{` opens the picker instead of being typed.
      new InputRule({
        find: /\{\{$/,
        handler: ({ state, range }) => {
          state.tr.delete(range.from, range.to)
          const pos = range.from
          setTimeout(() => onPick({ pos }), 0)
        },
      }),
    ]
  },

  addKeyboardShortcuts() {
    return {
      // Enter on a selected chip edits it.
      Enter: ({ editor }) => {
        const { selection } = editor.state
        if (
          !(selection instanceof NodeSelection) ||
          selection.node.type.name !== this.name
        ) {
          return false
        }
        this.options.onPick({
          pos: selection.from,
          expression: String(selection.node.attrs.expression),
        })
        return true
      },
    }
  },

  addProseMirrorPlugins() {
    const type = this.type
    const { onPick } = this.options
    const editor = this.editor
    return [
      new Plugin({
        key: new PluginKey("templateReference"),
        props: {
          handleClickOn: (_view, pos, node) => {
            if (node.type !== type || !editor.isEditable) return false
            onPick({ pos, expression: String(node.attrs.expression) })
            return true
          },
        },
        // Pasted or typed in full, `{{ … }}` becomes a chip too.
        appendTransaction: (transactions, _old, state) => {
          if (!transactions.some((tr) => tr.docChanged)) return null
          const found: { from: number; to: number; expression: string }[] = []
          state.doc.descendants((node, pos, parent) => {
            if (!node.isText || !node.text?.includes("{{")) return
            if (inCode(node, parent)) return
            for (const match of node.text.matchAll(REFERENCE)) {
              const from = pos + (match.index ?? 0)
              found.push({
                from,
                to: from + match[0].length,
                expression: match[1],
              })
            }
          })
          if (!found.length) return null
          const tr = state.tr
          for (const { from, to, expression } of found.reverse()) {
            tr.replaceWith(from, to, type.create({ expression }))
          }
          return tr
        },
      }),
    ]
  },
})
