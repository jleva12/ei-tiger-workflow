import * as React from "react"
import { cn } from "cn"

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { TooltipProvider } from "@/components/ui/tooltip"
import { AppShellContext, useAppShell } from "./app-shell-context"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/*
 * Layout: a 56px icon rail, a 224px workspace sidebar and a fluid main panel.
 * The shell is a size container named `shell`; its regions respond to the
 * shell's own width (1700 / 1270 / 1050 / 800 / 600px), so it also works when
 * embedded in a smaller frame.
 */

function AppShell({
  sidebarOpen: sidebarOpenProp,
  defaultSidebarOpen = true,
  onSidebarOpenChange,
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  sidebarOpen?: boolean
  defaultSidebarOpen?: boolean
  onSidebarOpenChange?: (open: boolean) => void
}) {
  const [sidebarOpenState, setSidebarOpenState] =
    React.useState(defaultSidebarOpen)
  const [mobileNavOpen, setMobileNavOpen] = React.useState(false)
  const sidebarOpen = sidebarOpenProp ?? sidebarOpenState
  const setSidebarOpen = React.useCallback(
    (open: boolean) => {
      if (sidebarOpenProp === undefined) setSidebarOpenState(open)
      onSidebarOpenChange?.(open)
    },
    [sidebarOpenProp, onSidebarOpenChange]
  )
  const value = React.useMemo(
    () => ({ sidebarOpen, setSidebarOpen, mobileNavOpen, setMobileNavOpen }),
    [sidebarOpen, setSidebarOpen, mobileNavOpen]
  )
  return (
    <AppShellContext.Provider value={value}>
      <TooltipProvider delay={250}>
        <div
          data-slot="app-shell"
          data-sidebar={sidebarOpen ? "open" : "closed"}
          className={cn(
            "@container/shell flex h-dvh min-w-[360px] overflow-hidden bg-background text-foreground",
            className
          )}
          {...props}
        >
          {children}
        </div>
      </TooltipProvider>
    </AppShellContext.Provider>
  )
}

/** Visually hidden until focused; jumps keyboard users past navigation. */
function SkipLink({ className, ...props }: React.ComponentProps<"a">) {
  return (
    <a
      data-slot="skip-link"
      className={cn(
        "fixed -top-[50px] left-[15px] z-[100] border bg-background px-[15px] py-2 text-xs focus:top-2.5",
        className
      )}
      {...props}
    />
  )
}

function MainPanel({ className, ...props }: React.ComponentProps<"main">) {
  return (
    <main
      data-slot="main-panel"
      className={cn("flex min-h-0 min-w-0 flex-1 flex-col", className)}
      {...props}
    />
  )
}

/** Opens the navigation sheet on narrow shells, or restores a collapsed sidebar. */
function SidebarTrigger() {
  const { sidebarOpen, setSidebarOpen, setMobileNavOpen } = useAppShell()
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="hidden @max-[1050px]/shell:inline-flex"
        aria-label="Open navigation"
        onClick={() => setMobileNavOpen(true)}
      >
        <Icon icon="sidebar" />
      </Button>
      {!sidebarOpen && (
        <Button
          variant="ghost"
          size="icon"
          className="@max-[1050px]/shell:hidden"
          aria-label="Expand sidebar"
          onClick={() => setSidebarOpen(true)}
        >
          <Icon icon="sidebar" />
        </Button>
      )}
    </>
  )
}

/** 58px page header with a subtle bottom divider. */
function Topbar({
  sidebarTrigger = true,
  className,
  children,
  ...props
}: React.ComponentProps<"header"> & {
  /** Render the mobile navigation / expand-sidebar button. */
  sidebarTrigger?: boolean
}) {
  return (
    <header
      data-slot="topbar"
      className={cn(
        "flex min-h-[58px] items-center gap-2.5 border-b px-[22px] @max-[1270px]/shell:px-[18px] @max-[600px]/shell:min-h-14 @max-[600px]/shell:gap-1.5 @max-[600px]/shell:px-3 @min-[1700px]/shell:min-h-[61px]",
        className
      )}
      {...props}
    >
      {sidebarTrigger && <SidebarTrigger />}
      {children}
    </header>
  )
}

