import { cva } from "class-variance-authority"

/* -------------------------------------------------------------------------- */
/* Tones                                                                      */
/* -------------------------------------------------------------------------- */

/** Pastel group tones used by status bands, kanban columns and symbols. */
export type Tone = "neutral" | "amber" | "pink" | "green" | "red"

/**
 * Sets `--tone` (band surface) and `--tone-foreground` (symbol colour) on an
 * element so descendants can read them with `bg-(--tone)` / `text-(--tone-foreground)`.
 */
export const toneVars: Record<Tone, string> = {
  neutral:
    "[--tone:var(--tone-neutral)] [--tone-foreground:var(--tone-neutral-foreground)]",
  amber:
    "[--tone:var(--tone-amber)] [--tone-foreground:var(--tone-amber-foreground)]",
  pink: "[--tone:var(--tone-pink)] [--tone-foreground:var(--tone-pink-foreground)]",
  green:
    "[--tone:var(--tone-green)] [--tone-foreground:var(--tone-green-foreground)]",
  red: "[--tone:var(--tone-red)] [--tone-foreground:var(--tone-red-foreground)]",
}

/* -------------------------------------------------------------------------- */
/* Task statuses                                                              */
/* -------------------------------------------------------------------------- */

export type TaskStatus =
  | "pending"
  | "enqueued"
  | "running"
  | "review"
  | "completed"
  | "failed"
  | "cancelled"

export const statusLabels: Record<TaskStatus, string> = {
  pending: "Pending",
  enqueued: "Queued",
  running: "Running",
  review: "Running · Reviewing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
}

export const statusBadgeVariants = cva(
  "border-0 font-[450] dark:brightness-[.85]",
  {
    variants: {
      status: {
        pending: "bg-status-neutral text-status-neutral-foreground",
        cancelled: "bg-status-neutral text-status-neutral-foreground",
        enqueued: "bg-status-queued text-status-queued-foreground",
        running: "bg-status-running text-status-running-foreground",
        review: "bg-status-review text-status-review-foreground",
        completed: "bg-status-success text-status-success-foreground",
        failed: "bg-status-failed text-status-failed-foreground",
      },
      size: {
        default: "h-[19px] px-[7px] py-0.5 text-3xs/[1.2]",
        lg: "h-auto min-h-[23px] px-[7px] py-0.5 text-xs/[1.2]",
      },
    },
    defaultVariants: {
      status: "pending",
      size: "default",
    },
  }
)

/* -------------------------------------------------------------------------- */
/* Group symbols                                                              */
/* -------------------------------------------------------------------------- */

/** The shape drawn at the start of a status band. */
export type SymbolKind =
  "backlog" | "progress" | "review" | "completed" | "failed" | "cancelled"

export const symbolTones: Record<SymbolKind, Tone> = {
  backlog: "neutral",
  progress: "amber",
  review: "pink",
  completed: "green",
  failed: "red",
  cancelled: "neutral",
}

/* -------------------------------------------------------------------------- */
/* Chips: verification results, verdicts, flags, roles                       */
/* -------------------------------------------------------------------------- */

export const chipVariants = cva(
  "h-auto gap-1.5 rounded-(--radius-chip) border-0 px-[7px] py-[3px] text-xs font-normal",
  {
    variants: {
      tone: {
        neutral: "bg-muted text-muted-foreground",
        success: "bg-success-surface text-success-foreground",
        warning: "bg-warning-surface text-warning-foreground",
        danger: "bg-danger-surface text-danger-foreground",
        notice: "bg-notice-chip text-notice-chip-foreground",
        outline:
          "border border-border bg-background px-[5px] py-px text-muted-foreground",
      },
    },
    defaultVariants: {
      tone: "neutral",
    },
  }
)

export type ChipTone = NonNullable<Parameters<typeof chipVariants>[0]>["tone"]

/* -------------------------------------------------------------------------- */
/* Run signals (job, step and log entry states)                               */
/* -------------------------------------------------------------------------- */

export type RunState = "running" | "completed" | "failed" | "planned"
