import { Extension } from "@tiptap/core"
import { PluginKey } from "@tiptap/pm/state"
import { Suggestion } from "@tiptap/suggestion"

import { Kbd } from "@/components/ui/kbd"
import { Icon } from "../icon"
import {
  availableCommands,
  filterCommands,
  type BlockCommand,
} from "./commands"
import { formatShortcut } from "./shortcuts"
import { suggestionRenderer } from "./suggestion"
import type { RichTextActions } from "./types"

const SlashCommandPluginKey = new PluginKey("slashCommand")

export interface SlashCommandOptions {
  getActions: () => RichTextActions
}

/** Typing `/` opens a filterable menu of blocks to insert or switch to. */
export const SlashCommand = Extension.create<SlashCommandOptions>({
  name: "slashCommand",

  addOptions() {
    return {
      getActions: () => {
        throw new Error("SlashCommand needs getActions")
      },
    }
  },

  addProseMirrorPlugins() {
    return [
      Suggestion<BlockCommand, BlockCommand>({
        editor: this.editor,
        pluginKey: SlashCommandPluginKey,
        char: "/",
        // Not inside code, where `/` is just a character.
        allow: ({ editor }) =>
          !editor.isActive("codeBlock") && !editor.isActive("code"),
        items: ({ editor, query }) =>
          filterCommands(availableCommands(editor), query),
        command: ({ editor, range, props }) => {
          editor.chain().focus().deleteRange(range).run()
          props.run(editor, this.options.getActions())
        },
        render: suggestionRenderer<BlockCommand>({
          label: "Insert block",
          empty: "No matching blocks",
          getKey: (command) => command.id,
          getGroup: (command) => command.group,
          renderItem: (command) => (
            <>
              <span className="grid size-7 place-items-center rounded-(--radius-item) border border-border bg-background text-muted-foreground">
                <Icon icon={command.icon} size={15} />
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-foreground">
                  {command.label}
                </span>
                <span className="truncate text-2xs text-muted-foreground">
                  {command.description}
                </span>
              </span>
              {command.shortcut && (
                <Kbd variant="ghost">{formatShortcut(command.shortcut)}</Kbd>
              )}
            </>
          ),
        }),
      }),
    ]
  },
})
