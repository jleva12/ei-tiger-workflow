import * as React from "react"
import { cn } from "cn"

import { AgentOrb } from "@/components/forge/avatars"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import type { KindInfo } from "@/lib/builder/types"
import { BuilderUiContext, type KindTone } from "./ui"

// Spelled out, so Tailwind sees each class.
const TONE_TILES: Record<KindTone, string> = {
  agent: "border-transparent bg-kind-agent text-kind-agent-foreground",
  action: "border-transparent bg-kind-action text-kind-action-foreground",
  logic: "border-transparent bg-kind-logic text-kind-logic-foreground",
}

/**
 * A step's mark: agents wear an agent orb (who, not what); the start and
 * the finish are ink tiles, the ends of the flow; where a person decides,
 * the tile is the warm notice ink; every other step is a hairline tile
 * with its graphite glyph, tinted by its group where the builder asks.
 */
export function KindGlyph({
  info,
  icon = info.icon,
  size = "default",
  className,
}: {
  info: KindInfo
  /** The step's own glyph, when it isn't its kind's. */
  icon?: IconProp
  size?: "default" | "sm"
  className?: string
}) {
  const box = size === "sm" ? "size-6" : "size-7"
  // Read without requiring a builder: run pages draw glyphs too.
  const tone = React.useContext(BuilderUiContext)?.kindTones?.[info.group]
  if (info.orb !== undefined) {
    return (
      <span className={cn("grid shrink-0 place-items-center", box, className)} aria-hidden="true">
        <AgentOrb
          variant={info.orb}
          size="lg"
          className={cn("border-0", size === "sm" ? "size-[1.125rem]" : "size-[1.3125rem]")}
        />
      </span>
    )
  }
  return (
    <span
      aria-hidden="true"
      className={cn(
        "grid shrink-0 place-items-center rounded-(--radius-soft) border",
        box,
        info.terminal
          ? "border-transparent bg-primary text-primary-foreground"
          : info.person
            ? "border-notice-border bg-background text-notice-accent"
            : tone
              ? TONE_TILES[tone]
              : "bg-background text-muted-foreground",
        className
      )}
    >
      <Icon icon={icon} size={size === "sm" ? 13 : 15} />
    </span>
  )
}
