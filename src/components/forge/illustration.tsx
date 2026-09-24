import * as React from "react"
import { cn } from "cn"
import { Unlink02Icon } from "@hugeicons/core-free-icons"

import { Icon } from "./icon"
import type { IconProp } from "./icons"

/*
 * Paper illustrations: the workspace's empty-state mark, drawn in CSS. Every
 * state shares one construction — a muted sheet tilted behind a white sheet
 * with a soft paper shadow — and changes only what's written on the front
 * sheet plus one accent: a loose "spark" glyph at the top right or a round
 * status badge pinned to the bottom-right corner.
 */

export type IllustrationName =
  | "tasks"
  | "complete"
  | "waiting"
  | "error"
  | "search"
  | "offline"
  | "locked"
  | "activity"
  | "logs"
  | "changes"
  | "conversation"

/**
 * Paper colours, read by every part as `bg-(--paper)` etc. so the whole
 * drawing can sit on the light workspace or inside the always-dark console.
 */
const surfaces = {
  paper:
    "[--paper:var(--background)] [--paper-back:var(--muted)] [--paper-edge:var(--border)] [--paper-ink:var(--border)] [--paper-terminal:var(--console)] [--paper-terminal-edge:var(--console-border)]",
  console:
    "[--paper:var(--console-hover)] [--paper-back:var(--console-raised)] [--paper-edge:color-mix(in_oklab,var(--console-faint)_55%,transparent)] [--paper-ink:var(--console-control)] [--paper-terminal:var(--console-hover)] [--paper-terminal-edge:var(--paper-edge)]",
}

const line = "block h-1 rounded-xs bg-(--paper-ink)"

/* -------------------------------------------------------------------------- */
/* Sheet contents                                                             */
/* -------------------------------------------------------------------------- */

type RowState = "done" | "failed" | "open"

/** A checklist row: a 13px check box and a line of text. */
function Row({ state, width }: { state: RowState; width: string }) {
  return (
    <>
      {state === "open" ? (
        <span className="size-[13px] rounded-(--radius-chip) border border-(--paper-edge)" />
      ) : (
        <span
          className={cn(
            "flex size-[13px] items-center justify-center rounded-(--radius-chip)",
            state === "done"
              ? "bg-illustration-check text-illustration-check-foreground"
              : "bg-illustration-fail text-illustration-fail-foreground"
          )}
        >
          <Icon icon={state === "done" ? "check" : "close"} size={12} />
        </span>
      )}
      <i className={cn(line, "self-center", width)} />
    </>
  )
}

function Checklist({ rows }: { rows: [RowState, string][] }) {
  return (
    <div className="grid h-full grid-cols-[14px_1fr] content-center gap-x-[7px] gap-y-[9px]">
      {rows.map(([state, width], index) => (
        <Row key={index} state={state} width={width} />
      ))}
    </div>
  )
}

/** A heading bar and three lines of body copy. */
function Document({ className }: { className?: string }) {
  return (
    <div
      className={cn("flex h-full flex-col justify-center gap-[7px]", className)}
    >
      <i className={cn(line, "mb-0.5 h-1.5 w-7")} />
      <i className={cn(line, "w-full")} />
      <i className={cn(line, "w-[88%]")} />
      <i className={cn(line, "w-[62%]")} />
    </div>
  )
}

/** Timeline: a rail of event dots, the last one still to come. */
function Timeline() {
  return (
    <div className="relative grid h-full grid-cols-[8px_1fr] content-center gap-x-2 gap-y-[11px]">
      <i className="absolute top-1/2 left-[3.5px] h-[38px] w-px -translate-y-1/2 bg-(--paper-edge)" />
      <span className="relative size-2 self-center rounded-full bg-event" />
      <i className={cn(line, "w-8 self-center")} />
      <span className="relative size-2 self-center rounded-full bg-event" />
      <i className={cn(line, "w-6 self-center")} />
      <span className="relative size-2 self-center rounded-full border border-(--paper-edge) bg-(--paper)" />
      <i className={cn(line, "w-[18px] self-center opacity-60")} />
    </div>
  )
}

