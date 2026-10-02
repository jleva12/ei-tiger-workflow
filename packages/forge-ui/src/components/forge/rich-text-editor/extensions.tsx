import { Extension, type AnyExtension, type Editor } from "@tiptap/core"
import { Audio } from "@tiptap/extension-audio"
import { CodeBlockLowlight } from "@tiptap/extension-code-block-lowlight"
import {
  Details,
  DetailsContent,
  DetailsSummary,
} from "@tiptap/extension-details"
import { Emoji, gitHubEmojis, type EmojiItem } from "@tiptap/extension-emoji"
import { FileHandler } from "@tiptap/extension-file-handler"
import { FindAndReplace } from "@tiptap/extension-find-and-replace"
import { Highlight } from "@tiptap/extension-highlight"
import { Image } from "@tiptap/extension-image"
import { InvisibleCharacters } from "@tiptap/extension-invisible-characters"
import { TaskItem, TaskList } from "@tiptap/extension-list"
import { BlockMath, InlineMath } from "@tiptap/extension-mathematics"
import { Mention } from "@tiptap/extension-mention"
import { Subscript } from "@tiptap/extension-subscript"
import { Superscript } from "@tiptap/extension-superscript"
import { TableKit } from "@tiptap/extension-table"
import { TextAlign } from "@tiptap/extension-text-align"
import { TextStyleKit } from "@tiptap/extension-text-style"
import { Twitch } from "@tiptap/extension-twitch"
import { Typography } from "@tiptap/extension-typography"
import { UniqueID } from "@tiptap/extension-unique-id"
import { Youtube } from "@tiptap/extension-youtube"
import {
  CharacterCount,
  Focus,
  Placeholder,
  Selection,
} from "@tiptap/extensions"
import { Markdown } from "@tiptap/markdown"
import { ReactNodeViewRenderer } from "@tiptap/react"
import { StarterKit } from "@tiptap/starter-kit"

import { CodeBlockView } from "./code-block-view"
import { AUDIO_TYPES, IMAGE_TYPES } from "./files"
import { lowlight } from "./lowlight"
import { MarkdownPaste } from "./markdown-paste"
import { SlashCommand } from "./slash-command"
import { suggestionRenderer } from "./suggestion"
import type {
  MentionItem,
  MentionSource,
  RichTextActions,
  RichTextFeatures,
} from "./types"

/**
 * Callbacks the extensions call into the component with. The object is
 * stable and reads the latest props itself, so the editor never rebuilds
 * for a new callback.
 */
export interface EditorHandlers {
  actions: RichTextActions
  insertFiles: (editor: Editor, files: File[], pos?: number) => void
  isEditable: () => boolean
  placeholder: () => string
  mentions: MentionSource
  isFullscreen: () => boolean
}

export interface ExtensionConfig {
  features: RichTextFeatures
  maxLength?: number
  /** Whether `@` suggests people (the source itself comes from handlers). */
  mentions: boolean
  handlers: EditorHandlers
}

/**
 * Editor-level keys: ⌘K links, ⌘F finds, ⌘⇧Enter toggles full screen and
 * Escape leaves it. Low priority, so open suggestion menus get Escape first.
 */
const Shortcuts = Extension.create<{
  handlers: EditorHandlers
  features: RichTextFeatures
}>({
  name: "richTextShortcuts",
  priority: 10,

  addKeyboardShortcuts() {
    const { handlers, features } = this.options
    const { actions } = handlers
    return {
      "Mod-k": ({ editor }) => {
        if (!editor.isEditable || editor.isActive("codeBlock")) return false
        actions.openDialog({ kind: "link" })
        return true
      },
      "Mod-f": () => {
        if (!features.findReplace) return false
        actions.toggleFind(true)
        return true
      },
      "Mod-Shift-Enter": () => {
        if (!features.fullscreen) return false
        actions.toggleFullscreen()
        return true
      },
      Escape: () => {
        if (!handlers.isFullscreen()) return false
        actions.toggleFullscreen(false)
        return true
      },
    }
  },
})

function emojiItems(emojis: EmojiItem[], query: string) {
  const q = query.toLowerCase()
  return emojis
    .filter(
      ({ shortcodes, tags }) =>
        shortcodes.some((code) => code.startsWith(q)) ||
        tags.some((tag) => tag.startsWith(q))
    )
    .slice(0, 8)
}

