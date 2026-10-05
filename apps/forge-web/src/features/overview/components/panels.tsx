import * as React from "react"
import { cn } from "cn"
import { Bar } from "recharts"

import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  OVERVIEW_PERIODS,
  type OverviewPeriod,
} from "@/features/overview/lib/overview"

/*
 * The parts of an organization's overview, as forge-aidlc-parent's delivery
 * overviews draw them (DESIGN.md: Read-out Sentence, Portfolio Ledger,
 * Chart Panels): a headline sentence, hairline-divided panels of quiet
 * charts with a one-line answer each, figures with their change against the
 * period before, and bar lists.
 */

/* Period ------------------------------------------------------------------ */

/** The 7, 30 or 90 days the overview reads, in the top bar. */
export function PeriodSwitch({
  value,
  onValueChange,
}: {
  value: OverviewPeriod
  onValueChange: (period: OverviewPeriod) => void
}) {
  return (
    <ToggleGroup
      aria-label="Period"
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onValueChange(next[0] as OverviewPeriod)
      }}
      spacing={0}
      className="rounded-(--radius-control) bg-muted p-[3px]"
    >
      {(Object.keys(OVERVIEW_PERIODS) as OverviewPeriod[]).map((key) => (
        <ToggleGroupItem
          key={key}
          value={key}
          aria-label={`Last ${OVERVIEW_PERIODS[key].label}`}
          className="h-[26px] min-w-0 rounded-(--radius-soft)! px-2.5 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised) @max-[600px]/shell:px-2"
        >
          <span className="@max-[600px]/shell:hidden">
            {OVERVIEW_PERIODS[key].label}
          </span>
          <span className="hidden @max-[600px]/shell:inline">{key}</span>
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** A small switch beside a panel's title: which way its chart splits. */
export function SplitSwitch<T extends string>({
  label,
  options,
  value,
  onValueChange,
}: {
  label: string
  options: Record<T, string>
  value: T
  onValueChange: (value: T) => void
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onValueChange(next[0] as T)
      }}
      spacing={0}
      className="rounded-(--radius-control) bg-muted p-0.5"
    >
      {(Object.keys(options) as T[]).map((key) => (
        <ToggleGroupItem
          key={key}
          value={key}
          className="h-[22px] min-w-0 rounded-(--radius-soft)! px-2 text-2xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
        >
          {options[key]}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/* Read-out ---------------------------------------------------------------- */

/** A fact in the read-out: ink on the slate sentence, so a skim reads the result. */
export function Fact({ children }: { children: React.ReactNode }) {
  return <strong className="font-[550] text-foreground">{children}</strong>
}

/** The sentence that opens the overview, at headline size. */
export function ReadOutText({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-1 mb-7 max-w-[64ch] text-[1.3125rem]/[1.5] font-normal tracking-[-.4px] text-balance text-muted-foreground @max-[600px]/shell:text-[1.0625rem]/[1.5] @max-[600px]/shell:tracking-[-.2px]">
      {children}
    </p>
  )
}

/* Figures ----------------------------------------------------------------- */

/** A figure over a quieter second line: its change, or what it's per. */
export function Figure({
  value,
  change,
  total,
}: {
  value: React.ReactNode
  change: React.ReactNode
  total: boolean
}) {
  return (
    <span className="flex flex-col items-end">
      {value === null ? (
        <span className="text-muted-foreground">None</span>
      ) : (
        <span className={cn(total && "font-[550]")}>{value}</span>
      )}
      <span className="text-2xs text-muted-foreground">{change}</span>
    </span>
  )
}

/**
 * A change against the period before: signed, and tinted only when it moved
 * enough to matter, green for better and red for worse. Volume (tokens,
 * calls, runs) has no better or worse, so it stays untinted.
 */
export function Delta({
  current,
  previous,
  better,
  threshold,
  format,
}: {
  current: number | null
  previous: number | null
  better: "higher" | "lower" | "neither"
  threshold: number
  format: (change: number) => string
}) {
  if (current === null || previous === null) return <>No earlier figure</>
  const change = current - previous
  if (Math.abs(change) < threshold) return <>Steady</>
  if (better === "neither") {
    return (
      <span>
        {change > 0 ? "+" : "−"}
        {format(change)}
        <span className="sr-only"> on the period before</span>
      </span>
    )
  }
  const improved = better === "higher" ? change > 0 : change < 0
  return (
    <span
      className={
        improved ? "text-success-foreground" : "text-danger-foreground"
      }
    >
      {change > 0 ? "+" : "−"}
      {format(change)}
      <span className="sr-only">
        {improved ? ", better than" : ", worse than"} the period before
      </span>
    </span>
  )
}

/** A relative change in volume: "+12%", "−40%"; a start from nothing is "new". */
export function VolumeDelta({
  current,
  previous,
}: {
  current: number
  previous: number
}) {
  if (previous === 0)
    return <>{current === 0 ? "No change" : "New this period"}</>
  const change = current / previous - 1
  if (Math.abs(change) < 0.02) return <>Steady</>
  return (
    <span>
      {change > 0 ? "+" : "−"}
      {Math.round(Math.abs(change) * 100)}%
      <span className="sr-only"> on the period before</span>
    </span>
  )
}

/**
 * Something each week over the last 12 weeks as small columns, sharing a
 * scale down the ledger, so rows' trends compare at a glance.
 */
export function WeekBars({ weeks, peak }: { weeks: number[]; peak: number }) {
  const gap = 2
  const bar = 5
  const width = weeks.length * bar + gap * (weeks.length - 1)
  const height = 22
  return (
    <svg
      aria-hidden="true"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="shrink-0 overflow-visible"
    >
      <line
        x1={0}
        x2={width}
        y1={height - 0.5}
        y2={height - 0.5}
        strokeWidth={1}
        className="stroke-chart-4"
      />
      {weeks.map((count, index) =>
        count ? (
          <rect
            key={index}
            x={index * (bar + gap)}
            y={height - Math.max(2, (count / peak) * height)}
            width={bar}
            height={Math.max(2, (count / peak) * height)}
            rx={1}
            className="fill-viz-ramp-3"
          />
        ) : null
      )}
    </svg>
  )
}

/**
 * A small warm cell for work waiting on people: a run's ID, or a count.
 * A day or more of waiting rings it.
 */
export function HandoffKey({
  label,
  overdue,
  mono = true,
}: {
  label: React.ReactNode
  overdue: boolean
  /** Run IDs are IDs, so monospace; counts are figures. */
  mono?: boolean
}) {
  return (
    <span
      aria-hidden="true"
      data-overdue={overdue || undefined}
      className={cn(
        "inline-flex h-[19px] shrink-0 items-center rounded-(--radius-chip) border border-notice-border bg-notice-surface px-[5px] text-3xs font-normal text-notice-chip-foreground data-overdue:border-notice-accent data-overdue:ring-[1.5px] data-overdue:ring-notice-accent/25",
        mono ? "font-mono" : "font-medium tabular-nums"
      )}
    >
      {label}
    </span>
  )
}

/* Chart panels ------------------------------------------------------------ */

/** One of the hairline-divided panels of the overview. */
export function Panel({
  title,
  answer,
  action,
  className,
  children,
}: {
  title: string
  answer: React.ReactNode
  /** A control beside the title, such as a breakdown switch. */
  action?: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn(
        "flex min-w-0 flex-col border-l px-6 pt-5 pb-2 first:border-l-0 first:pl-0 last:pr-0 @max-[900px]/shell:border-t @max-[900px]/shell:border-l-0 @max-[900px]/shell:px-0 @max-[900px]/shell:first:border-t-0",
        className
      )}
    >
      <div className="flex min-h-5 items-center justify-between gap-3">
        <h3 className="text-xs font-medium">{title}</h3>
        {action}
      </div>
      <p className="mt-1 mb-4 min-h-[2lh] text-xs/[1.55] text-muted-foreground">
        {answer}
      </p>
      {children}
    </section>
  )
}

/** A row of panels under a top hairline: three columns, stacked when narrow. */
export function PanelRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-3 border-t @max-[900px]/shell:grid-cols-1">
      {children}
    </div>
  )
}

/** Facts under a chart: label on the left, value on the right. */
export function Facts({
  items,
  className,
}: {
  items: [React.ReactNode, React.ReactNode][]
  className?: string
}) {
  return (
    <dl className={cn("mt-3 border-t text-xs", className)}>
      {items.map(([label, value], index) => (
        <div
          key={index}
          className="flex items-baseline justify-between gap-4 border-b py-2 last:border-b-0"
        >
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="font-medium tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** A chart's legend: a mark, its label and its total. */
export function Legend({
  items,
}: {
  items: {
    key: string
    label: string
    value: React.ReactNode
    mark: React.ReactNode
  }[]
}) {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-2xs text-muted-foreground">
      {items.map((item) => (
        <li key={item.key} className="flex items-center gap-1.5">
          {item.mark}
          {item.label}
          <span className="font-medium text-foreground tabular-nums">
            {item.value}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** Buckets as a table for screen readers; the chart itself is hidden from them. */
export function ChartTable({
  caption,
  columns,
  rows,
}: {
  caption: string
  columns: string[]
  rows: React.ReactNode[][]
}) {
  return (
    <div className="sr-only">
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, i) => (
                <td key={i}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Stacked bars of some series, each segment parted by a surface edge.
 * Recharts reads its children directly, so call it as a function.
 */
export function StackedBars({
  keys,
  stackId,
}: {
  keys: string[]
  stackId: string
}) {
  return keys.map((key, index) => (
    <Bar
      key={key}
      dataKey={key}
      stackId={stackId}
      fill={`var(--color-${key})`}
      // The surface-coloured edge is the gap between stacked segments.
      stroke="var(--background)"
      strokeWidth={1}
      radius={index === keys.length - 1 ? [2, 2, 0, 0] : 0}
      maxBarSize={24}
      isAnimationActive={false}
    />
  ))
}

/** A row of a bar list. */
export type BarListRow = {
  key: string
  label: React.ReactNode
  value: number
  /** A quieter figure after the value, such as its share. */
  detail?: React.ReactNode
  /** A mark before the label, such as the series' swatch. */
  mark?: React.ReactNode
  /** The bar's fill; the ramp's single step when unset. */
  barClassName?: string
}

/**
 * Some values as bars on one scale, each labelled and figured: tokens by
 * workflow, spend by model, runs by member.
 */
export function BarList({
  rows,
  format,
  labelWidth = "minmax(0,9rem)",
  className,
}: {
  rows: BarListRow[]
  format: (value: number) => string
  /** The label column's width. */
  labelWidth?: string
  className?: string
}) {
  const top = Math.max(Number.EPSILON, ...rows.map((row) => row.value))
  return (
    <dl className={cn("border-t text-xs", className)}>
      {rows.map((row) => (
        <div
          key={row.key}
          style={{ gridTemplateColumns: `${labelWidth} minmax(0,1fr) auto` }}
          className="grid items-center gap-3 border-b py-2 last:border-b-0"
        >
          <dt className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
            {row.mark}
            <span className="truncate">{row.label}</span>
          </dt>
          <span aria-hidden="true" className="h-1.5 min-w-0">
            <span
              className={cn(
                "block h-full rounded-r-[2px]",
                row.barClassName ?? "bg-viz-ramp-3"
              )}
              style={{
                width: `${Math.max(1, (row.value / top) * 100)}%`,
              }}
            />
          </span>
          <dd className="text-right font-medium tabular-nums">
            {format(row.value)}
            {row.detail !== undefined && (
              <span className="ml-1.5 font-normal text-muted-foreground">
                {row.detail}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/* Loading ----------------------------------------------------------------- */

/** The overview's shape while it loads: the read-out, the ledger, the panels. */
export function OverviewSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading the overview"
      className="flex flex-col"
    >
      <Skeleton className="mt-1 mb-2.5 h-6 w-[min(640px,100%)] rounded-(--radius-soft)" />
      <Skeleton className="mb-8 h-6 w-[min(420px,80%)] rounded-(--radius-soft)" />
      <div className="grid grid-cols-3 gap-6 @max-[900px]/shell:grid-cols-1">
        <Skeleton className="col-span-2 h-64 rounded-(--radius-card) @max-[900px]/shell:col-span-1" />
        <Skeleton className="h-64 rounded-(--radius-card)" />
      </div>
      <div className="mt-10 overflow-hidden rounded-(--radius-band) border">
        <div className="flex items-center gap-3 px-4 py-3.5">
          <Skeleton className="h-4 w-24 rounded-(--radius-chip)" />
          <Skeleton className="ml-auto h-7 w-44 rounded-(--radius-control)" />
        </div>
        {Array.from({ length: 4 }, (_, index) => (
          <div
            key={index}
            className="flex items-center gap-6 border-t px-4 py-4"
          >
            <Skeleton className="h-4 w-40 rounded-(--radius-chip)" />
            <Skeleton className="ml-auto h-4 w-24 rounded-(--radius-chip)" />
            <Skeleton className="h-4 w-16 rounded-(--radius-chip)" />
            <Skeleton className="h-4 w-20 rounded-(--radius-chip)" />
          </div>
        ))}
      </div>
    </div>
  )
}
