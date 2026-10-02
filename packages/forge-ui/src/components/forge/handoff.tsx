import * as React from "react"
import { cn } from "cn"

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Icon } from "./icon"
import { Chip, RunStateIcon } from "./status"
import type { ChipTone, RunState } from "./variants"

/* -------------------------------------------------------------------------- */
/* Verification                                                               */
/* -------------------------------------------------------------------------- */

type VerificationResult =
  "passed" | "failed" | "skipped" | "not_run" | "unable_to_verify"

const verificationTone: Record<VerificationResult, ChipTone> = {
  passed: "success",
  failed: "danger",
  skipped: "neutral",
  not_run: "neutral",
  unable_to_verify: "warning",
}

const verificationLabel: Record<VerificationResult, string> = {
  passed: "Passed",
  failed: "Failed",
  skipped: "Skipped",
  not_run: "Not run",
  unable_to_verify: "Unable to verify",
}

/**
 * Result chip for a check. "Unable to verify" is amber and never styled as a
 * pass or an observed failure.
 */
function VerificationStatus({
  result,
  withIcon = false,
  className,
  children,
}: {
  result: VerificationResult
  /** Lead with a check / cross glyph (used on round summaries). */
  withIcon?: boolean
  className?: string
  children?: React.ReactNode
}) {
  return (
    <Chip
      tone={verificationTone[result]}
      icon={
        !withIcon
          ? undefined
          : result === "passed"
            ? "completed"
            : result === "failed"
              ? "failed"
              : undefined
      }
      className={cn(
        result === "unable_to_verify" && "max-w-[140px] whitespace-normal",
        className
      )}
    >
      {children ?? verificationLabel[result]}
    </Chip>
  )
}

/** Tinted block summarising one verification pass and its commands. */
function VerificationRound({
  title,
  result,
  meta,
  note,
  className,
  children,
}: {
  title: React.ReactNode
  result?: VerificationResult
  meta?: React.ReactNode
  note?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="verification-round"
      className={cn(
        "border-b bg-[color-mix(in_srgb,var(--muted)_55%,var(--background))] px-[23px] pt-[19px] pb-[21px] @max-[600px]/shell:px-[15px] @max-[600px]/shell:pt-4 @max-[600px]/shell:pb-[18px]",
        className
      )}
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h5 className="text-sm font-[550]">{title}</h5>
        {result && <VerificationStatus result={result} withIcon />}
        {meta && <span className="text-xs text-muted-foreground">{meta}</span>}
        {note && <span className="text-xs text-muted-foreground">{note}</span>}
      </header>
      {children}
    </div>
  )
}

type VerificationCommand = {
  result: VerificationResult
  command: string
  source?: React.ReactNode
  exitCode?: React.ReactNode
  duration?: React.ReactNode
  /** Diagnostics shown beneath the command (output, reasons…). */
  details?: React.ReactNode
}

