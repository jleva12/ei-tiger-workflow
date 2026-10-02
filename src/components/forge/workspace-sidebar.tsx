import * as React from "react"
import { mergeProps } from "@base-ui/react/merge-props"
import { useRender } from "@base-ui/react/use-render"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { useAppShell, useOptionalAppShell } from "./app-shell-context"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { ConnectionDot } from "./status"

/**
 * The 224px workspace navigation column. On shells narrower than 1050px it
 * hides, and the same content opens in a left sheet from `SidebarTrigger`.
 */
function WorkspaceSidebar({
  label = "Workspace navigation",
  description = "Navigate the workspace.",
  className,
  children,
}: {
  label?: string
  description?: string
  className?: string
  children: React.ReactNode
}) {
  const { sidebarOpen, mobileNavOpen, setMobileNavOpen } = useAppShell()
  return (
    <>
      <aside
        data-slot="workspace-sidebar"
        data-state={sidebarOpen ? "open" : "closed"}
        aria-label={label}
        className={cn(
          "flex min-h-0 w-56 shrink-0 flex-col overflow-auto border-r bg-background data-[state=closed]:hidden @max-[1270px]/shell:w-[205px] @max-[1050px]/shell:hidden @min-[1700px]/shell:w-60",
          className
        )}
      >
        {children}
      </aside>
      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent
          side="left"
          className="gap-0 overflow-y-auto data-[side=left]:w-[270px] data-[side=left]:sm:max-w-[270px] [&>[data-slot=sheet-close]]:top-[22px]"
        >
          <SheetHeader className="sr-only">
            <SheetTitle>{label}</SheetTitle>
            <SheetDescription>{description}</SheetDescription>
          </SheetHeader>
          {children}
        </SheetContent>
      </Sheet>
    </>
  )
}

/** The 58px brand row aligned with the topbar. */
function SidebarBrand({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-brand"
      className={cn(
        "flex h-[58px] min-h-[58px] items-center gap-[9px] border-b pr-[17px] pl-5 text-sm font-[550] whitespace-nowrap in-data-[slot=sheet-content]:h-[70px] in-data-[slot=sheet-content]:min-h-[70px] in-data-[slot=sheet-content]:pr-[60px] @max-[1270px]/shell:gap-1.5 @max-[1270px]/shell:pl-[15px] @max-[1270px]/shell:text-[0.8125rem] @min-[1700px]/shell:h-[61px] @min-[1700px]/shell:min-h-[61px]",
        className
      )}
      {...props}
    />
  )
}

function SidebarBrandName({
  name,
  suffix,
}: {
  name: React.ReactNode
  suffix?: React.ReactNode
}) {
  return (
    <span className="truncate">
      {name}
      {suffix && <span className="font-[450]"> {suffix}</span>}
    </span>
  )
}

/** Small ghost icon button that sits after the brand name (e.g. a menu trigger). */
function SidebarBrandAction({
  className,
  ...props
}: React.ComponentProps<typeof Button>) {
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      className={cn("-ml-[5px]", className)}
      {...props}
    />
  )
}

function SidebarCollapseButton({
  className,
  ...props
}: React.ComponentProps<typeof Button>) {
  const { setSidebarOpen } = useAppShell()
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label="Collapse sidebar"
      className={cn(
        "ml-auto text-subtle in-data-[slot=sheet-content]:hidden",
        className
      )}
      onClick={() => setSidebarOpen(false)}
      {...props}
    >
      <Icon icon="sidebar" size={16} />
    </Button>
  )
}

function SidebarSection({
  variant = "default",
  className,
  ...props
}: React.ComponentProps<"div"> & {
  variant?: "default" | "primary" | "flush"
}) {
  return (
    <div
      data-slot="sidebar-section"
      data-variant={variant}
      className={cn(
        "border-b px-[15px] pt-3.5 pb-[17px]",
        variant === "primary" && "pt-3 pb-[15px]",
        variant === "flush" && "border-0",
        className
      )}
      {...props}
    />
  )
}

/** Full-width command palette entry with its keyboard shortcut. */
function CommandButton({
  shortcut = "⌘ K",
  className,
  children = "Command",
  ...props
}: React.ComponentProps<typeof Button> & { shortcut?: string }) {
  return (
    <Button
      variant="outline"
      className={cn(
        "mb-3 h-[34px] w-full justify-start gap-3 bg-[color-mix(in_oklch,var(--muted)_45%,var(--background))] text-subtle dark:bg-[color-mix(in_oklch,var(--muted)_45%,var(--background))]",
        className
      )}
      {...props}
    >
      <Icon icon="command" />
      <span>{children}</span>
      <Kbd variant="ghost" className="ml-auto">
        {shortcut}
      </Kbd>
    </Button>
  )
}

