import { cn } from "cn"

import type { ChartConfig } from "@/components/ui/chart"

/*
 * What the overview's charts share: their axes, the fixed colour slots of
 * the things a chart compares (models, workflows and agents), and the marks
 * their legends and tooltips draw. Ported from the delivery overviews of
 * forge-aidlc-parent, so the two read alike.
 */

export const axisProps = {
  tickLine: false,
  axisLine: false,
  tickMargin: 8,
  fontSize: 11,
} as const

// Room at the right for the last date's label, centred on the plot's edge.
export const chartMargin = { top: 6, right: 18, left: 0, bottom: 0 } as const

/**
 * A compared series' slot: the five most used in fixed colours, by use,
 * and Other for the rest.
 */
export type SeriesSlot = `m${number}` | "other"

/** A compared thing as the charts name it, in its slot. */
export type Series = { key: SeriesSlot; label: string }

/** Each slot's colour, fixed by slot; Other stays graphite. */
export const SERIES_COLOR: Record<SeriesSlot, string> = {
  m1: "var(--viz-1)",
  m2: "var(--viz-2)",
  m3: "var(--viz-3)",
  m4: "var(--viz-4)",
  m5: "var(--viz-5)",
  other: "var(--chart-3)",
}

export const SERIES_SWATCH: Record<SeriesSlot, string> = {
  m1: "bg-viz-1",
  m2: "bg-viz-2",
  m3: "bg-viz-3",
  m4: "bg-viz-4",
  m5: "bg-viz-5",
  other: "bg-chart-3",
}

export const seriesConfig = (series: Series[]): ChartConfig =>
  Object.fromEntries(
    series.map((entry) => [
      entry.key,
      { label: entry.label, color: SERIES_COLOR[entry.key] },
    ])
  )

/**
 * Slots for some things by how much of something they used: the five that
 * used most in fixed slots, the rest as Other, none for what used nothing.
 */
export function slotsBy<T>(
  items: T[],
  keyOf: (item: T) => string,
  amountOf: (item: T) => number,
  nameOf: (item: T) => string
) {
  const slots = new Map<string, SeriesSlot>()
  items
    .filter((item) => amountOf(item) > 0)
    .sort(
      (a, b) => amountOf(b) - amountOf(a) || nameOf(a).localeCompare(nameOf(b))
    )
    .forEach((item, index) =>
      slots.set(keyOf(item), index < 5 ? `m${index + 1}` : "other")
    )
  return slots
}

/** A tooltip row: the series' mark, its name and its value, formatted. */
export const tooltipRow =
  (config: ChartConfig, format: (value: number) => string) =>
  (value: unknown, name: unknown, item: { color?: string }) => (
    <>
      <span
        aria-hidden="true"
        className="size-2.5 shrink-0 rounded-[2px]"
        style={{ background: item.color }}
      />
      <span className="flex flex-1 items-center justify-between gap-4 leading-none">
        <span className="text-muted-foreground">
          {config[String(name)]?.label ?? String(name)}
        </span>
        <span className="font-medium text-foreground tabular-nums">
          {format(Number(value))}
        </span>
      </span>
    </>
  )

/** A legend's square mark for a bar or area series. */
export const swatch = (className: string) => (
  <span
    aria-hidden="true"
    className={cn("size-2 shrink-0 rounded-[2px]", className)}
  />
)
