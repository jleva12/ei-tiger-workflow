import * as React from "react"
import { cn } from "cn"

import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/** "Changes  +24 −6" totals line. */
function DiffTotals({
  added,
  removed,
  label,
  className,
}: {
  added: number
  removed: number
  label?: React.ReactNode
  className?: string
}) {
  return (
    <span
      data-slot="diff-totals"
      className={cn(
        "flex items-center gap-2.5 text-[0.8125rem] whitespace-nowrap",
        className
      )}
    >
      {label && <span className="text-muted-foreground">{label}</span>}
      <span className="font-medium text-diff-added">+{added}</span>
      <span className="font-medium text-diff-removed">−{removed}</span>
    </span>
  )
}

/** Changes workspace: a file navigator beside the selected file's diff. */
function DiffWorkspace({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="diff-workspace"
      className={cn(
        "grid min-h-0 flex-1 grid-cols-[minmax(240px,575px)_minmax(0,1fr)] grid-rows-[minmax(0,1fr)] gap-[23px] @max-[1250px]/shell:grid-cols-[minmax(220px,475px)_minmax(0,1fr)] @max-[1250px]/shell:gap-[15px] @max-[900px]/shell:grid-cols-1 @max-[900px]/shell:grid-rows-[auto_minmax(0,1fr)] @max-[900px]/shell:gap-3",
        className
      )}
      {...props}
    />
  )
}

function FileNavigator({ className, ...props }: React.ComponentProps<"nav">) {
  return (
    <nav
      data-slot="file-navigator"
      aria-label="Changed files"
      className={cn("flex min-h-0 min-w-0 flex-col", className)}
      {...props}
    />
  )
}

function FileFilter({
  className,
  placeholder = "Find a file…",
  "aria-label": ariaLabel = "Filter changed files",
  ...props
}: React.ComponentProps<"input">) {
  return (
    <InputGroup
      className={cn("h-8 shrink-0 rounded-(--radius-soft)", className)}
    >
      <InputGroupAddon>
        <Icon icon="search" size={15} />
      </InputGroupAddon>
      <InputGroupInput
        aria-label={ariaLabel}
        placeholder={placeholder}
        className="text-sm md:text-sm"
        {...props}
      />
    </InputGroup>
  )
}

/** "CHANGED FILES   4" caption. */
function FileListLabel({
  count,
  className,
  children = "Changed files",
}: {
  count?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "mx-2 mt-[25px] mb-2.5 flex shrink-0 justify-between text-xs tracking-[.6px] text-muted-foreground uppercase @max-[900px]/shell:mx-1 @max-[900px]/shell:mt-1",
        className
      )}
    >
      <span>{children}</span>
      {count !== undefined && <span>{count}</span>}
    </div>
  )
}

function FileList({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="file-list"
      className={cn(
        "min-h-0 flex-1 [scrollbar-gutter:stable] overflow-y-auto overscroll-contain",
        className
      )}
      {...props}
    />
  )
}

/** A changed-file row with its path and +/− counts. */
function ChangedFile({
  path,
  added,
  removed,
  icon = "code",
  active = false,
  className,
  ...props
}: React.ComponentProps<"button"> & {
  path: string
  added?: number
  removed?: number
  icon?: IconProp
  active?: boolean
}) {
  return (
    <button
      type="button"
      data-active={active || undefined}
      aria-current={active ? "true" : undefined}
      className={cn(
        "flex w-full items-start gap-[7px] rounded-(--radius-item) px-2 py-[11px] text-left text-sm transition-colors hover:bg-muted @max-[900px]/shell:border data-active:bg-muted",
        className
      )}
      {...props}
    >
      <Icon
        icon={icon}
        size={16}
        className="mt-0.5 shrink-0 text-muted-foreground"
      />
      <span className="min-w-0 flex-1 leading-[1.5] wrap-anywhere">{path}</span>
      {(added !== undefined || removed !== undefined) && (
        <small className="flex gap-[5px] font-mono text-xs/[1.8]">
          {added !== undefined && (
            <span className="text-diff-added">+{added}</span>
          )}
          {removed !== undefined && (
            <span className="text-diff-removed">−{removed}</span>
          )}
        </small>
      )}
    </button>
  )
}

/** Bordered frame for one file's patch. */
function FileDiff({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="file-diff"
      className={cn(
        "flex h-full min-h-0 flex-col overflow-hidden rounded-(--radius-control) border",
        className
      )}
      {...props}
    />
  )
}

function FileDiffHeader({
  path,
  added,
  removed,
  actions,
  className,
}: {
  path: string
  added?: number
  removed?: number
  actions?: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex min-h-[45px] shrink-0 flex-wrap items-center gap-2 border-b bg-muted px-[13px] py-2 text-xs",
        className
      )}
    >
      <Icon icon="code" size={15} className="shrink-0 text-muted-foreground" />
      <strong className="flex-[1_1_180px] font-mono text-sm font-medium wrap-anywhere">
        {path}
      </strong>
      {added !== undefined && removed !== undefined && (
        <DiffTotals added={added} removed={removed} className="text-xs" />
      )}
      {actions && (
        <div className="ml-auto flex flex-wrap gap-1 whitespace-nowrap *:data-[slot=button]:h-8 *:data-[slot=button]:rounded-(--radius-chip) *:data-[slot=button]:text-xs">
          {actions}
        </div>
      )}
    </div>
  )
}

