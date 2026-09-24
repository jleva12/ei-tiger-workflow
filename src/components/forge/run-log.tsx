import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { Illustration, type IllustrationName } from "./illustration"
import { ConnectionDot, RunStateIcon } from "./status"
import type { RunState } from "./variants"

/* -------------------------------------------------------------------------- */
/* Layout and job list                                                        */
/* -------------------------------------------------------------------------- */

/** A 220px job list beside the log output (GitHub Actions style). */
function RunLogLayout({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="run-log-layout"
      className={cn(
        "grid min-h-[570px] grid-cols-[220px_minmax(0,1fr)] @max-[1250px]/shell:grid-cols-[180px_minmax(0,1fr)] @max-[900px]/shell:grid-cols-1",
        className
      )}
      {...props}
    />
  )
}

function JobsSidebar({ className, ...props }: React.ComponentProps<"aside">) {
  return (
    <aside
      data-slot="jobs-sidebar"
      className={cn(
        "border-r px-3 py-6 @max-[900px]/shell:flex @max-[900px]/shell:flex-wrap @max-[900px]/shell:gap-[7px] @max-[900px]/shell:border-r-0 @max-[900px]/shell:border-b @max-[900px]/shell:px-5 @max-[900px]/shell:py-3",
        className
      )}
      {...props}
    />
  )
}

/** Uppercase caption row, e.g. "WORKFLOW  #3" or "AGENT JOBS". */
function JobsLabel({
  trailing,
  className,
  children,
}: {
  trailing?: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "flex justify-between px-[11px] pt-[26px] pb-[9px] text-xs tracking-[.65px] text-muted-foreground uppercase first:pt-0 first:pb-[15px] @max-[900px]/shell:hidden",
        className
      )}
    >
      <span>{children}</span>
      {trailing && <span>{trailing}</span>}
    </div>
  )
}

/**
 * A job row: state icon, name with detail lines, optional count. Planned
 * jobs render as inert, dimmed rows.
 */
function JobButton({
  state,
  icon,
  name,
  details,
  count,
  active = false,
  className,
  ...props
}: React.ComponentProps<"button"> & {
  state?: RunState
  icon?: IconProp
  name: React.ReactNode
  details?: React.ReactNode[]
  count?: React.ReactNode
  active?: boolean
}) {
  const planned = state === "planned"
  const content = (
    <>
      {state ? (
        <RunStateIcon state={state} size={planned ? 16 : 17} />
      ) : (
        icon && <Icon icon={icon} size={16} className="shrink-0" />
      )}
      <span className="min-w-0 flex-1 wrap-anywhere">
        {name}
        {details?.map((detail, index) => (
          <small
            key={index}
            className="mt-1 block text-xs font-normal text-muted-foreground @max-[900px]/shell:hidden"
          >
            {detail}
          </small>
        ))}
      </span>
      {count !== undefined && (
        <span className="ml-auto text-xs font-normal">{count}</span>
      )}
    </>
  )
  const classes = cn(
    "flex w-full items-center gap-[9px] rounded-(--radius-soft) px-2.5 py-[11px] text-left text-sm text-muted-foreground transition-colors @max-[900px]/shell:w-auto @max-[900px]/shell:border @max-[900px]/shell:py-2 @max-[900px]/shell:text-[0.8125rem]",
    planned
      ? "opacity-65 @max-[900px]/shell:hidden"
      : "hover:bg-muted data-active:bg-muted data-active:font-medium data-active:text-foreground",
    className
  )
  if (planned)
    return (
      <div data-slot="job-button" data-state="planned" className={classes}>
        {content}
      </div>
    )
  return (
    <button
      type="button"
      data-slot="job-button"
      data-state={state}
      data-active={active || undefined}
      aria-pressed={active}
      className={classes}
      {...props}
    >
      {content}
    </button>
  )
}

/** Key figures under the job list (started, duration, usage, cost). */
function RunStats({ className, ...props }: React.ComponentProps<"dl">) {
  return (
    <dl
      data-slot="run-stats"
      className={cn(
        "mx-[11px] mt-7 grid grid-cols-[1fr_auto] gap-x-2 gap-y-[15px] border-t pt-[21px] text-xs @max-[900px]/shell:hidden",
        className
      )}
      {...props}
    />
  )
}

