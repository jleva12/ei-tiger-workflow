import type {
  Series,
  SeriesSlot,
} from "@/features/overview/components/chart-kit"
import { plural, type Unit } from "@/features/overview/lib/format"
import type {
  OverviewScope,
  OverviewSubject,
  OverviewSummary,
  SubjectKind,
} from "@/features/overview/lib/overview"

/*
 * How the overview's charts split what they show: by type (in a fixed
 * order, down the blue ramp) or by workflow or agent (in their colour
 * slots, which the ledger shows too).
 */

export const KIND_ORDER: SubjectKind[] = ["workflow", "agent", "assistant"]

/** What the usage panels share. */
export type UsageContext = {
  summary: OverviewSummary
  scope: OverviewScope
  unit: Unit
  /** The dates to label, shared by every chart. */
  ticks: number[]
  /** Each workflow's or agent's colour slot, by key. */
  slots: Map<string, SeriesSlot>
  subjects: Map<string, OverviewSubject>
}

/** Types in a fixed order, strongest first: workflows, agents, the assistant. */
export const KIND_COLOR: Record<
  SubjectKind,
  { color: string; swatch: string }
> = {
  workflow: { color: "var(--viz-ramp-1)", swatch: "bg-viz-ramp-1" },
  agent: { color: "var(--viz-ramp-3)", swatch: "bg-viz-ramp-3" },
  assistant: { color: "var(--chart-3)", swatch: "bg-chart-3" },
}

/**
 * The series a chart stacks by workflow or agent: the slots in use in the
 * scope, named, in slot order, then Other.
 */
export function subjectSeries(
  context: UsageContext,
  measure: "tokens" | "cost" | "started"
) {
  const { summary, slots, subjects } = context
  const used = new Set<string>()
  for (const bucket of summary.buckets) {
    for (const [key, value] of Object.entries(bucket.bySubject)) {
      if (value[measure] > 0) used.add(key)
    }
  }
  const series: Series[] = []
  let others = 0
  for (const key of used) {
    const slot = slots.get(key) ?? "other"
    if (slot === "other") others += 1
    else series.push({ key: slot, label: subjects.get(key)?.name ?? key })
  }
  series.sort((a, b) =>
    a.key.localeCompare(b.key, undefined, { numeric: true })
  )
  if (others) series.push({ key: "other", label: plural(others, "other") })
  const rows = summary.buckets.map((bucket) => {
    const row: Record<string, number> = { start: bucket.start }
    for (const entry of series) row[entry.key] = 0
    for (const [key, value] of Object.entries(bucket.bySubject)) {
      const slot = slots.get(key) ?? "other"
      if (slot in row) row[slot] += value[measure]
    }
    return row
  })
  return { series, rows }
}

/** `openai/gpt-5.2` → `gpt-5.2`; the provider stays in the cell's title. */
export const shortModel = (model: string) =>
  model.includes("/") ? model.slice(model.indexOf("/") + 1) : model
