import * as React from "react"
import { cn } from "cn"

import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { ConnectionDot } from "./status"

/** A readable-width page for secondary views (activity, dashboards). */
function WorkspaceView({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="workspace-view"
      className={cn(
        "max-w-[920px] px-4 py-5 @max-[600px]/shell:px-1 @max-[600px]/shell:py-3",
        className
      )}
      {...props}
    />
  )
}

/** Icon + 21px title, with a muted description underneath. */
function ViewHeading({
  icon,
  title,
  description,
  className,
}: {
  icon?: IconProp
  title: React.ReactNode
  description?: React.ReactNode
  className?: string
}) {
  return (
    <header data-slot="view-heading" className={cn("mb-[26px]", className)}>
      <div className="mb-2 flex items-center gap-2.5">
        {icon && <Icon icon={icon} />}
        <h1 className="text-[1.3125rem] font-medium tracking-[-.5px]">
          {title}
        </h1>
      </div>
      {description && (
        <p className="text-xs text-muted-foreground">{description}</p>
      )}
    </header>
  )
}

/** Vertical event timeline. */
function EventList({ className, ...props }: React.ComponentProps<"ol">) {
  return (
    <ol
      data-slot="event-list"
      className={cn("flex list-none flex-col gap-[22px] p-0", className)}
      {...props}
    />
  )
}

/** One event: a status dot, the event type, a timestamp, message and reference. */
function EventItem({
  type,
  time,
  error = false,
  reference,
  className,
  children,
  ...props
}: React.ComponentProps<"li"> & {
  type: React.ReactNode
  time?: React.ReactNode
  error?: boolean
  /** A monospace reference (task ID, run ID…). */
  reference?: React.ReactNode
}) {
  return (
    <li
      data-slot="event-item"
      className={cn("flex items-start gap-[13px]", className)}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          "mt-[5px] size-[7px] shrink-0 rounded-full",
          error ? "bg-event-error" : "bg-event"
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 @max-[600px]/shell:flex-col">
          <strong className="text-xs font-medium capitalize">{type}</strong>
          {time && <time className="text-3xs text-subtle">{time}</time>}
        </div>
        {children && (
          <p className="mt-[5px] text-2xs/[1.7] wrap-anywhere text-muted-foreground">
            {children}
          </p>
        )}
        {reference && <code className="text-3xs text-subtle">{reference}</code>}
      </div>
    </li>
  )
}

/** Queue / service header row: presence dot, name, state on the right. */
function StatusHeading({
  online = true,
  title,
  status,
  className,
}: {
  online?: boolean
  title: React.ReactNode
  status?: React.ReactNode
  className?: string
}) {
  return (
    <div
      data-slot="status-heading"
      className={cn(
        "flex items-center gap-2.5 border-t py-[22px] text-xs",
        className
      )}
    >
      <ConnectionDot online={online} />
      <strong className="font-medium">{title}</strong>
      {status && (
        <span className="ml-auto text-2xs text-muted-foreground">{status}</span>
      )}
    </div>
  )
}

/** Hairline grid of large numeric stats. */
function StatGrid({ className, ...props }: React.ComponentProps<"dl">) {
  return (
    <dl
      data-slot="stat-grid"
      className={cn(
        "grid grid-cols-3 border-t border-l @max-[600px]/shell:grid-cols-2",
        className
      )}
      {...props}
    />
  )
}

function Stat({
  label,
  value,
  className,
}: {
  label: React.ReactNode
  value: React.ReactNode
  className?: string
}) {
  return (
    <div
      data-slot="stat"
      className={cn(
        "border-r border-b p-6 @max-[600px]/shell:p-[18px]",
        className
      )}
    >
      <dt className="text-xs text-muted-foreground capitalize">{label}</dt>
      <dd className="mt-3 text-[1.75rem] font-[450] tabular-nums">{value}</dd>
    </div>
  )
}

/** Stack of small muted notes, each optionally led by an icon. */
function Footnotes({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="footnotes"
      className={cn(
        "mt-[27px] flex flex-col gap-3.5 text-2xs text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

function Footnote({
  icon,
  className,
  children,
}: {
  icon?: IconProp
  className?: string
  children: React.ReactNode
}) {
  return (
    <p className={cn("flex items-center gap-2", className)}>
      {icon && <Icon icon={icon} />}
      {children}
    </p>
  )
}

export {
  WorkspaceView,
  ViewHeading,
  EventList,
  EventItem,
  StatusHeading,
  StatGrid,
  Stat,
  Footnotes,
  Footnote,
}