/** A tiny terminal window with a prompt and a blinking caret. */
function Terminal() {
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-[3px] border-b border-console-border px-2 py-1.5">
        <i className="size-[5px] rounded-full bg-console-control" />
        <i className="size-[5px] rounded-full bg-console-control" />
        <i className="size-[5px] rounded-full bg-console-control" />
      </div>
      <div className="grid flex-1 grid-cols-[8px_1fr] content-center items-center gap-x-1 gap-y-2 px-2">
        <Icon icon="right" size={9} className="text-console-success" />
        <i className="block h-1 w-8 rounded-xs bg-console-control" />
        <span />
        <i className="block h-1 w-6 rounded-xs bg-console-control" />
        <Icon icon="right" size={9} className="text-console-success" />
        <i className="block h-2 w-1 animate-pulse bg-console-muted" />
      </div>
    </div>
  )
}

type DiffKind = "context" | "insert" | "delete"

/** Unified diff rows: context, two insertions and a deletion. */
function Diff() {
  const rows: [DiffKind, string][] = [
    ["context", "w-7"],
    ["insert", "w-9"],
    ["insert", "w-6"],
    ["delete", "w-8"],
  ]
  return (
    <div className="flex h-full flex-col justify-center gap-[3px]">
      {rows.map(([kind, width], index) => (
        <div
          key={index}
          className={cn(
            "flex items-center gap-1 rounded-[3px] px-1 py-[3px]",
            kind === "insert" && "bg-diff-insert",
            kind === "delete" && "bg-diff-delete"
          )}
        >
          <span
            className={cn(
              "w-1.5 font-mono text-[8px] leading-none",
              kind === "insert" && "text-diff-added",
              kind === "delete" && "text-diff-removed"
            )}
          >
            {kind === "insert" ? "+" : kind === "delete" ? "−" : ""}
          </span>
          <i
            className={cn(
              "block h-1 rounded-xs",
              width,
              kind === "context" && "bg-(--paper-ink)",
              kind === "insert" && "bg-diff-added/30",
              kind === "delete" && "bg-diff-removed/25"
            )}
          />
        </div>
      ))}
    </div>
  )
}

/** A question, a dark reply and a typing indicator. */
function Conversation() {
  return (
    <div className="flex h-full flex-col justify-center gap-[5px]">
      <span className="flex flex-col gap-1 self-start rounded-[6px] rounded-bl-[2px] bg-(--paper-back) px-1.5 py-[5px]">
        <i className={cn(line, "w-7")} />
        <i className={cn(line, "w-5")} />
      </span>
      <span className="self-end rounded-[6px] rounded-br-[2px] bg-primary px-1.5 py-[5px]">
        <i className="block h-1 w-7 rounded-xs bg-primary-foreground/35" />
      </span>
      <span className="flex gap-[3px] self-start rounded-[6px] rounded-bl-[2px] bg-(--paper-back) px-1.5 py-[5px]">
        <i className="size-1 rounded-full bg-subtle" />
        <i className="size-1 rounded-full bg-subtle/70" />
        <i className="size-1 rounded-full bg-subtle/40" />
      </span>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Accents                                                                    */
/* -------------------------------------------------------------------------- */

/** The loose glyph floating off the sheet's top-right corner. */
function Spark({ icon, className }: { icon: IconProp; className?: string }) {
  return (
    <span
      className={cn(
        "absolute top-0 right-0.5 text-illustration-spark",
        className
      )}
    >
      <Icon icon={icon} size={16} />
    </span>
  )
}

/** A round status chip pinned over the sheet's bottom-right corner. */
function Badge({ className, children }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "absolute top-[64px] left-[74px] flex size-[28px] items-center justify-center rounded-full border-[2.5px] border-(--paper) shadow-(--shadow-paper)",
        className
      )}
    >
      {children}
    </span>
  )
}

/** A padlock drawn in CSS: an open-bottom shackle over a keyed body. */
function Padlock() {
  return (
    <span className="flex flex-col items-center">
      <i className="-mb-px h-[6px] w-[8px] rounded-t-full border-[1.5px] border-b-0 border-current" />
      <i className="flex h-[8px] w-[11px] items-center justify-center rounded-[2px] bg-current">
        <i className="size-[2px] rounded-full bg-primary" />
      </i>
    </span>
  )
}

