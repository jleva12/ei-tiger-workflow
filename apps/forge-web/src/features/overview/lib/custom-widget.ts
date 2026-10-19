import { z } from "zod"

import {
  SERIES_COLOR,
  SERIES_SWATCH,
  slotsBy,
  type SeriesSlot,
} from "@/features/overview/components/chart-kit"
import {
  CHART_TYPES,
  DEFAULT_CHART_TYPE,
  type ChartType,
} from "@/features/overview/lib/chart-type"
import {
  retitled,
  withSettings,
  withWidget,
  type DashboardWidget,
} from "@/features/overview/lib/dashboard"
import { compact, duration, percent, usd } from "@/features/overview/lib/format"
import {
  addActivity,
  addUsage,
  emptyTotals,
  inScope,
  kindOf,
  SUBJECT_KINDS,
  type OrganizationOverview,
  type OverviewScope,
  type OverviewSubject,
  type SubjectKind,
  type Totals,
} from "@/features/overview/lib/overview"
import {
  KIND_COLOR,
  KIND_ORDER,
  shortModel,
} from "@/features/overview/lib/series"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * Widgets people define themselves: a measure of what the organization's
 * workflows, agents and assistant did (a sum, or a rate of two sums), split
 * by type, by workflow or agent, or by model, and shown over the period, as
 * one total, or as a ranking. A widget is its settings in the layout: it
 * reads the overview the page already has, follows the ledger's focus, and
 * colours what it splits as every chart of the page does.
 */

/* Measures ---------------------------------------------------------------- */

type Measure = {
  /** Its name in the picker and in the widget's title. */
  label: string
  /** Where the picker lists it. */
  group: "usage" | "runs"
  /** Its value from some totals; null for a rate with nothing to divide. */
  of: (totals: Totals) => number | null
  /** A rate: it can't be stacked, or summed across a split. */
  rate: boolean
  /** Only model calls carry a model, so only their measures split by it. */
  byModel: boolean
  format: (value: number) => string
  axis: (value: number) => string
  /** Its axis ticks may fall between whole numbers. */
  decimals: boolean
  /** Which way is good, for tinting its change; volume is neither. */
  better: "higher" | "lower" | "neither"
  /** The least change worth calling a change. */
  threshold: number
}

const finished = (totals: Totals) =>
  totals.succeeded + totals.failed + totals.abandoned
const ratio =
  (num: (totals: Totals) => number, den: (totals: Totals) => number) =>
  (totals: Totals) => {
    const below = den(totals)
    return below ? num(totals) / below : null
  }
const dollars = (value: number) =>
  value < 10 && value % 1 ? `$${value.toFixed(2)}` : `$${compact(value)}`

const volume = (
  label: string,
  group: Measure["group"],
  field: keyof Totals,
  byModel: boolean,
  better: Measure["better"] = "neither"
): Measure => ({
  label,
  group,
  of: (totals) => totals[field],
  rate: false,
  byModel,
  format: compact,
  axis: compact,
  decimals: false,
  better,
  threshold: 0,
})