/**
 * A sidebar row: icon, label and optional trailing meta (a count, a dot or a
 * chevron). Renders a button by default; pass `render={<a href="…" />}` for links.
 * Selecting a row inside the mobile sheet closes the sheet.
 */
function NavItem({
  icon,
  active = false,
  meta,
  size = "default",
  className,
  children,
  render,
  onClick,
  ...props
}: useRender.ComponentProps<"button"> & {
  icon?: IconProp
  active?: boolean
  meta?: React.ReactNode
  size?: "default" | "sm"
}) {
  const shell = useOptionalAppShell()
  return useRender({
    defaultTagName: "button",
    render,
    props: mergeProps<"button">(
      {
        type: render ? undefined : "button",
        "aria-current": active ? "page" : undefined,
        className: cn(
          "flex min-h-9 w-full items-center gap-3 rounded-(--radius-control) px-2.5 py-[7px] text-left text-sm text-nav-foreground transition-[background-color] duration-150 hover:bg-muted hover:text-foreground data-active:bg-muted data-active:font-medium data-active:text-foreground [&>svg]:shrink-0",
          size === "sm" && "text-xs",
          className
        ),
        onClick: (event: React.MouseEvent<HTMLButtonElement>) => {
          onClick?.(event)
          // An item that opens a menu (a menu trigger's `render`) keeps the
          // mobile nav open, or the menu would close with it; its choices
          // close the nav instead.
          if (!event.currentTarget.hasAttribute("aria-haspopup"))
            shell?.setMobileNavOpen(false)
        },
        children: (
          <>
            {icon && <Icon icon={icon} size={size === "sm" ? 14 : 17} />}
            {children}
            {meta !== undefined && (
              <span className="ml-auto flex items-center text-2xs font-normal text-subtle">
                {meta}
              </span>
            )}
          </>
        ),
      },
      props
    ),
    state: { slot: "nav-item", active },
  })
}

/** Uppercase caption above a group of nav items, with an optional action. */
function NavSectionHeading({
  action,
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & { action?: React.ReactNode }) {
  return (
    <div
      data-slot="nav-section-heading"
      className={cn(
        "flex items-center gap-1.5 px-1 pb-[9px] text-2xs tracking-[.025em] text-subtle uppercase",
        className
      )}
      {...props}
    >
      <Icon icon="down" size={12} />
      <span>{children}</span>
      {action && <span className="ml-auto flex">{action}</span>}
    </div>
  )
}

const projectColors = [
  "bg-project-1",
  "bg-project-2",
  "bg-project-3",
  "bg-project-4",
] as const

/** A repository/project row with a small coloured marker. */
function ProjectItem({
  color = 0,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<typeof NavItem>, "icon" | "size" | "color"> & {
  /** Marker colour index (cycles through four muted hues). */
  color?: number
}) {
  return (
    <NavItem size="sm" className={cn("min-h-8", className)} {...props}>
      <span
        aria-hidden="true"
        className={cn(
          "mx-1 size-2 shrink-0 rounded-[3px]",
          projectColors[Math.abs(color) % projectColors.length]
        )}
      />
      <span className="truncate">{children}</span>
    </NavItem>
  )
}

/** Indented group of nested nav items. */
function SubNav({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sub-nav"
      className={cn("ml-[18px]", className)}
      {...props}
    />
  )
}

function SidebarHint({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="sidebar-hint"
      className={cn(
        "px-2.5 py-[5px] text-2xs leading-[1.7] text-subtle",
        className
      )}
      {...props}
    />
  )
}

/** Sync/connection status pinned to the bottom of the sidebar. */
function SidebarStatus({
  online = true,
  icon = "check",
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & { online?: boolean; icon?: IconProp }) {
  return (
    <div
      data-slot="sidebar-status"
      role="status"
      className={cn(
        "mt-auto flex items-center gap-[7px] px-[19px] py-5 text-3xs whitespace-nowrap text-subtle in-data-[slot=sheet-content]:pb-[23px]",
        className
      )}
      {...props}
    >
      <ConnectionDot online={online} />
      <span>{children}</span>
      <Icon icon={icon} size={13} className="ml-auto" />
    </div>
  )
}

export {
  WorkspaceSidebar,
  SidebarBrand,
  SidebarBrandName,
  SidebarBrandAction,
  SidebarCollapseButton,
  SidebarSection,
  CommandButton,
  NavItem,
  NavSectionHeading,
  ProjectItem,
  SubNav,
  SidebarHint,
  SidebarStatus,
}
