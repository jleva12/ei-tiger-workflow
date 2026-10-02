import type * as React from "react"
import { useSyncExternalStore } from "react"
import { Cancel01Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { useScreen, type Panel } from "./lib/screen-store"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet"

const subscribeNarrow = (listener: () => void) => {
  const query = window.matchMedia("(max-width: 767px)")
  query.addEventListener("change", listener)
  return () => query.removeEventListener("change", listener)
}
const isNarrow = () => window.matchMedia("(max-width: 767px)").matches

/**
 * A panel beside the chat, shown while `useScreen`'s panel is `panel`: a
 * titled header with a close button, and `children` scrolling below. Render
 * it inside `AssistantScreen` (its `aside`), so it can read the session.
 */
export function SidePanel({
  panel,
  title,
  icon,
  actions,
  className,
  children,
}: {
  panel: Panel
  title: React.ReactNode
  icon: IconProp
  /** Header controls before the close button. */
  actions?: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  const open = useScreen((state) => state.panel === panel)
  const closePanel = useScreen((state) => state.closePanel)
  const narrow = useSyncExternalStore(subscribeNarrow, isNarrow, () => false)
  if (narrow)
    return (
      <Sheet
        open={open}
        onOpenChange={(next) => {
          if (!next) closePanel()
        }}
      >
        <SheetContent className="data-[side=right]:w-[calc(100%-24px)] data-[side=right]:sm:max-w-sm">
          <SheetHeader className="border-b pr-14">
            <SheetTitle>{title}</SheetTitle>
            <SheetDescription className="sr-only">
              Details for this conversation.
            </SheetDescription>
          </SheetHeader>
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
        </SheetContent>
      </Sheet>
    )
  if (!open) return null
  return (
    <aside
      aria-label={typeof title === "string" ? title : panel}
      data-panel={panel}
      className={cn(
        "flex w-72 shrink-0 animate-in flex-col border-s bg-sidebar duration-200 fade-in slide-in-from-right-4",
        className
      )}
    >
      <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
        <Icon
          icon={icon}
          size={15}
          className="shrink-0 text-muted-foreground"
        />
        <h2 className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h2>
        {actions}
        <TooltipIconButton
          tooltip="Close"
          side="bottom"
          className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
          onClick={closePanel}
        >
          <Icon icon={Cancel01Icon} size={14} />
        </TooltipIconButton>
      </header>
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </aside>
  )
}

/** In the top bar: shows or hides a side panel, with an optional count. */
export function PanelButton({
  panel,
  label,
  icon,
  badge,
}: {
  panel: Panel
  label: string
  icon: IconProp
  badge?: React.ReactNode
}) {
  const open = useScreen((state) => state.panel === panel)
  const togglePanel = useScreen((state) => state.togglePanel)
  return (
    <TooltipIconButton
      tooltip={
        open ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`
      }
      side="bottom"
      aria-pressed={open}
      className="h-7 w-auto gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:text-foreground aria-pressed:bg-muted aria-pressed:text-foreground"
      onClick={() => togglePanel(panel)}
    >
      <Icon icon={icon} size={15} />
      {badge !== undefined && <span className="tabular-nums">{badge}</span>}
    </TooltipIconButton>
  )
}