function RunStat({
  label,
  children,
}: {
  label: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-[450] tabular-nums">{children}</dd>
    </>
  )
}

/** The log pane column. */
function LogPane({ className, ...props }: React.ComponentProps<"section">) {
  return (
    <section
      data-slot="log-pane"
      className={cn(
        "min-w-0 px-[27px] pt-[25px] pb-[30px] @max-[1250px]/shell:px-5 @max-[1250px]/shell:py-6 @max-[600px]/shell:px-3 @max-[600px]/shell:py-5",
        className
      )}
      {...props}
    />
  )
}

/** Job title, model/outcome line and actions above the console. */
function LogPaneHeading({
  title,
  state,
  subtitle,
  actions,
  className,
}: {
  title: React.ReactNode
  state?: RunState
  subtitle?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "mb-[22px] flex items-center justify-between gap-[15px] @max-[600px]/shell:items-start",
        className
      )}
    >
      <div className="min-w-0">
        <h2 className="mb-[7px] text-lg font-[550] tracking-[-.3px]">
          {title}
        </h2>
        {subtitle && (
          <p className="flex items-center gap-1.5 text-[0.8125rem] wrap-anywhere text-muted-foreground">
            {state && <RunStateIcon state={state} size={14} />}
            {subtitle}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-2 *:data-[slot=button]:rounded-(--radius-soft)">
          {actions}
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Console                                                                    */
/* -------------------------------------------------------------------------- */

/** Always-dark terminal surface for streamed agent output. */
function LogConsole({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="log-console"
      className={cn(
        "overflow-hidden rounded-(--radius-card) border border-console-border bg-console text-console-foreground scrollbar-dark",
        className
      )}
      {...props}
    />
  )
}

function LogToolbar({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="log-toolbar"
      className={cn(
        "flex h-11 items-center gap-[13px] border-b border-console-border bg-console-raised px-3.5 @max-[600px]/shell:gap-[7px] @max-[600px]/shell:px-2",
        className
      )}
      {...props}
    />
  )
}

/** Borderless search field for the console toolbar. */
function LogSearch({
  className,
  "aria-label": ariaLabel = "Search logs",
  placeholder = "Search logs…",
  ...props
}: React.ComponentProps<"input">) {
  return (
    <label className="flex flex-1 items-center gap-[7px] text-console-muted">
      <Icon icon="search" size={15} className="shrink-0" />
      <Input
        aria-label={ariaLabel}
        placeholder={placeholder}
        className={cn(
          "h-[31px] rounded-none border-0 bg-transparent p-0 text-sm text-console-foreground shadow-none placeholder:text-console-muted focus-visible:ring-0 md:text-sm dark:bg-transparent",
          className
        )}
        {...props}
      />
    </label>
  )
}

function LogToolbarText({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "text-xs whitespace-nowrap text-console-muted @max-[600px]/shell:hidden",
        className
      )}
      {...props}
    />
  )
}

/** Ghost button tuned for the dark toolbar; `aria-pressed` turns it green. */
function LogToolbarButton({
  className,
  ...props
}: React.ComponentProps<typeof Button>) {
  return (
    <Button
      variant="ghost"
      size="sm"
      className={cn(
        "rounded-(--radius-item) text-xs text-console-text hover:bg-console-control hover:text-console-foreground aria-pressed:text-console-success dark:hover:bg-console-control",
        className
      )}
      {...props}
    />
  )
}

/** The scrolling log body. Keyboard-focusable so it can be scrolled with keys. */
function LogScroll({
  className,
  "aria-label": ariaLabel = "Log output",
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="log-scroll"
      tabIndex={0}
      role="region"
      aria-label={ariaLabel}
      className={cn(
        "h-[min(56vh,700px)] min-h-[340px] overflow-auto pt-[9px] pb-[35px] [overflow-anchor:none] focus-visible:outline-offset-[-2px]! @max-[600px]/shell:h-[53vh]",
        className
      )}
      {...props}
    />
  )
}