/** Breadcrumb trail; ancestor crumbs collapse away on narrow shells. */
function TopbarBreadcrumb({
  className,
  children,
  ...props
}: React.ComponentProps<"nav">) {
  return (
    <Breadcrumb className={cn("min-w-0", className)} {...props}>
      <BreadcrumbList className="flex-nowrap gap-2.5 text-sm whitespace-nowrap text-muted-foreground sm:gap-2.5 @max-[600px]/shell:gap-1.5 @max-[600px]/shell:[&_svg]:hidden @max-[800px]/shell:[&>[data-crumb=ancestor]]:hidden @max-[800px]/shell:[&>[data-slot=breadcrumb-separator]]:hidden">
        {children}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

/** An ancestor crumb: icon plus a link-styled button. */
function TopbarCrumb({
  icon,
  className,
  children,
  ...props
}: React.ComponentProps<"button"> & { icon?: IconProp }) {
  return (
    <BreadcrumbItem data-crumb="ancestor" className="gap-2.5">
      {icon && <Icon icon={icon} />}
      <button
        type="button"
        className={cn("transition-colors hover:text-foreground", className)}
        {...props}
      >
        {children}
      </button>
    </BreadcrumbItem>
  )
}

function TopbarCrumbSeparator() {
  return (
    <BreadcrumbSeparator className="mx-0.5 text-subtle">/</BreadcrumbSeparator>
  )
}

/** The current page title, rendered as the document's h1. */
function TopbarPage({
  icon,
  children,
}: {
  icon?: IconProp
  children: React.ReactNode
}) {
  return (
    <BreadcrumbItem data-crumb="page" className="min-w-0 gap-2.5">
      {icon && <Icon icon={icon} size={15} className="text-foreground" />}
      <h1 className="truncate text-sm font-medium @max-[600px]/shell:text-[0.8125rem]">
        <BreadcrumbPage>{children}</BreadcrumbPage>
      </h1>
    </BreadcrumbItem>
  )
}

function TopbarActions({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="topbar-actions"
      className={cn(
        "ml-auto flex items-center gap-3 @max-[1270px]/shell:gap-2 @max-[600px]/shell:gap-[7px]",
        className
      )}
      {...props}
    />
  )
}

/** Divider-separated slot for header avatars; hidden on narrower shells. */
function TopbarAgents({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="topbar-agents"
      className={cn("border-r pr-[13px] @max-[1270px]/shell:hidden", className)}
      {...props}
    />
  )
}

/** The one dark primary action in the header. Collapses to an icon at 600px. */
function PrimaryAction({
  icon = "addCircle",
  className,
  children,
  ...props
}: React.ComponentProps<typeof Button> & { icon?: IconProp }) {
  return (
    <Button
      className={cn(
        "gap-1.5 px-3 @max-[600px]/shell:w-8 @max-[600px]/shell:px-0",
        className
      )}
      {...props}
    >
      <Icon icon={icon} size={16} />
      <span className="@max-[600px]/shell:sr-only">{children}</span>
    </Button>
  )
}

/** View switcher row below the header: tabs on the left, filters on the right. */
function ViewToolbar({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="view-toolbar"
      className={cn(
        "flex min-h-14 items-center gap-[13px] border-b px-[22px] @max-[1270px]/shell:flex-wrap @max-[1270px]/shell:gap-2 @max-[1270px]/shell:px-[18px] @max-[1270px]/shell:py-[11px] @max-[1050px]/shell:flex-nowrap @max-[800px]/shell:flex-wrap @max-[600px]/shell:px-[13px] @max-[600px]/shell:py-2.5 @min-[1700px]/shell:min-h-[59px]",
        className
      )}
      {...props}
    />
  )
}

function ToolbarFilters({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="toolbar-filters"
      className={cn(
        "ml-auto flex items-center gap-[7px] @max-[1270px]/shell:gap-[5px] @max-[800px]/shell:mt-1 @max-[800px]/shell:w-full @max-[800px]/shell:justify-start @max-[600px]/shell:overflow-x-auto @max-[800px]/shell:[&>:last-child]:ml-auto @max-[600px]/shell:[&>:last-child]:hidden",
        className
      )}
      {...props}
    />
  )
}

/** The scrolling content region of the main panel. */
function PageContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="page-content"
      tabIndex={-1}
      className={cn(
        "relative min-h-0 flex-1 overflow-auto px-[22px] pt-5 pb-[26px] outline-none @max-[1270px]/shell:px-[18px] @max-[800px]/shell:pt-[15px] @max-[600px]/shell:px-[13px] @max-[600px]/shell:pt-3.5 @max-[600px]/shell:pb-5",
        className
      )}
      {...props}
    />
  )
}

/** 34px status footer. The second item is pushed to the right edge. */
function WorkspaceFooter({
  className,
  ...props
}: React.ComponentProps<"footer">) {
  return (
    <footer
      data-slot="workspace-footer"
      className={cn(
        "flex h-[34px] min-h-[34px] items-center gap-4 border-t px-[23px] text-3xs text-subtle *:flex *:items-center *:gap-1.5 @max-[600px]/shell:px-[13px] @max-[600px]/shell:text-4xs [&>*:nth-child(2)]:ml-auto",
        className
      )}
      {...props}
    />
  )
}

/** A keyboard hint for the footer, hidden on narrow shells. */
function FooterShortcut({
  keys,
  children,
}: {
  keys: string
  children: React.ReactNode
}) {
  return (
    <span className="@max-[600px]/shell:hidden!">
      <Kbd variant="outline">{keys}</Kbd>
      {children}
    </span>
  )
}

export {
  AppShell,
  SkipLink,
  MainPanel,
  SidebarTrigger,
  Topbar,
  TopbarBreadcrumb,
  TopbarCrumb,
  TopbarCrumbSeparator,
  TopbarPage,
  TopbarActions,
  TopbarAgents,
  PrimaryAction,
  ViewToolbar,
  ToolbarFilters,
  PageContent,
  WorkspaceFooter,
  FooterShortcut,
}
