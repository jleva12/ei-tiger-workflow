import * as React from "react"
import { cn } from "cn"

import {
  Table,
  TableBody,
  TableFooter,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/*
 * Dense numeric tables (token usage, cost, quotas): grouped headers, iteration
 * bands, subtotal rows and a totals footer, all with tabular numerals. The
 * frame scrolls horizontally and is keyboard-focusable on narrow screens.
 */

function MetricsTable({
  label,
  minWidth = 925,
  className,
  children,
}: {
  /** Accessible name for the scrollable frame. */
  label: string
  minWidth?: number
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      data-slot="metrics-table"
      role="region"
      aria-label={label}
      tabIndex={0}
      className={cn(
        "overflow-auto rounded-(--radius-band) border [&>[data-slot=table-container]]:overflow-visible",
        className
      )}
    >
      <Table
        style={{ minWidth }}
        className="border-separate border-spacing-0 text-sm tabular-nums [&_td]:border-b [&_td]:px-2.5 [&_td]:py-[15px] [&_th]:border-b [&_th]:px-2.5 [&_th]:py-[15px] [&_tr]:border-0 [&_tr]:hover:bg-transparent"
      >
        {children}
      </Table>
    </div>
  )
}

const rowVariants = {
  /** Top header row spanning column groups ("Input", "Output", "Totals"). */
  group:
    "[&>th]:bg-muted [&>th]:py-3 [&>th]:text-center [&>th]:text-[0.8125rem] [&>th]:font-semibold [&>th[rowspan]]:text-left",
  /** Second header row with right-aligned column labels. */
  column:
    "[&>th]:bg-[color-mix(in_oklch,var(--muted)_45%,var(--background))] [&>th]:py-2.5 [&>th]:text-right [&>th]:text-[0.8125rem] [&>th]:font-medium [&>th]:text-muted-foreground",
  /** Section band (e.g. "Iteration 2"). */
  band: "[&>th]:bg-[color-mix(in_oklch,var(--muted)_60%,var(--background))] [&>th]:py-2.5 [&>th]:text-left [&>th]:font-semibold",
  /** A body row; hover tints it. */
  row: "hover:bg-[color-mix(in_oklch,var(--muted)_35%,var(--background))]!",
  /** Subtotal after a section. */
  subtotal:
    "[&>*]:bg-[color-mix(in_oklch,var(--muted)_25%,var(--background))] [&>*]:py-3.5 [&>*]:font-[550]",
  /** Grand total in the footer. */
  total:
    "[&>*]:border-t [&>*]:border-b-0! [&>*]:bg-muted [&>*]:py-3.5 [&>*]:font-semibold",
} as const

function MetricsRow({
  variant = "row",
  className,
  ...props
}: React.ComponentProps<typeof TableRow> & {
  variant?: keyof typeof rowVariants
}) {
  return (
    <TableRow
      data-variant={variant}
      className={cn(rowVariants[variant], className)}
      {...props}
    />
  )
}

/** Right-aligned value with an optional secondary line (e.g. its cost). */
function MetricValue({
  value,
  secondary,
  strong = false,
  divider = false,
  className,
}: {
  value: React.ReactNode
  secondary?: React.ReactNode
  strong?: boolean
  /** Draw a hairline on the left to start a column group. */
  divider?: boolean
  className?: string
}) {
  return (
    <td
      className={cn(
        "text-right align-middle",
        strong && "font-semibold",
        divider && "border-l",
        className
      )}
    >
      {value}
      {secondary && (
        <span className="mt-1 block text-[0.8125rem]/[1.4] font-normal text-muted-foreground">
          {secondary}
        </span>
      )}
    </td>
  )
}

/** Muted placeholder for values a provider did not report. */
function Unreported({
  children = "Not reported",
}: {
  children?: React.ReactNode
}) {
  return (
    <span className="text-[0.8125rem] font-normal text-muted-foreground">
      {children}
    </span>
  )
}

/** Summary line above a metrics table with an emphasised grand total. */
function MetricsSummary({
  total,
  totalLabel,
  className,
  children,
}: {
  total?: React.ReactNode
  totalLabel?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="metrics-summary"
      className={cn(
        "flex flex-wrap items-baseline gap-x-[26px] gap-y-3 pb-[18px] text-[0.8125rem] text-muted-foreground tabular-nums [&_strong]:font-semibold [&_strong]:text-foreground",
        className
      )}
    >
      {children}
      {total && (
        <span className="ml-auto inline-flex items-baseline gap-2 @max-[760px]/shell:ml-0 @max-[760px]/shell:basis-full">
          {totalLabel}
          <strong className="text-lg tracking-[-.02em]">{total}</strong>
        </span>
      )}
    </div>
  )
}

export {
  MetricsTable,
  MetricsRow,
  MetricValue,
  Unreported,
  MetricsSummary,
  TableHeader as MetricsHeader,
  TableBody as MetricsBody,
  TableFooter as MetricsFooter,
}
