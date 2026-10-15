import * as React from "react"

import { cn } from "@/lib/utils"
import {
  accentInk,
  isAccent,
  type ChatPageSettings,
} from "@/features/agents/lib/page-settings"

import "./chat-page-look.css"

/**
 * The region a chat page fills, in the page's look: its theme (as a class,
 * so the theme's tokens and `dark:` styles follow it inside, whatever the
 * console around it shows), its accent (the primary colour, focus ring and,
 * as it says, the visitor's messages), corners, font and width.
 */
export function ChatPageLook({
  settings,
  theme,
  className,
  children,
}: {
  settings: Pick<
    ChatPageSettings,
    "accent" | "bubbles" | "corners" | "font" | "width"
  >
  /** The theme it shows: the page's own, or the visitor's system's. */
  theme: "light" | "dark"
  className?: string
  children: React.ReactNode
}) {
  const accent =
    settings.accent && isAccent(settings.accent) ? settings.accent : undefined
  const style = accent
    ? ({
        "--primary": accent,
        "--primary-foreground": accentInk(accent),
        "--ring": accent,
        "--focus": accent,
        "--chat-accent": accent,
        "--chat-accent-ink": accentInk(accent),
      } as React.CSSProperties)
    : undefined
  return (
    <div
      data-slot="chat-page-look"
      data-bubbles={settings.bubbles}
      data-corners={settings.corners}
      data-font={settings.font}
      data-width={settings.width}
      className={cn(theme, "relative", className)}
      style={style}
    >
      {children}
    </div>
  )
}