/** A reading glass resting on the lower-right of the sheet. */
function Lens() {
  return (
    <>
      <span className="absolute top-[50px] left-[56px] size-[32px] rounded-full border-[2.5px] border-muted-foreground bg-(--paper)/65 shadow-(--shadow-paper)">
        <i className="absolute top-[5px] left-[6px] h-[7px] w-[3px] rotate-45 rounded-full bg-(--paper-edge)" />
      </span>
      <i className="absolute top-[85px] left-[82px] h-[4px] w-[13px] rotate-45 rounded-full bg-muted-foreground" />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* States                                                                     */
/* -------------------------------------------------------------------------- */

type Drawing = {
  /** What's written on the front sheet. */
  sheet: React.ReactNode
  /** Extra classes for the front sheet (padding, surface). */
  sheetClassName?: string
  /** Spark, badge or lens drawn over the sheets. */
  accent?: React.ReactNode
}

const drawings: Record<IllustrationName, Drawing> = {
  tasks: {
    sheet: (
      <Checklist
        rows={[
          ["done", "w-8"],
          ["done", "w-8"],
          ["open", "w-[23px]"],
        ]}
      />
    ),
    accent: <Spark icon="plus" />,
  },
  complete: {
    sheet: (
      <Checklist
        rows={[
          ["done", "w-8"],
          ["done", "w-[26px]"],
          ["done", "w-[30px]"],
        ]}
      />
    ),
    accent: <Spark icon="sparkles" />,
  },
  waiting: {
    sheet: (
      <Checklist
        rows={[
          ["open", "w-8"],
          ["open", "w-[26px]"],
          ["open", "w-[20px]"],
        ]}
      />
    ),
    accent: (
      <Badge className="bg-status-running text-status-running-foreground dark:brightness-[.85]">
        <Icon icon="clock" size={14} />
      </Badge>
    ),
  },
  error: {
    sheet: (
      <Checklist
        rows={[
          ["done", "w-8"],
          ["failed", "w-[26px]"],
          ["open", "w-[20px]"],
        ]}
      />
    ),
    accent: <Spark icon="warning" className="text-signal-failed" />,
  },
  search: {
    sheet: <Document />,
    accent: <Lens />,
  },
  offline: {
    sheet: <Document className="opacity-55" />,
    accent: (
      <Badge className="bg-offline text-background">
        <Icon icon={Unlink02Icon} size={14} />
      </Badge>
    ),
  },
  locked: {
    sheet: <Document className="opacity-55 blur-[0.6px]" />,
    accent: (
      <Badge className="bg-primary text-primary-foreground">
        <Padlock />
      </Badge>
    ),
  },
  activity: {
    sheet: <Timeline />,
    accent: <Spark icon="plus" />,
  },
  logs: {
    sheet: <Terminal />,
    sheetClassName:
      "overflow-hidden border-(--paper-terminal-edge) bg-(--paper-terminal) p-0",
  },
  changes: {
    sheet: <Diff />,
    sheetClassName: "px-2",
  },
  conversation: {
    sheet: <Conversation />,
    sheetClassName: "px-2",
    accent: <Spark icon="sparkles" />,
  },
}

/**
 * A paper illustration for an empty, waiting or error state. Decorative:
 * pair it with a title or text that says the same thing.
 */
function Illustration({
  name,
  size = "default",
  surface = "paper",
  className,
}: {
  name: IllustrationName
  /** `sm` draws it at about three-quarters size for panels and table cells. */
  size?: "default" | "sm"
  /** `console` recolours the paper for the always-dark log console. */
  surface?: keyof typeof surfaces
  className?: string
}) {
  const drawing = drawings[name]
  return (
    <div
      aria-hidden="true"
      data-slot="illustration"
      data-name={name}
      className={cn(
        "relative h-[102px] w-[112px] shrink-0",
        surfaces[surface],
        size === "sm" && "[zoom:.72]",
        className
      )}
    >
      <div className="absolute top-[7px] left-3 h-20 w-[82px] [transform:rotate(-12deg)_translate(-8px,1px)] rounded-[10px] border border-(--paper-edge) bg-(--paper-back)" />
      <div
        className={cn(
          "absolute top-[7px] left-3 h-20 w-[82px] rotate-5 rounded-[10px] border border-(--paper-edge) bg-(--paper) p-3 shadow-(--shadow-paper)",
          drawing.sheetClassName
        )}
      >
        {drawing.sheet}
      </div>
      {drawing.accent}
    </div>
  )
}

export { Illustration }
