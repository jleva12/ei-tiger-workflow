import { Note01Icon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"

/** The note as it will look in the notes panel, in the approval card. */
export function NotePreview({ args }: { args: Record<string, unknown> }) {
  const text = typeof args.text === "string" ? args.text : ""
  return (
    <figure className="flex items-start gap-2.5 rounded-(--radius-soft) border border-border/70 bg-background px-3 py-2.5 shadow-xs">
      <Icon
        icon={Note01Icon}
        size={14}
        className="mt-0.5 shrink-0 text-muted-foreground"
      />
      <blockquote className="text-sm leading-relaxed text-foreground">
        {text}
      </blockquote>
    </figure>
  )
}
