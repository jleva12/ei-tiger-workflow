import { ComputerIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { FLOATING_CONTROL } from "@/features/agents/components/chat-page-theme-toggle"
import { cn } from "@/lib/utils"

/**
 * Opens a chat page's agent in the Forge Agents desktop app (its
 * `forge-agents://` link), for pages that offer it: in the top bar, or over
 * the page's corner when it has none (`floating`). The app asks before
 * adding the agent.
 */
export function ChatPageDesktopLink({
  href,
  floating = false,
}: {
  href: string
  floating?: boolean
}) {
  const label = "Open in the desktop app"
  return (
    <Button
      variant="ghost"
      size={floating ? "icon-sm" : "icon-xs"}
      aria-label={label}
      title={label}
      data-slot="chat-page-desktop-link"
      nativeButton={false}
      render={<a href={href} />}
      className={cn(floating && FLOATING_CONTROL)}
    >
      <Icon icon={ComputerIcon} />
    </Button>
  )
}