export const MEASURES = {
  tokens: volume("Model tokens", "usage", "tokens", true),
  input: volume("Input tokens", "usage", "input", true),
  output: volume("Output tokens", "usage", "output", true),
  cached: volume("Cached input tokens", "usage", "cached", true),
  thinking: volume("Thinking tokens", "usage", "thinking", true),
  calls: volume("Model calls", "usage", "calls", true),
  failedCalls: volume(
    "Failed model calls",
    "usage",
    "failedCalls",
    true,
    "lower"
  ),
  cost: {
    ...volume("Model spend", "usage", "cost", true),
    format: usd,
    axis: dollars,
    decimals: true,
  },
  tokensPerCall: {
    ...volume("Tokens per model call", "usage", "tokens", true),
    of: ratio(
      (totals) => totals.tokens,
      (totals) => totals.calls
    ),
    rate: true,
    format: compact,
    threshold: 1,
  },
  started: volume("Runs and invocations", "runs", "started", false),
  succeeded: volume("Successful runs", "runs", "succeeded", false, "higher"),
  failed: volume("Failed runs", "runs", "failed", false, "lower"),
  abandoned: volume("Abandoned runs", "runs", "abandoned", false, "lower"),
  retried: volume("Retried runs", "runs", "retried", false, "lower"),
  successRate: {
    ...volume("Success rate", "runs", "succeeded", false, "higher"),
    of: ratio((totals) => totals.succeeded, finished),
    rate: true,
    format: percent,
    axis: percent,
    decimals: true,
    threshold: 0.005,
  },
  finishTime: {
    ...volume("Time to finish", "runs", "durationMs", false, "lower"),
    of: ratio(
      (totals) => totals.durationMs,
      (totals) => totals.timed
    ),
    rate: true,
    format: duration,
    axis: duration,
    threshold: 1000,
  },
  costPerRun: {
    ...volume("Spend per run", "runs", "cost", false, "lower"),
    of: ratio(
      (totals) => totals.cost,
      (totals) => totals.started
    ),
    rate: true,
    format: usd,
    axis: dollars,
    decimals: true,
    threshold: 0.005,
  },
} satisfies Record<string, Measure>

export type MeasureKey = keyof typeof MEASURES

export const MEASURE_GROUPS: Record<Measure["group"], string> = {
  usage: "Model usage",
  runs: "Runs",
}

/* The other choices -------------------------------------------------------- */

export const SPLITS = {
  none: { label: "Nothing", by: "" },
  kind: { label: "Type", by: "by type" },
  subject: { label: "Workflow or agent", by: "by workflow or agent" },
  model: { label: "Model", by: "by model" },
} as const

export type SplitKey = keyof typeof SPLITS

export const SHOWS = {
  time: "Over time",
  total: "Total",
  ranking: "Ranking",
} as const

export type ShowKey = keyof typeof SHOWS

export const ONLY = {
  all: "All workflows and agents",
  workflow: "Workflows",
  agent: "Agents",
  assistant: "Assistant",
} as const

export type OnlyKey = keyof typeof ONLY

const keysOf = <T extends object>(choices: T) =>
  Object.keys(choices) as [keyof T & string, ...(keyof T & string)[]]

/** A custom widget's settings, each with its default. */
export const CUSTOM_SETTINGS = z.object({
  measure: z.enum(keysOf(MEASURES)).default("tokens"),
  split: z.enum(keysOf(SPLITS)).default("none"),
  show: z.enum(keysOf(SHOWS)).default("time"),
  chart: z.enum(CHART_TYPES).default(DEFAULT_CHART_TYPE),
  only: z.enum(keysOf(ONLY)).default("all"),
})

export type CustomSpec = z.infer<typeof CUSTOM_SETTINGS>

export const DEFAULT_SPEC: CustomSpec = CUSTOM_SETTINGS.parse({})

/** A custom widget's spec from its settings, defaults filling what's left out. */
export const specOf = (settings: Record<string, unknown>): CustomSpec => {
  const parsed = CUSTOM_SETTINGS.safeParse(settings)
  return parsed.success ? parsed.data : DEFAULT_SPEC
}

/**
 * A spec as it can be drawn. What a measure can't be split by falls back to
 * nothing, a ranking of nothing is a total, and a rate split into series is
 * drawn as lines, since rates don't stack.
 */
export function drawable(spec: CustomSpec): CustomSpec {
  const measure = MEASURES[spec.measure]
  const split = spec.split === "model" && !measure.byModel ? "none" : spec.split
  const show = spec.show === "ranking" && split === "none" ? "total" : spec.show
  const chart: ChartType =
    measure.rate && split !== "none" ? "line" : spec.chart
  return { ...spec, split, show, chart }
}

/** Whether the spec's chart type is fixed (a rate split into series). */
export const linesOnly = (spec: CustomSpec) =>
  MEASURES[spec.measure].rate && drawable(spec).split !== "none"

/** The name a custom widget goes by until it's given one. */
export function titleOf(spec: CustomSpec) {
  const drawn = drawable(spec)
  const by = SPLITS[drawn.split].by
  const only = drawn.only === "all" ? "" : ` · ${ONLY[drawn.only]}`
  return `${MEASURES[drawn.measure].label}${by ? ` ${by}` : ""}${only}`
}

