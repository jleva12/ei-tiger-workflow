import * as React from "react"

import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import type { ChatPageChange } from "@/features/agents/lib/api"
import type { ChatPageSettings } from "@/features/agents/lib/page-settings"

type Option = "hide_history" | "hide_top_bar" | "follow_ups"

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
]

/** A page without these settings yet: everything shown, follow-ups on. */
const valueOf = (page: Pick<ChatPageSettings, Option>, key: Option) =>
  key === "follow_ups" ? page.follow_ups !== false : Boolean(page[key])

/**
 * How a chat page looks beyond its conversation: what it leaves out, and
 * whether it suggests follow-up questions. Each switch saves at once.
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