/** Every extension the editor loads, trimmed to the enabled features. */
export function createExtensions({
  features,
  maxLength,
  mentions,
  handlers,
}: ExtensionConfig): AnyExtension[] {
  const extensions: AnyExtension[] = [
    Shortcuts.configure({ handlers, features }),
    StarterKit.configure({
      heading: features.headings ? { levels: [1, 2, 3, 4, 5, 6] } : false,
      // Replaced by the highlighted code block below when that's on.
      codeBlock: features.codeHighlighting ? false : {},
      link: {
        openOnClick: false,
        autolink: true,
        linkOnPaste: true,
        defaultProtocol: "https",
      },
      dropcursor: { color: "var(--focus)", width: 2 },
      undoRedo: { depth: 200 },
    }),
    Placeholder.configure({
      includeChildren: true,
      placeholder: ({ editor, node }) => {
        if (node.type.name === "heading") return `Heading ${node.attrs.level}`
        if (node.type.name === "detailsSummary") return "Summary"
        if (editor.isEmpty) return handlers.placeholder()
        return features.slashCommands ? "Type / for commands" : ""
      },
    }),
    CharacterCount.configure({ limit: maxLength ?? null }),
    Focus.configure({ className: "has-focus", mode: "shallowest" }),
    Selection.configure({ className: "selection" }),
    Markdown,
    MarkdownPaste,
  ]

  if (features.textStyle) {
    extensions.push(TextStyleKit, Highlight.configure({ multicolor: true }))
  }
  if (features.alignment) {
    extensions.push(TextAlign.configure({ types: ["heading", "paragraph"] }))
  }
  if (features.scripts) extensions.push(Subscript, Superscript)
  if (features.typography) extensions.push(Typography)
  if (features.taskLists) {
    extensions.push(TaskList, TaskItem.configure({ nested: true }))
  }
  if (features.tables) {
    extensions.push(TableKit.configure({ table: { resizable: true } }))
  }
  if (features.codeHighlighting) {
    extensions.push(
      CodeBlockLowlight.extend({
        addNodeView: () => ReactNodeViewRenderer(CodeBlockView),
      }).configure({ lowlight, enableTabIndentation: true, tabSize: 2 })
    )
  }
  if (features.images) {
    extensions.push(
      Image.configure({
        allowBase64: true,
        resize: {
          enabled: true,
          directions: ["top-left", "top-right", "bottom-left", "bottom-right"],
          minWidth: 48,
          minHeight: 48,
          alwaysPreserveAspectRatio: true,
        },
      })
    )
  }
  if (features.media) {
    extensions.push(
      Youtube.configure({ nocookie: true, width: 640, height: 360 }),
      Twitch.configure({ width: 640, height: 360 }),
      Audio.configure({ allowBase64: true, controls: true })
    )
  }
  if (features.images || features.media) {
    const allowedMimeTypes = [
      ...(features.images ? IMAGE_TYPES : []),
      ...(features.media ? AUDIO_TYPES : []),
    ]
    extensions.push(
      FileHandler.configure({
        allowedMimeTypes,
        onDrop: (editor, files, pos) =>
          handlers.insertFiles(editor, files, pos),
        // A copy that also carries HTML (an image copied from a web page)
        // pastes through the HTML; only bare files are inserted here.
        onPaste: (editor, files, html) => {
          if (!html) handlers.insertFiles(editor, files)
        },
      })
    )
  }
  if (features.math) {
    const edit =
      (kind: "inlineMath" | "blockMath") =>
      (node: { attrs: Record<string, unknown> }, pos: number) => {
        if (!handlers.isEditable()) return
        handlers.actions.openDialog({
          kind,
          pos,
          initial: String(node.attrs.latex ?? ""),
        })
      }
    // Configured apart, so blocks render in KaTeX display mode.
    extensions.push(
      InlineMath.configure({
        katexOptions: { throwOnError: false },
        onClick: edit("inlineMath"),
      }),
      BlockMath.configure({
        katexOptions: { throwOnError: false, displayMode: true },
        onClick: edit("blockMath"),
      })
    )
  }
  if (features.details) {
    extensions.push(
      Details.configure({
        persist: true,
        HTMLAttributes: { class: "details" },
      }),
      DetailsSummary,
      DetailsContent
    )
  }
  if (features.emoji) {
    extensions.push(
      Emoji.configure({
        emojis: gitHubEmojis,
        enableEmoticons: true,
        suggestion: {
          items: ({ editor, query }) =>
            emojiItems(editor.storage.emoji.emojis, query),
          render: suggestionRenderer<EmojiItem>({
            label: "Emoji",
            empty: "No matching emoji",
            getKey: (item) => item.name,
            renderItem: (item) => (
              <>
                <span className="grid w-5 place-items-center text-base leading-none">
                  {item.emoji ??
                    (item.fallbackImage && (
                      <img src={item.fallbackImage} alt="" className="size-4" />
                    ))}
                </span>
                <span className="truncate">:{item.shortcodes[0]}:</span>
              </>
            ),
          }),
        },
      })
    )
  }
  if (mentions) {
    extensions.push(
      Mention.configure({
        HTMLAttributes: { class: "mention" },
        deleteTriggerWithBackspace: true,
        suggestion: {
          debounce: 150,
          items: ({ query, signal }) => handlers.mentions(query, signal),
          render: suggestionRenderer<MentionItem>({
            label: "People",
            empty: "No one matches",
            getKey: (item) => item.id,
            renderItem: (item) => (
              <span className="flex min-w-0 flex-col">
                <span className="truncate text-foreground">{item.label}</span>
                {item.description && (
                  <span className="truncate text-2xs text-muted-foreground">
                    {item.description}
                  </span>
                )}
              </span>
            ),
          }),
        },
      })
    )
  }
  if (features.slashCommands) {
    extensions.push(
      SlashCommand.configure({ getActions: () => handlers.actions })
    )
  }
  if (features.findReplace) {
    extensions.push(FindAndReplace.configure({ injectCSS: false }))
  }
  if (features.invisibleCharacters) {
    extensions.push(InvisibleCharacters.configure({ visible: false }))
  }
  if (features.uniqueIds) {
    extensions.push(
      UniqueID.configure({ attributeName: "data-id", types: "all" })
    )
  }
  return extensions
}
