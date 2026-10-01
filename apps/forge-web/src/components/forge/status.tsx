import * as React from "react"
import { cn } from "cn"

import { Badge } from "@/components/ui/badge"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import {
  chipVariants,
  statusBadgeVariants,
  statusLabels,
  toneVars,
  type ChipTone,
  type RunState,
  type SymbolKind,
  type TaskStatus,
  type Tone,
} from "./variants"

/** Compact pastel badge for a task status. Children override the label. */
function StatusBadge({
  status,
  size = "default",
  className,
  children,
  ...props
}: React.ComponentProps<typeof Badge> & {
  status: TaskStatus
  size?: "default" | "lg"
}) {
  return (
    <Badge
      variant="secondary"
      data-status={status}
      className={cn(statusBadgeVariants({ status, size }), className)}
      {...props}
    >
      {children ?? statusLabels[status]}
    </Badge>
  )
}

/**
 * The 14px glyph at the start of a status band: a dashed ring for queued
 * work, a ring with a dot for progress, a spinner for review and a filled
 * check for completion. Inherits `--tone-foreground` from a tone parent.
 */
function StatusSymbol({
  kind = "backlog",
  tone,
  className,
  ...props
}: React.ComponentProps<"span"> & { kind?: SymbolKind; tone?: Tone }) {
  return (
    <span
      data-slot="status-symbol"
      data-kind={kind}
      aria-hidden="true"
      className={cn(
        tone && toneVars[tone],
        "[--symbol:var(--tone-foreground,var(--tone-neutral-foreground))]",
        "flex size-3.5 shrink-0 items-center justify-center rounded-full border-[1.8px] border-dashed border-(--symbol) text-(--symbol)",
        kind === "progress" &&
          "border-2 after:size-1.5 after:rounded-full after:bg-(--symbol) after:content-['']",
        kind === "review" && "border-0",
        kind === "completed" && "border-0 bg-(--symbol) text-background",
        className
      )}
      {...props}
    >
      {kind === "completed" && <Icon icon="check" size={11} />}
      {kind === "review" && <Icon icon="loading" size={17} />}
    </span>
  )
}

/** 6px presence dot: green when live, grey when offline or paused. */
function ConnectionDot({
  online = true,
  className,
  ...props
}: React.ComponentProps<"span"> & { online?: boolean }) {
  return (
    <span
      data-slot="connection-dot"
      data-online={online}
      aria-hidden="true"
      className={cn(
        "inline-block size-1.5 shrink-0 rounded-full",
        online ? "bg-online" : "bg-offline",
        className
      )}
      {...props}
    />
  )
}

/** Monospace count inside a status band. */
function CountBadge({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="count-badge"
      className={cn(
        "h-[18px] min-w-[15px] rounded-(--radius-chip) border bg-background px-[3px] text-center font-mono text-3xs leading-4 text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

/** Small tinted label for results, verdicts, flags and roles. */
function Chip({
  tone = "neutral",
  icon,
  className,
  children,
  ...props
}: React.ComponentProps<typeof Badge> & { tone?: ChipTone; icon?: IconProp }) {
  return (
    <Badge
      variant="secondary"
      data-tone={tone}
      className={cn(chipVariants({ tone }), className)}
      {...props}
    >
      {icon && <Icon icon={icon} data-icon="inline-start" />}
      {children}
    </Badge>
  )
}

const runStateIcon: Record<RunState, IconProp> = {
  running: "loading",
  completed: "completed",
  failed: "failed",
  planned: "clock",
}

/** Coloured glyph for a job, step or log entry state. */
function RunStateIcon({
  state,
  size = 17,
  className,
}: {
  state: RunState
  size?: number
  className?: string
}) {
  return (
    <span
      data-slot="run-state-icon"
      data-state={state}
      className={cn(
        "inline-flex shrink-0 items-center",
        state === "running" && "text-signal-running",
        state === "completed" && "text-signal-success",
        state === "failed" && "text-signal-failed",
        state === "planned" && "text-muted-foreground",
        className
      )}
    >
      <Icon icon={runStateIcon[state]} size={size} />
    </span>
  )
}

export {
  StatusBadge,
  StatusSymbol,
  ConnectionDot,
  CountBadge,
  Chip,
  RunStateIcon,
}