type DiffLine = {
  type: "context" | "insert" | "delete" | "hunk"
  /** Line number in the original file. */
  old?: number
  /** Line number in the changed file. */
  new?: number
  content: string
}

/** A diff line's side: the original file ("old") or the changed one ("new"). */
type DiffSide = "old" | "new"

// The side a line's content belongs to: deletes are only in the original.
const contentSide = (line: DiffLine): DiffSide =>
  line.type === "delete" ? "old" : "new"

/**
 * GitHub-style unified diff: two gutters, +/− markers and tinted rows.
 *
 * Rows carry `data-line-index` (the line's index in `lines`) and
 * `data-line-type`; gutter cells carry `data-side` and `data-line` (their
 * number), and the content cell those of its own side (new, or old for a
 * delete), so a surrounding handler (a context menu) can tell which line
 * and side an event is on. Hunk rows (`data-line-type="hunk"`) have no
 * sides.
 *
 * `renderGutter` replaces a gutter's number (e.g. with a button);
 * `renderAfterLine` adds a full-width row under a line (comments, a
 * composer) when it returns something. That row stays in view as the diff
 * scrolls sideways.
 */
function DiffView({
  lines,
  wrap = false,
  className,
  renderGutter,
  renderAfterLine,
}: {
  lines: DiffLine[]
  wrap?: boolean
  className?: string
  /** A gutter's content; the line's number on that side by default. */
  renderGutter?: (
    line: DiffLine,
    side: DiffSide,
    index: number
  ) => React.ReactNode
  /** Content for a full-width row right under a line, or nothing. */
  renderAfterLine?: (line: DiffLine, index: number) => React.ReactNode
}) {
  return (
    <div
      data-slot="diff-view"
      className={cn(
        "relative min-h-0 flex-1 overflow-auto overscroll-contain",
        // Lets rows under a line size to the visible width (`100cqw`).
        renderAfterLine && "@container/diff-view",
        className
      )}
    >
      <table
        className={cn(
          "w-full border-collapse font-mono text-[0.8125rem]",
          wrap ? "table-fixed" : "table-auto"
        )}
      >
        <colgroup>
          <col className="w-[5ch]" />
          <col className="w-[5ch]" />
          <col />
        </colgroup>
        <tbody>
          {lines.map((line, index) => {
            if (line.type === "hunk") {
              return (
                <tr key={index} data-line-index={index} data-line-type="hunk">
                  <td
                    colSpan={3}
                    className="bg-[color-mix(in_srgb,var(--primary)_7%,var(--background))] px-3 py-[7px] whitespace-pre-wrap text-muted-foreground"
                  >
                    {line.content}
                  </td>
                </tr>
              )
            }
            const after = renderAfterLine?.(line, index)
            const side = contentSide(line)
            return (
              <React.Fragment key={index}>
                <tr
                  data-line-index={index}
                  data-line-type={line.type}
                  className="leading-[1.7]"
                >
                  {(["old", "new"] as const).map((gutter) => (
                    <td
                      key={gutter}
                      data-side={gutter}
                      data-line={line[gutter]}
                      className={cn(
                        "min-w-[5ch] px-2 text-right align-top text-muted-foreground select-none",
                        line.type === "insert" && "bg-diff-insert-gutter",
                        line.type === "delete" && "bg-diff-delete-gutter"
                      )}
                    >
                      {renderGutter
                        ? renderGutter(line, gutter, index)
                        : line[gutter]}
                    </td>
                  ))}
                  <td
                    data-side={side}
                    data-line={line[side]}
                    className={cn(
                      "relative pr-3.5 pl-6 align-top before:absolute before:top-0 before:left-2 before:select-none",
                      wrap
                        ? "wrap-anywhere whitespace-pre-wrap"
                        : "whitespace-pre",
                      line.type === "insert" &&
                        "bg-diff-insert before:content-['+']",
                      line.type === "delete" &&
                        "bg-diff-delete before:content-['−']"
                    )}
                  >
                    {line.content}
                  </td>
                </tr>
                {after != null && after !== false && (
                  <tr data-slot="diff-line-after" data-after-line-index={index}>
                    <td colSpan={3} className="p-0">
                      <div className="sticky left-0 w-[100cqw] max-w-full font-sans text-sm whitespace-normal">
                        {after}
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export {
  DiffTotals,
  DiffWorkspace,
  FileNavigator,
  FileFilter,
  FileListLabel,
  FileList,
  ChangedFile,
  FileDiff,
  FileDiffHeader,
  DiffView,
  type DiffLine,
  type DiffSide,
}