/* In a layout ------------------------------------------------------------- */

/**
 * A custom widget's spec and title set: its settings keep only what isn't
 * the default, and a title that's only the name it would go by is no title.
 */
export function withCustomSpec(
  widgets: readonly DashboardWidget[],
  id: string,
  spec: CustomSpec,
  title: string
): DashboardWidget[] {
  const own = title.trim()
  return retitled(
    withSettings(widgets, id, spec, DEFAULT_SPEC),
    id,
    own === titleOf(spec) ? "" : own
  )
}

/** A custom widget added under the rest, at a width; and its ID. */
export function withCustomWidget(
  widgets: readonly DashboardWidget[],
  spec: CustomSpec,
  title: string,
  w: number
) {
  const added = withWidget(widgets, "custom", w)
  const id = added[added.length - 1].id
  return { widgets: withCustomSpec(added, id, spec, title), id }
}

/* Reading ----------------------------------------------------------------- */

/** One series of a custom widget's chart: a colour slot, a kind, or the whole. */
export type CustomSeries = {
  key: string
  label: string
  color: string
  swatch: string
  current: number | null
  previous: number | null
}

/** One thing a ranking lists: a workflow or agent, a model or a kind. */
export type CustomEntry = {
  key: string
  label: string
  swatch: string
  current: number
}

export type CustomData = {
  spec: CustomSpec
  series: CustomSeries[]
  /** Each bucket's value by series key, at `start`; null where a rate has none. */
  rows: Record<string, number | null>[]
  /** Highest first; rates with nothing to divide are left out. */
  ranking: CustomEntry[]
  current: number | null
  previous: number | null
}

/** What colours a split: the ledger's slots, and the subjects' names. */
export type CustomColours = {
  slots: ReadonlyMap<string, SeriesSlot>
  subjects: ReadonlyMap<string, OverviewSubject>
}

const SLOT_ORDER: SeriesSlot[] = ["m1", "m2", "m3", "m4", "m5", "other"]
const WHOLE = "value"

type Pair = { current: Totals; previous: Totals }
const pair = (): Pair => ({ current: emptyTotals(), previous: emptyTotals() })

/**
 * A custom widget's figures from the overview, in the ledger's focus: its
 * series bucket by bucket, their totals against the period before, and a
 * ranking of what it splits by.
 */
