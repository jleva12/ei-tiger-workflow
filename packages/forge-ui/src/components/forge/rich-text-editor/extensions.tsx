import { Extension, type AnyExtension, type Editor } from "@tiptap/core"
import { CodeBlockLowlight } from "@tiptap/extension-code-block-lowlight"
import type { EmojiItem } from "@tiptap/extension-emoji"
import { FileHandler } from "@tiptap/extension-file-handler"
import { FindAndReplace } from "@tiptap/extension-find-and-replace"
import { InvisibleCharacters } from "@tiptap/extension-invisible-characters"
import {
  CharacterCount,
  Focus,
  Placeholder,
  Selection,
} from "@tiptap/extensions"
import { keydownHandler } from "@tiptap/pm/keymap"
import { Plugin, PluginKey } from "@tiptap/pm/state"
import { ReactNodeViewRenderer } from "@tiptap/react"

import { CodeBlockView } from "./code-block-view"
import {
  createActionContext,
  type ActionRegistry,
} from "./core/action-registry"
import { createDocumentExtensions } from "./core/schema"
import { IMAGE_TYPES } from "./files"
import { lowlight } from "./lowlight"
import { MarkdownPaste } from "./markdown-paste"
import { SlashCommand } from "./slash-command"
import { suggestionRenderer } from "./suggestion"
import type {
  EditorController,
  EditorUiHandlers,
  MentionItem,
  MentionSource,
  ResolvedEditorFeatures,
  UrlPolicy,
} from "./types"

/**
 * Callbacks the extensions call into the component with. The object is
 * stable and reads the latest props itself, so the editor never rebuilds
 * for a new callback.
 */
export interface EditorHandlers {
  ui: () => EditorUiHandlers
  registry: ActionRegistry
  controller: EditorController
  insertFiles: (editor: Editor, files: File[], pos?: number) => void
  placeholder: () => string
  mentions: MentionSource
  urlPolicy: () => UrlPolicy | undefined
  onUnsafeContent: (removed: number) => void
}

export interface ExtensionConfig {
  features: ResolvedEditorFeatures
  maxLength?: number
  /** Whether `@` suggests people (the source itself comes from handlers). */
  mentions: boolean
  urlPolicy?: UrlPolicy
  handlers: EditorHandlers
  /** The app's own extensions, added last. */
  extensions?: AnyExtension[]
}

/**
 * Editor-level keys: ⌘K links, ⌘F finds, ⌘⇧Enter toggles full screen and
 * Escape leaves it. Low priority, so open suggestion menus get Escape first.
 */
const Shortcuts = Extension.create<{ handlers: EditorHandlers }>({
  name: "richTextShortcuts",
  priority: 10,

  addKeyboardShortcuts() {
    const { registry, controller, ui } = this.options.handlers
    const run = (id: string) => {
      if (!registry.get(id)) return false
      void registry.run(id, createActionContext(controller, "shortcut"))
      return true
    }
    return {
      "Mod-k": () => run("link"),
      "Mod-f": () => run("find"),
      "Mod-Shift-Enter": () => run("fullscreen"),
      Escape: () => {
        if (!ui().viewState().fullscreen) return false
        ui().toggleFullscreen(false)
        return true
      },
    }
  },
})

/**
 * Binds the shortcuts of the app's own actions. Built-in actions keep the
 * bindings their extensions own; an app action with the same keys wins.
 */
const ActionShortcuts = Extension.create<{ handlers: EditorHandlers }>({
  name: "actionShortcuts",
  priority: 1000,

  addProseMirrorPlugins() {
    const { registry, controller } = this.options.handlers
    let version = -1
    let handler: ReturnType<typeof keydownHandler> | null = null
    return [
      new Plugin({
        key: new PluginKey("actionShortcuts"),
        props: {
          handleKeyDown: (view, event) => {
            if (registry.getVersion() !== version) {
              version = registry.getVersion()
              const bindings: Record<string, () => boolean> = {}
              for (const action of registry.list()) {
                if (!action.shortcut || registry.isBuiltIn(action.id)) continue
                bindings[action.shortcut] = () => {
                  void registry.run(
                    action.id,
                    createActionContext(controller, "shortcut")
                  )
                  return true
                }
              }
              handler = keydownHandler(bindings)
            }
            return handler?.(view, event) ?? false
          },
        },
      }),
    ]
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

/**
 * Every extension the editor loads: the document's (shared with the
 * tests), the views and suggestion menus that need React, then the tools.
 */
export function createEditorExtensions({
  features,
  maxLength,
  mentions,
  urlPolicy,
  handlers,
  extensions: custom = [],
}: ExtensionConfig): AnyExtension[] {
  const extensions = createDocumentExtensions({
    features,
    urlPolicy,
    onUnsafeContent: handlers.onUnsafeContent,
    codeBlock: features.codeHighlighting
      ? CodeBlockLowlight.extend({
          addNodeView: () => ReactNodeViewRenderer(CodeBlockView),
        }).configure({ lowlight, enableTabIndentation: true, tabSize: 2 })
      : undefined,
    onMathClick: (kind, node, pos) => {
      if (!handlers.controller.isReadOnly()) {
        handlers.ui().openDialog({
          kind,
          pos,
          initial: String(node.attrs.latex ?? ""),
        })
      }
    },
    emojiSuggestion: {
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
    mention: mentions
      ? {
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
        }
      : undefined,
  })

  extensions.push(
    Shortcuts.configure({ handlers }),
    ActionShortcuts.configure({ handlers }),
    Placeholder.configure({
      includeChildren: true,
      placeholder: ({ editor, node }) => {
        if (node.type.name === "heading") return `Heading ${node.attrs.level}`
        if (editor.isEmpty) return handlers.placeholder()
        return features.slashCommands ? "Type / for commands" : ""
      },
    }),
    CharacterCount.configure({ limit: maxLength ?? null }),
    Focus.configure({ className: "has-focus", mode: "shallowest" }),
    Selection.configure({ className: "selection" }),
    MarkdownPaste.configure({
      getUrlPolicy: handlers.urlPolicy,
      onUnsafe: handlers.onUnsafeContent,
    })
  )

  if (features.images) {
    extensions.push(
      FileHandler.configure({
        allowedMimeTypes: IMAGE_TYPES,
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
  if (features.slashCommands) {
    extensions.push(
      SlashCommand.configure({
        registry: handlers.registry,
        controller: handlers.controller,
      })
    )
  }
  if (features.findReplace) {
    extensions.push(FindAndReplace.configure({ injectCSS: false }))
  }
  if (features.invisibleCharacters) {
    extensions.push(InvisibleCharacters.configure({ visible: false }))
  }
  return [...extensions, ...custom]
}
