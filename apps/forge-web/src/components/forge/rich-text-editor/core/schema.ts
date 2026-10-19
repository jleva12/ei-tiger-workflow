import { Extension, type AnyExtension, type JSONContent } from "@tiptap/core"
import { gitHubEmojis } from "@tiptap/extension-emoji"
import type { EmojiOptions } from "@tiptap/extension-emoji"
import { Highlight } from "@tiptap/extension-highlight"
import { Image } from "@tiptap/extension-image"
import { TaskItem, TaskList } from "@tiptap/extension-list"
import { BlockMath, InlineMath } from "@tiptap/extension-mathematics"
import { Mention, type MentionOptions } from "@tiptap/extension-mention"
import {
  Table,
  TableCell,
  TableHeader,
  TableRow,
} from "@tiptap/extension-table"
import { Typography } from "@tiptap/extension-typography"
import { Markdown } from "@tiptap/markdown"
import { Marked, type marked } from "marked"
import type { Node as PMNode } from "@tiptap/pm/model"
import { StarterKit } from "@tiptap/starter-kit"

import type { ResolvedEditorFeatures, UrlPolicy } from "../types.ts"
import {
  EmojiMarkdown,
  MarkdownLiteral,
  SubscriptMarkdown,
  SuperscriptMarkdown,
  markdownFallbacks,
} from "./markdown.ts"
import { defaultUrlPolicy, sanitizeContent } from "./security.ts"
import { EditorLifecycle } from "./lifecycle.ts"
import { SelectionBookmarks } from "./selection-manager.ts"

/** The UI pieces the component plugs into the document extensions. */
export interface DocumentExtensionOptions {
  features: ResolvedEditorFeatures
  urlPolicy?: UrlPolicy
  /** Replaces the plain code block (with the highlighted one, say). */
  codeBlock?: AnyExtension
  /** Turns `@` mentions on, with this suggestion setup. */
  mention?: Partial<MentionOptions>["suggestion"]
  emojiSuggestion?: Partial<EmojiOptions>["suggestion"]
  onMathClick?: (
    kind: "inlineMath" | "blockMath",
    node: PMNode,
    pos: number
  ) => void
  /** Told how many unsafe links and images the initial content lost. */
  onUnsafeContent?: (removed: number) => void
}

/**
 * Cleans the content an editor starts with, once the Markdown extension has
 * parsed it: links and images the URL policy refuses are taken out, and an
 * empty string becomes an empty document (Tiptap would otherwise read it as
 * HTML, which needs a page).
 */
const InitialContent = Extension.create<{
  policy: UrlPolicy
  onUnsafe?: (removed: number) => void
}>({
  name: "initialContent",
  // After the Markdown extension (100), which turns the string into JSON.
  priority: 50,

  onBeforeCreate() {
    const { options } = this.editor
    const content = options.content
    if (typeof content === "string" && !content.trim()) {
      options.content = { type: "doc", content: [{ type: "paragraph" }] }
    } else if (
      content &&
      typeof content === "object" &&
      !Array.isArray(content)
    ) {
      const clean = sanitizeContent(content as JSONContent, this.options.policy)
      options.content = clean.content
      if (clean.removed) this.options.onUnsafe?.(clean.removed)
    }
  },
})

// GFM cells hold one line of inline text; paragraphs in a cell are joined
// with <br> on the way out and split on the way back in.
const MarkdownTableCell = TableCell.extend({ content: "paragraph+" })
const MarkdownTableHeader = TableHeader.extend({ content: "paragraph+" })

/**
 * The extensions that define the document and its Markdown: nodes, marks,
 * history, input rules, literal fallbacks for disabled syntax and selection
 * bookmarks. Everything here works without a page, so it's what the tests
 * load; the component adds its menus and views on top.
 */
