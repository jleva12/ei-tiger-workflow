import * as React from "react"
import {
  unstable_useSlashCommandAdapter,
  useAui,
  type AssistantClient,
  type Unstable_TriggerItem,
  type Unstable_TriggerMatcher,
} from "@assistant-ui/react"

import { ComposerTriggerPopover } from "@/components/assistant-ui/elements/composer-trigger-popover.aui"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"

import { useAssistantSettings } from "./assistant-context"

/**
 * A `/` command in the composer. What picking it does depends on its fields:
 *
 * - `execute`: runs in the app (start a conversation, open a sheet…); the
 *   typed `/command` is removed.
 * - `prompt`: fills the composer with the prompt, and sends it with `send`.
 * - neither: leaves `/id ` in the composer for the user to add arguments to,
 *   and the agent receives the command as text (handle it in its
 *   instructions).
 */
export type AssistantCommand = {
  /** Typed after the slash: `deploy` is `/deploy`. */
  id: string
  description?: string
  /** A Forge icon name or a Hugeicons glyph. */
  icon?: IconProp
  prompt?: string
  /** With `prompt`: send it straight away instead of leaving it to edit. */
  send?: boolean
  execute?: (aui: AssistantClient) => void
}

// Commands only open from a `/` at the very start of the message, so a path
// or a fraction later in the text never pops the menu.
const atStartOfMessage: Unstable_TriggerMatcher = (text, char, cursor) => {
  if (!text.startsWith(char)) return null
  const query = text.slice(char.length, cursor)
  return /\s/u.test(query) ? null : { query, offset: 0, endOffset: cursor }
}

function runCommand(command: AssistantCommand, aui: AssistantClient) {
  // The popover has removed the typed `/query` by now.
  const rest = aui.composer.getState().text.trimStart()
  if (command.execute) {
    command.execute(aui)
  } else if (command.prompt !== undefined) {
    aui.composer.setText(rest ? `${command.prompt} ${rest}` : command.prompt)
    if (command.send) aui.composer.send()
  } else {
    aui.composer.setText(`/${command.id} ${rest}`)
  }
}

// assistant-ui also matches descriptions; commands whose name starts with the
// query come first, then other name matches, then description matches.
const rank = (item: Unstable_TriggerItem, query: string) => {
  const id = item.id.toLowerCase()
  if (id.startsWith(query)) return 0
  return id.includes(query) ? 1 : 2
}

type TriggerAdapter = ReturnType<
  typeof unstable_useSlashCommandAdapter
>["adapter"]

function byName(adapter: TriggerAdapter): TriggerAdapter {
  return {
    ...adapter,
    search: (query) => {
      const lower = query.toLowerCase()
      return [...(adapter.search?.(query) ?? [])].sort(
        (a, b) => rank(a, lower) - rank(b, lower)
      )
    },
  }
}

type GlyphComponent = React.FC<{ className?: string }>

// One stable component per icon, so the popover's icon map never remounts.
const glyphs = new Map<IconProp, GlyphComponent>()
function glyphFor(icon: IconProp): GlyphComponent {
  let glyph = glyphs.get(icon)
  if (!glyph) {
    glyph = ({ className }) => <Icon icon={icon} className={className} />
    glyphs.set(icon, glyph)
  }
  return glyph
}

/**
 * The `/` command menu above the composer, for `AssistantScreen`'s
 * `commands`. Arrow keys move, Enter or Tab picks, Escape closes.
 */
function AssistantCommandMenu() {
  const { commands = [] } = useAssistantSettings()
  const aui = useAui()
  const slash = unstable_useSlashCommandAdapter({
    commands: commands.map((command) => ({
      id: command.id,
      description: command.description,
      icon: command.id,
      // The popover removes the typed `/query` as it calls this, and the
      // composer state reflects that from the next task.
      execute: () => setTimeout(() => runCommand(command, aui)),
    })),
    removeOnExecute: true,
  })
  const adapter = React.useMemo(() => byName(slash.adapter), [slash.adapter])
  const iconMap = React.useMemo(
    () =>
      Object.fromEntries(
        commands.flatMap((command) =>
          command.icon ? [[command.id, glyphFor(command.icon)]] : []
        )
      ),
    [commands]
  )

  return (
    <ComposerTriggerPopover
      char="/"
      matcher={atStartOfMessage}
      adapter={adapter}
      action={slash.action}
      iconMap={iconMap}
      emptyItemsLabel="No matching commands"
    />
  )
}

export { AssistantCommandMenu }
