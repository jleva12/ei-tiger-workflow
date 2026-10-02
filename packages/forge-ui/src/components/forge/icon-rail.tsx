import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/** The narrow far-left column of icon shortcuts. Hidden below 600px. */
function IconRail({
  className,
  "aria-label": ariaLabel = "Quick navigation",
  ...props
}: React.ComponentProps<"aside">) {
  return (
    <aside
      data-slot="icon-rail"
      aria-label={ariaLabel}
      className={cn(
        "z-20 flex w-14 shrink-0 flex-col items-center border-r bg-background px-2.5 pb-[18px] @max-[1270px]/shell:w-[50px] @max-[600px]/shell:hidden @min-[1700px]/shell:w-[58px]",
        className
      )}
      {...props}
    />
  )
}

/** The product mark at the top of the rail. */
function AppMark({
  icon = "layers",
  className,
  children,
  ...props
}: React.ComponentProps<"button"> & { icon?: IconProp }) {
  return (
    <button
      type="button"
      data-slot="app-mark"
      className={cn(
        "mb-3.5 flex h-[58px] items-center justify-center text-foreground @min-[1700px]/shell:h-[61px]",
        className
      )}
      {...props}
    >
      {children ?? <Icon icon={icon} size={26} />}
    </button>
  )
}

function RailNav({ className, ...props }: React.ComponentProps<"nav">) {
  return (
    <nav
      data-slot="rail-nav"
      className={cn(
        "flex flex-col items-center gap-2.5 text-rail-foreground",
        className
      )}
      {...props}
    />
  )
}

/** Bottom-aligned group for theme, settings and profile. */
function RailFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="rail-footer"
      className={cn(
        "mt-auto flex flex-col items-center gap-[13px] text-rail-foreground",
        className
      )}
      {...props}
    />
  )
}

/** A 32px ghost icon button with a right-side tooltip label. */
function RailButton({
  icon,
  label,
  active = false,
  className,
  ...props
}: React.ComponentProps<typeof Button> & {
  icon: IconProp
  label: string
  active?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            aria-label={label}
            aria-current={active ? "page" : undefined}
            data-active={active || undefined}
            className={cn(active && "bg-muted text-foreground", className)}
            {...props}
          />
        }
      >
        <Icon icon={icon} size={19} />
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  )
}

export { IconRail, AppMark, RailNav, RailFooter, RailButton }