function VerificationCommands({
  commands,
  className,
}: {
  commands: VerificationCommand[]
  className?: string
}) {
  const head =
    "h-auto pr-3 pb-1.5 pl-0 text-xs font-medium text-muted-foreground"
  const cell = "py-[7px] pr-3 pl-0 align-top text-sm whitespace-normal"
  return (
    <Table className={cn("mt-[13px] mb-1", className)}>
      <TableHeader className="[&_tr]:border-0">
        <TableRow className="border-0 hover:bg-transparent">
          <TableHead className={head}>Status</TableHead>
          <TableHead className={head}>Command</TableHead>
          <TableHead className={cn(head, "@max-[600px]/shell:hidden")}>
            Source
          </TableHead>
          <TableHead className={head}>Exit</TableHead>
          <TableHead className={head}>Duration</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {commands.map((command, index) => (
          <TableRow key={index} className="border-0 hover:bg-transparent">
            <TableCell className={cell}>
              <VerificationStatus result={command.result} />
            </TableCell>
            <TableCell className={cn(cell, "w-full")}>
              <code className="text-[0.8125rem] wrap-anywhere whitespace-pre-wrap">
                {command.command}
              </code>
              {command.details}
            </TableCell>
            <TableCell className={cn(cell, "@max-[600px]/shell:hidden")}>
              {command.source}
            </TableCell>
            <TableCell className={cn(cell, "tabular-nums")}>
              {command.exitCode ?? "—"}
            </TableCell>
            <TableCell className={cn(cell, "whitespace-nowrap tabular-nums")}>
              {command.duration}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Output / diagnostics disclosure under a verification command. */
function CommandOutput({
  summary = "Output",
  defaultOpen,
  reason,
  children,
}: {
  summary?: React.ReactNode
  defaultOpen?: boolean
  reason?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <details
      open={defaultOpen}
      className="group/output mt-2 leading-[1.6] whitespace-normal"
    >
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 font-medium [&::-webkit-details-marker]:hidden">
        <Icon
          icon="right"
          size={12}
          className="transition-transform duration-150 group-open/output:rotate-90"
        />
        {summary}
      </summary>
      <div className="mt-2">
        {reason && (
          <p className="max-w-[75ch] wrap-anywhere text-destructive">
            {reason}
          </p>
        )}
        {children && (
          <pre
            tabIndex={0}
            className="mt-2 max-h-[280px] overflow-auto overscroll-contain rounded-(--radius-item) border bg-background px-3.5 py-3 font-mono text-[0.8125rem]/[1.7] wrap-anywhere whitespace-pre-wrap"
          >
            {children}
          </pre>
        )}
      </div>
    </details>
  )
}

/* -------------------------------------------------------------------------- */
/* Handoffs                                                                   */
/* -------------------------------------------------------------------------- */

/** A bordered, collapsible iteration: header band, summary row, agent entries. */
function HandoffIteration({
  title,
  latest = false,
  count,
  summary,
  defaultOpen = true,
  className,
  children,
}: {
  title: React.ReactNode
  latest?: boolean
  count?: React.ReactNode
  summary?: React.ReactNode
  defaultOpen?: boolean
  className?: string
  children?: React.ReactNode
}) {
  return (
    <Collapsible
      defaultOpen={defaultOpen}
      render={<section />}
      className={cn(
        "mb-6 overflow-hidden rounded-(--radius-card) border",
        className
      )}
    >
      <h3 className="m-0 bg-muted">
        <CollapsibleTrigger className="group/iteration flex min-h-[46px] w-full items-center justify-start gap-2.5 px-[17px] py-3 text-left text-sm font-[550] focus-visible:outline-offset-[-4px]! @max-[600px]/shell:gap-[7px] @max-[600px]/shell:px-3">
          <Icon
            icon="right"
            size={14}
            className="shrink-0 transition-transform duration-150 group-data-panel-open/iteration:rotate-90"
          />
          {title}
          {latest && (
            <Chip tone="outline" className="px-1.5 py-0.5 font-[450]">
              Latest
            </Chip>
          )}
          {count && (
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              {count}
            </span>
          )}
        </CollapsibleTrigger>
      </h3>
      {summary && (
        <div className="flex flex-wrap justify-between gap-x-[18px] gap-y-[7px] bg-muted pr-[17px] pb-3 pl-[42px] text-xs/[1.6] text-muted-foreground @max-[600px]/shell:pl-[34px]">
          {summary}
        </div>
      )}
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  )
}

/** One agent's handoff within an iteration. */
function HandoffAgent({
  name,
  state,
  status,
  meta,
  className,
  children,
}: {
  name: React.ReactNode
  state?: RunState
  status?: React.ReactNode
  meta?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <section
      data-slot="handoff-agent"
      className={cn(
        "min-w-0 px-[23px] pt-[23px] pb-[25px] not-first:border-t @max-[600px]/shell:px-[15px] @max-[600px]/shell:pt-[18px] @max-[600px]/shell:pb-[21px]",
        className
      )}
    >
      <div className="flex flex-wrap items-center gap-[9px]">
        {state && <RunStateIcon state={state} />}
        <h4 className="text-sm font-[550]">{name}</h4>
        {status && (
          <span className="ml-auto text-xs text-muted-foreground capitalize @max-[600px]/shell:ml-0">
            {status}
          </span>
        )}
      </div>
      {meta && (
        <div className="mt-[9px] mb-[19px] ml-[26px] flex flex-wrap gap-x-4 gap-y-1.5 text-xs wrap-anywhere text-muted-foreground @max-[600px]/shell:ml-0">
          {meta}
        </div>
      )}
      {children}
    </section>
  )
}

/** "Result" block with a verdict chip and summary prose. */
function HandoffResult({
  title = "Result",
  verdict,
  className,
  children,
}: {
  title?: React.ReactNode
  verdict?: "approved" | "needs-changes"
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div className={cn("mt-[21px]", className)}>
      <h5 className="mb-2.5 text-sm font-medium text-muted-foreground">
        {title}
      </h5>
      {verdict && (
        <Chip
          tone={verdict === "approved" ? "success" : "warning"}
          icon={verdict === "approved" ? "completed" : "warning"}
          className="py-1"
        >
          {verdict === "approved" ? "Approved" : "Needs changes"}
        </Chip>
      )}
      {children && (
        <p className="mt-2 mb-3.5 max-w-[75ch] text-sm/[1.7] wrap-anywhere whitespace-pre-wrap">
          {children}
        </p>
      )}
    </div>
  )
}

/** A review finding: severity, location and explanation on a left rule. */
function Finding({
  severity,
  location,
  note,
  className,
  children,
}: {
  severity: React.ReactNode
  location?: React.ReactNode
  note?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="finding"
      className={cn("my-[13px] border-l py-0.5 pl-[13px]", className)}
    >
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-[5px]">
        <strong className="text-sm font-[550] capitalize">{severity}</strong>
        {location && (
          <code className="text-xs wrap-anywhere text-muted-foreground">
            {location}
          </code>
        )}
        {note && (
          <small className="text-xs text-muted-foreground">{note}</small>
        )}
      </div>
      {children && (
        <p className="my-[5px] max-w-[75ch] text-sm/[1.7] wrap-anywhere whitespace-pre-wrap">
          {children}
        </p>
      )}
    </div>
  )
}

export {
  VerificationStatus,
  VerificationRound,
  VerificationCommands,
  CommandOutput,
  HandoffIteration,
  HandoffAgent,
  HandoffResult,
  Finding,
  type VerificationResult,
  type VerificationCommand,
}