export function createDocumentExtensions({
  features,
  urlPolicy = defaultUrlPolicy,
  codeBlock,
  mention,
  emojiSuggestion,
  onMathClick,
  onUnsafeContent,
}: DocumentExtensionOptions): AnyExtension[] {
  const lists = features.bulletList || features.orderedList
  const extensions: AnyExtension[] = [
    StarterKit.configure({
      bold: features.bold ? {} : false,
      italic: features.italic ? {} : false,
      underline: features.underline ? {} : false,
      strike: features.strike ? {} : false,
      code: features.inlineCode ? {} : false,
      link: features.links
        ? {
            openOnClick: false,
            autolink: true,
            linkOnPaste: true,
            defaultProtocol: "https",
            isAllowedUri: (url, context) =>
              context.defaultValidate(url) && urlPolicy(url, "link"),
          }
        : false,
      heading: features.headings ? { levels: [1, 2, 3, 4, 5, 6] } : false,
      blockquote: features.blockquote ? {} : false,
      bulletList: features.bulletList ? {} : false,
      orderedList: features.orderedList ? {} : false,
      listItem: lists ? {} : false,
      listKeymap: lists || features.taskList ? {} : false,
      codeBlock: features.codeBlock && !codeBlock ? {} : false,
      horizontalRule: features.horizontalRule ? {} : false,
      undoRedo: features.history ? { depth: 200 } : false,
      dropcursor: { color: "var(--focus)", width: 2 },
    }),
    Markdown.configure({
      indentation: { style: "space", size: 2 },
      // Tiptap registers each extension's tokenizer on the parser it's
      // given; a parser of its own keeps another editor's syntax (math,
      // mentions, highlights) from leaking into this one.
      marked: new Marked() as unknown as typeof marked,
    }),
    InitialContent.configure({ policy: urlPolicy, onUnsafe: onUnsafeContent }),
    MarkdownLiteral,
    ...markdownFallbacks(features),
    SelectionBookmarks,
    EditorLifecycle,
  ]

  if (features.codeBlock && codeBlock) extensions.push(codeBlock)
  if (features.highlight) extensions.push(Highlight)
  if (features.subscript) extensions.push(SubscriptMarkdown)
  if (features.superscript) extensions.push(SuperscriptMarkdown)
  if (features.typography) extensions.push(Typography)
  if (features.taskList) {
    extensions.push(TaskList, TaskItem.configure({ nested: true }))
  }
  if (features.tables) {
    extensions.push(
      // Column widths have no Markdown, so columns aren't resizable.
      Table.configure({ resizable: false }),
      TableRow,
      MarkdownTableHeader,
      MarkdownTableCell
    )
  }
  if (features.images) {
    extensions.push(
      Image.extend({
        // Pasted or dropped HTML only brings images the URL policy allows.
        parseHTML() {
          return [
            {
              tag: "img[src]",
              getAttrs: (element) =>
                urlPolicy(element.getAttribute("src") ?? "", "image")
                  ? null
                  : false,
            },
          ]
        },
      }).configure({ allowBase64: true })
    )
  }
  if (features.math) {
    // Configured apart, so blocks render in KaTeX display mode.
    extensions.push(
      InlineMath.configure({
        katexOptions: { throwOnError: false },
        onClick: onMathClick
          ? (node, pos) => onMathClick("inlineMath", node, pos)
          : undefined,
      }),
      BlockMath.configure({
        katexOptions: { throwOnError: false, displayMode: true },
        onClick: onMathClick
          ? (node, pos) => onMathClick("blockMath", node, pos)
          : undefined,
      })
    )
  }
  if (features.emoji) {
    extensions.push(
      EmojiMarkdown.configure({
        emojis: gitHubEmojis,
        enableEmoticons: true,
        ...(emojiSuggestion && { suggestion: emojiSuggestion }),
      })
    )
  }
  if (mention) {
    extensions.push(
      Mention.configure({
        HTMLAttributes: { class: "mention" },
        deleteTriggerWithBackspace: true,
        suggestion: mention,
      })
    )
  }
  return extensions
}
