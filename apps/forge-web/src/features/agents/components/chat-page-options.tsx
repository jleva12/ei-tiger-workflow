import * as React from "react"

import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import type { ChatPageChange } from "@/features/agents/lib/api"
import type { ChatPageSettings } from "@/features/agents/lib/page-settings"

type Option =
  "hide_history" | "hide_top_bar" | "follow_ups" | "streaming" | "desktop_link"

const OPTIONS: { key: Option; label: string; about: string }[] = [
  {
    key: "hide_history",
    label: "Hide conversation history",
    about:
      "Leaves out the list of the visitor's conversations: the conversation fills the page, and a new one starts from the top bar.",
  },
  {
    key: "hide_top_bar",
    label: "Hide the top bar",
    about:
      "Leaves out the bar over the conversation, with its title (and, with the history hidden, the new-conversation button).",
  },
  {
    key: "follow_ups",
    label: "Suggest follow-up questions",
    about:
      "After each reply, a few questions to ask next, a click each. Writing them takes a model call of its own.",
  },
  {
    key: "streaming",
    label: "Stream replies",
    about:
      "Words appear as the model writes them. Off, each reply arrives once the model is done and is written in word by word.",
  },
  {
    key: "desktop_link",
    label: "Offer the desktop app",
    about:
      "A button that opens the agent in the Forge Agents desktop app, which asks the visitor before adding it. Turn it on when the people you share the page with have the app: without it, the button does nothing.",
  },
]

/** On until they're turned off: a page set before they existed has them. */
const ON_BY_DEFAULT: ReadonlySet<Option> = new Set(["follow_ups", "streaming"])

/** A page without these settings yet: everything shown, follow-ups and streaming on, no desktop app. */
const valueOf = (page: Pick<ChatPageSettings, Option>, key: Option) =>
  ON_BY_DEFAULT.has(key) ? page[key] !== false : Boolean(page[key])

/**
 * How a chat page looks beyond its conversation: what it leaves out,
 * whether it suggests follow-up questions, whether its replies stream, and
 * whether it offers the desktop app. Each switch saves at once.
 */
export function ChatPageOptions({
  page,
  canManage,
  saving,
  onChange,
  bare = false,
}: {
  page: Pick<ChatPageSettings, Option>
  canManage: boolean
  saving: boolean
  onChange: (change: Omit<ChatPageChange, "enabled">) => void
  /** Without the line above it, where it heads a section of its own. */
  bare?: boolean
}) {
  const id = React.useId()
  return (
    <div
      role="group"
      aria-label="What the page shows"
      className={cn("flex flex-col gap-2.5", !bare && "border-t pt-2.5")}
    >
      {OPTIONS.map((option) => (
        <div
          key={option.key}
          className="flex items-start justify-between gap-3"
        >
          <div className="flex min-w-0 flex-col gap-0.5">
            <label
              htmlFor={`${id}-${option.key}`}
              className="text-xs font-medium text-foreground"
            >
              {option.label}
            </label>
            <p className="text-2xs/[1.5] text-muted-foreground">
              {option.about}
            </p>
          </div>
          <Switch
            id={`${id}-${option.key}`}
            className="mt-0.5"
            checked={valueOf(page, option.key)}
            disabled={!canManage || saving}
            onCheckedChange={(on) => onChange({ [option.key]: on })}
          />
        </div>
      ))}
    </div>
  )
}
