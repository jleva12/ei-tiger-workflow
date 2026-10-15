import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

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
      className={cn(
        floating &&
          "absolute top-3 right-3 z-10 rounded-full border bg-background/85 shadow-(--shadow-raised) backdrop-blur-sm"
      )}
    >
      <Icon icon={theme === "dark" ? "sun" : "moon"} />
    </Button>
  )
}
