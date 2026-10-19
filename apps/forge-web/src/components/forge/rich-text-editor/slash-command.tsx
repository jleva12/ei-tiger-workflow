import { Extension } from "@tiptap/core"
import { PluginKey } from "@tiptap/pm/state"
import { Suggestion } from "@tiptap/suggestion"

import { Kbd } from "@/components/ui/kbd"
import { ActionIcon } from "./controls"
import {
  createActionContext,
  type ActionRegistry,
} from "./core/action-registry"
import { formatShortcut } from "./shortcuts"
import { suggestionRenderer } from "./suggestion"
import type { EditorAction, EditorController } from "./types"

const SlashCommandPluginKey = new PluginKey("slashCommand")

export interface SlashCommandOptions {
  registry: ActionRegistry
  controller: EditorController
}

/** Matches a `/` query against labels and keywords, best matches first. */
export function filterActions(actions: EditorAction[], query: string) {
  const q = query.trim().toLowerCase()
  if (!q) return actions
  const score = (action: EditorAction) => {
    const label = action.label.toLowerCase()
    if (label.startsWith(q)) return 0
    if (label.split(/\s+/).some((word) => word.startsWith(q))) return 1
    if (action.keywords?.some((keyword) => keyword.startsWith(q))) return 2
    if (label.includes(q)) return 3
    return -1
  }
  return actions
    .map((action) => ({ action, score: score(action) }))
    .filter(({ score }) => score >= 0)
    .sort((a, b) => a.score - b.score)
    .map(({ action }) => action)
}

/**
 * Typing `/` opens a filterable menu of every action that offers itself
 * there (`slashCommand`): the built-in blocks, AI and the app's own.
 */
export const SlashCommand = Extension.create<SlashCommandOptions>({
  name: "slashCommand",

  addOptions() {
    return {
      registry: null as unknown as ActionRegistry,
      controller: null as unknown as EditorController,
    }
  },

  addProseMirrorPlugins() {
    const { registry, controller } = this.options
    return [
      Suggestion<EditorAction, EditorAction>({
        editor: this.editor,
        pluginKey: SlashCommandPluginKey,
        char: "/",
        // Not inside code, where `/` is just a character.
        allow: ({ editor }) =>
          !editor.isActive("codeBlock") && !editor.isActive("code"),
        items: ({ query }) => {
          const context = createActionContext(controller, "slash")
          const offered = registry.list().filter((action) => {
            if (!action.slashCommand) return false
            const state = registry.state(action.id, context)
            return state.visible && state.enabled
          })
          return filterActions(offered, query)
        },
        command: ({ editor, range, props: action }) => {
          editor.chain().focus().deleteRange(range).run()
          void registry.run(action.id, createActionContext(controller, "slash"))
        },
        render: suggestionRenderer<EditorAction>({
          label: "Insert block",
          empty: "No matching blocks",
          getKey: (action) => action.id,
          getGroup: (action) => action.group,
          renderItem: (action) => (
            <>
              <span className="grid size-7 place-items-center rounded-(--radius-item) border border-border bg-background text-muted-foreground">
                <ActionIcon icon={action.icon} size={15} />
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-foreground">{action.label}</span>
                {action.description && (
                  <span className="truncate text-2xs text-muted-foreground">
                    {action.description}
                  </span>
                )}
              </span>
              {action.shortcut && (
                <Kbd variant="ghost">{formatShortcut(action.shortcut)}</Kbd>
              )}
            </>
          ),
        }),
      }),
    ]
  },
})