export function customData(
  overview: OrganizationOverview,
  scope: OverviewScope,
  settings: CustomSpec,
  colours: CustomColours
): CustomData {
  const spec = drawable(settings)
  const measure = MEASURES[spec.measure]
  const kept = (subject: string) =>
    inScope(scope, subject) &&
    (spec.only === "all" || kindOf(subject) === spec.only)

  // Models take slots as the subjects do: by tokens, in the scope.
  const modelTokens = new Map<string, number>()
  for (const fact of overview.usage) {
    if (fact.bucket < 0 || !inScope(scope, fact.subject)) continue
    modelTokens.set(
      fact.model,
      (modelTokens.get(fact.model) ?? 0) + fact.input + fact.output
    )
  }
  const modelSlots = slotsBy(
    [...modelTokens.keys()],
    (model) => model,
    (model) => modelTokens.get(model) ?? 0,
    (model) => model
  )

  const placeOf = (subject: string, model: string) => {
    switch (spec.split) {
      case "kind":
        return { entry: kindOf(subject), series: kindOf(subject) as string }
      case "subject":
        return {
          entry: subject,
          series: colours.slots.get(subject) ?? "other",
        }
      case "model":
        return { entry: model, series: modelSlots.get(model) ?? "other" }
      default:
        return { entry: WHOLE, series: WHOLE }
    }
  }

  const whole = pair()
  const entries = new Map<string, Pair>()
  const series = new Map<string, Pair>()
  const buckets = overview.buckets.map(() => new Map<string, Totals>())
  const pairOf = (map: Map<string, Pair>, key: string) => {
    let found = map.get(key)
    if (!found) map.set(key, (found = pair()))
    return found
  }
  const add = <F extends { bucket: number; subject: string }>(
    fact: F,
    model: string,
    into: (totals: Totals, fact: F) => void
  ) => {
    if (!kept(fact.subject)) return
    const place = placeOf(fact.subject, model)
    const when = fact.bucket < 0 ? "previous" : "current"
    into(whole[when], fact)
    into(pairOf(entries, place.entry)[when], fact)
    into(pairOf(series, place.series)[when], fact)
    const bucket = buckets[fact.bucket]
    if (bucket) {
      let totals = bucket.get(place.series)
      if (!totals) bucket.set(place.series, (totals = emptyTotals()))
      into(totals, fact)
    }
  }
  for (const fact of overview.usage) add(fact, fact.model, addUsage)
  // Runs have no model: split by model, only model calls count.
  if (spec.split !== "model") {
    for (const fact of overview.activity) add(fact, "", addActivity)
  }

  const has = (found: Pair | undefined) => {
    if (!found) return false
    const value = measure.of(found.current)
    return value !== null && (measure.rate || value > 0)
  }
  const order =
    spec.split === "none"
      ? [WHOLE]
      : spec.split === "kind"
        ? KIND_ORDER.filter((kind) => has(series.get(kind)))
        : SLOT_ORDER.filter((slot) => has(series.get(slot)))

  // A slot's name is the one thing in it; Other is the rest.
  const holder = (slot: string, slots: ReadonlyMap<string, SeriesSlot>) =>
    [...slots].find(([, held]) => held === slot)?.[0]
  const nameOf = (key: string) => {
    if (spec.split === "none") return measure.label
    if (spec.split === "kind") return SUBJECT_KINDS[key as SubjectKind].plural
    if (key === "other") return "Other"
    if (spec.split === "model") {
      return shortModel(holder(key, modelSlots) ?? key)
    }
    const subject = holder(key, colours.slots)
    return (subject && colours.subjects.get(subject)?.name) ?? key
  }
  const paint = (key: string) =>
    spec.split === "none"
      ? { color: "var(--viz-ramp-1)", swatch: "bg-viz-ramp-1" }
      : spec.split === "kind"
        ? KIND_COLOR[key as SubjectKind]
        : {
            color: SERIES_COLOR[key as SeriesSlot],
            swatch: SERIES_SWATCH[key as SeriesSlot],
          }

  const drawn: CustomSeries[] = order.map((key) => {
    const found = series.get(key) ?? pair()
    return {
      key,
      label: nameOf(key),
      ...paint(key),
      current: measure.of(found.current),
      previous: measure.of(found.previous),
    }
  })

  const rows = overview.buckets.map((bucket, index) => ({
    start: parseTimestamp(bucket.start).getTime(),
    ...Object.fromEntries(
      order.map((key) => {
        const totals = buckets[index].get(key)
        return [key, totals ? measure.of(totals) : measure.rate ? null : 0]
      })
    ),
  }))

  const ranking: CustomEntry[] =
    spec.split === "none"
      ? []
      : [...entries]
          .map(([key, found]) => {
            const swatch =
              spec.split === "kind"
                ? KIND_COLOR[key as SubjectKind].swatch
                : SERIES_SWATCH[
                    (spec.split === "model"
                      ? modelSlots.get(key)
                      : colours.slots.get(key)) ?? "other"
                  ]
            const label =
              spec.split === "kind"
                ? SUBJECT_KINDS[key as SubjectKind].plural
                : spec.split === "model"
                  ? shortModel(key)
                  : (colours.subjects.get(key)?.name ?? key)
            return { key, label, swatch, current: measure.of(found.current) }
          })
          .filter(
            (entry): entry is CustomEntry =>
              entry.current !== null && (measure.rate || entry.current > 0)
          )
          .sort(
            (a, b) => b.current - a.current || a.label.localeCompare(b.label)
          )

  return {
    spec,
    series: drawn,
    rows,
    ranking,
    current: measure.of(whole.current),
    previous: measure.of(whole.previous),
  }
}
