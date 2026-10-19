import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** A control over a chat page's corner, when it has no top bar to hold it. */
export const FLOATING_CONTROL =
  "rounded-full border bg-background/85 shadow-(--shadow-raised) backdrop-blur-sm"

/**
 * Switches a chat page between light and dark, for whoever's reading it: in
 * its top bar, or over its corner when the page has none (`floating`).
 */
export function ChatPageThemeToggle({
  theme,
  onChange,
  floating = false,
}: {
  theme: "light" | "dark"
  onChange: (theme: "light" | "dark") => void
  floating?: boolean
}) {
  const next = theme === "dark" ? "light" : "dark"
  const label = `Switch to the ${next} theme`
  return (
    <Button
      variant="ghost"
      size={floating ? "icon-sm" : "icon-xs"}
      aria-label={label}
      title={label}
      data-slot="chat-page-theme-toggle"
      onClick={() => onChange(next)}
      className={cn(floating && FLOATING_CONTROL)}
    >
      <Icon icon={theme === "dark" ? "sun" : "moon"} />
    </Button>
  )
}