const logKindIcon = {
  event: "activity",
  tool: "code",
  message: "message",
} as const

/**
 * A collapsible log line: number, chevron, kind glyph, title, status and
 * time. Children are the entry's output, shown when expanded.
 */
function LogEntry({
  number,
  kind = "event",
  status,
  title,
  time,
  defaultOpen,
  className,
  children,
}: {
  number?: React.ReactNode
  kind?: keyof typeof logKindIcon
  status?: string
  title: React.ReactNode
  time?: React.ReactNode
  defaultOpen?: boolean
  className?: string
  children?: React.ReactNode
}) {
  return (
    <details
      data-slot="log-entry"
      open={defaultOpen}
      className={cn("group/entry font-mono text-sm/[1.7]", className)}
    >
      <summary className="flex min-h-8 cursor-pointer list-none flex-wrap items-center gap-[9px] py-[5px] pr-[17px] hover:bg-console-hover @max-[600px]/shell:gap-1.5 @max-[600px]/shell:pr-[9px] [&::-webkit-details-marker]:hidden">
        <span className="w-[42px] shrink-0 text-right text-xs text-console-faint select-none @max-[600px]/shell:w-[34px]">
          {number}
        </span>
        <Icon
          icon="right"
          size={12}
          className="transition-transform duration-150 group-open/entry:rotate-90"
        />
        <span className="flex items-center text-console-muted">
          <Icon icon={logKindIcon[kind]} size={14} />
        </span>
        <strong className="min-w-0 flex-1 font-[450] wrap-anywhere">
          {title}
        </strong>
        {status && (
          <span
            className={cn(
              "text-xs text-console-muted",
              status === "completed" && "text-console-success",
              status === "failed" && "text-console-danger",
              status === "running" && "text-console-warning"
            )}
          >
            {status}
          </span>
        )}
        {time && (
          <time className="ml-auto text-xs whitespace-nowrap text-console-dim">
            {time}
          </time>
        )}
      </summary>
      {children && (
        <pre
          className={cn(
            "m-0 pt-1 pr-6 pb-[15px] pl-[65px] text-sm/[1.7] wrap-anywhere whitespace-pre-wrap text-console-text @max-[600px]/shell:pl-[41px]",
            kind === "message" && "text-console-foreground",
            status === "failed" && "text-console-danger"
          )}
        >
          {children}
        </pre>
      )}
    </details>
  )
}

/** Centered console message; `illustration` draws dark paper instead of the icon. */
function LogEmpty({
  icon = "activity",
  illustration,
  title,
  children,
}: {
  icon?: IconProp
  illustration?: IllustrationName
  title: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <div className="px-[25px] py-[75px] text-center text-console-muted">
      {illustration ? (
        <Illustration
          name={illustration}
          size="sm"
          surface="console"
          className="mx-auto mb-[18px]"
        />
      ) : (
        <Icon icon={icon} size={26} className="mx-auto mb-[18px]" />
      )}
      <h3 className="mb-2 text-sm font-medium text-console-foreground">
        {title}
      </h3>
      {children && (
        <p className="mx-auto max-w-[350px] text-sm/[1.7]">{children}</p>
      )}
    </div>
  )
}

/** Connection state on the left, a secondary note on the right. */
function LogFooter({
  online = true,
  trailing,
  className,
  children,
}: {
  online?: boolean
  trailing?: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      data-slot="log-footer"
      className={cn(
        "flex items-center justify-between gap-2 border-t border-console-border px-[15px] py-[11px] text-xs text-console-muted",
        className
      )}
    >
      <span className="flex items-center gap-[7px]">
        <ConnectionDot online={online} />
        {children}
      </span>
      {trailing && (
        <span className="@max-[600px]/shell:hidden">{trailing}</span>
      )}
    </div>
  )
}

export {
  RunLogLayout,
  JobsSidebar,
  JobsLabel,
  JobButton,
  RunStats,
  RunStat,
  LogPane,
  LogPaneHeading,
  LogConsole,
  LogToolbar,
  LogSearch,
  LogToolbarText,
  LogToolbarButton,
  LogScroll,
  LogEntry,
  LogEmpty,
  LogFooter,
}
